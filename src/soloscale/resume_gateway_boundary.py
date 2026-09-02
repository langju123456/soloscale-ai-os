"""Bounded resume-upload preparation for the mock-only hosted gateway slice.

Raw selected files are parsed in memory.  Only allowlisted text, layout metadata,
and a typed, direct-identifier-sanitized payload may cross the ModelGateway
boundary.  This module intentionally contains no network transport.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import zipfile
import zlib
from dataclasses import dataclass
from enum import StrEnum
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import ClassVar, Literal
from xml.etree import ElementTree

from pydantic import BaseModel, Field
from pypdf import PdfReader

from soloscale.models import ContractModel, utc_now
from soloscale.resume_evidence_pack import (
    build_composition_evidence_plan,
    build_jd_positioning_brief,
)
from soloscale.resume_models import (
    CandidateEvidencePack,
    CandidateProfile,
    CompositionEvidencePlan,
    JDPositioningBrief,
    ResumeAtomicFact,
    RoleStrategy,
    build_resume_atomic_facts,
)

MAX_RESUME_FILE_BYTES = 5 * 1024 * 1024
MAX_RESUME_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_RESUME_UPLOAD_FILES = 3
_MAX_EXTRACTED_TEXT = 250_000
_MAX_PDF_DECOMPRESSED_BYTES = 8 * 1024 * 1024
_MAX_PDF_OBJECT_HEADER_BYTES = 64 * 1024
_DOCX_DOCUMENT = "word/document.xml"
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W = f"{{{_W_NS}}}"
_ALLOWED_SUFFIXES = {".docx", ".pdf", ".txt", ".md", ".html", ".htm"}
_SECTION_HEADINGS = {
    "SUMMARY",
    "PROJECT HIGHLIGHTS",
    "EDUCATION",
    "TECHNICAL SKILLS",
    "WORK EXPERIENCE",
}
_HEADING_ALIASES = {
    "SUMMARY": "SUMMARY",
    "PROFILE": "SUMMARY",
    "PROFESSIONAL SUMMARY": "SUMMARY",
    "PROJECTS": "PROJECT HIGHLIGHTS",
    "PROJECT HIGHLIGHTS": "PROJECT HIGHLIGHTS",
    "EDUCATION": "EDUCATION",
    "SKILLS": "TECHNICAL SKILLS",
    "TECHNICAL SKILLS": "TECHNICAL SKILLS",
    "EXPERIENCE": "WORK EXPERIENCE",
    "PROFESSIONAL EXPERIENCE": "WORK EXPERIENCE",
    "WORK EXPERIENCE": "WORK EXPERIENCE",
    "个人简介": "SUMMARY",
    "职业概述": "SUMMARY",
    "个人总结": "SUMMARY",
    "项目": "PROJECT HIGHLIGHTS",
    "项目经历": "PROJECT HIGHLIGHTS",
    "教育": "EDUCATION",
    "教育经历": "EDUCATION",
    "技能": "TECHNICAL SKILLS",
    "技术技能": "TECHNICAL SKILLS",
    "专业技能": "TECHNICAL SKILLS",
    "经历": "WORK EXPERIENCE",
    "工作经历": "WORK EXPERIENCE",
}
_PRIVATE_PATH_RE = re.compile(
    r"(?:(?:/Users|/home)/[^\s,;]+|[A-Za-z]:\\[^\r\n,;]+)", re.IGNORECASE
)
_PRIVATE_PROFILE_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:linkedin\.com/in|github\.com)/[A-Za-z0-9._%/~+-]+",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE_CANDIDATE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d().\s-]{6,}\d)(?!\w)")
_ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+(?:[A-Za-z0-9.'-]+\s+){0,6}"
    r"(?:Street|St|Road|Rd|Avenue|Ave|Lane|Ln|Drive|Dr|Boulevard|Blvd|Court|Ct|Way)\b"
    r"(?:[^\r\n;]{0,80})?",
    re.IGNORECASE,
)
_CHINESE_ADDRESS_RE = re.compile(
    r"[\u4e00-\u9fff0-9]{2,40}(?:省|市|区|县|镇|乡|街道|路|街|巷)"
    r"[\u4e00-\u9fff0-9号室栋单元-]{1,50}"
)
_PRIVATE_TOKEN_RE = re.compile(r"__SS_PRIVATE_[A-Z]+_\d{2}__")
_BULLET_PREFIX_RE = re.compile(r"^(?:[-*\u2022]|\d+[.)])\s+")
_SAFE_STYLE_IDS = {
    "bodytext": "BodyText",
    "heading1": "Heading1",
    "heading2": "Heading2",
    "heading3": "Heading3",
    "listparagraph": "ListParagraph",
    "normal": "Normal",
    "nospacing": "NoSpacing",
    "quote": "Quote",
    "subtitle": "Subtitle",
    "title": "Title",
}
_SAFE_FONT_FAMILIES = {
    value.casefold(): value
    for value in (
        "Aptos",
        "Arial",
        "Calibri",
        "Courier New",
        "Georgia",
        "Helvetica",
        "Tahoma",
        "Times New Roman",
        "Verdana",
    )
}
_SAFE_BULLET_STYLES = {"bullet", "numbered"}
_SAFE_SPACING_RE = re.compile(r"\d+(?:\.\d+)?(?:pt|in|cm|mm|px)?", re.IGNORECASE)


class ResumeUploadError(ValueError):
    """A selected file is outside the explicitly authorized upload boundary."""


class PDFExtractionTrace(ContractModel):
    """Body-free observability for the canonical PDF extraction policy."""

    primary_extractor: Literal["bounded_stream"] = "bounded_stream"
    primary_quality: Literal["USABLE", "UNRELIABLE"]
    fallback_used: bool
    fallback_extractor: Literal["pypdf"] | None = None
    fallback_quality: Literal["NOT_USED", "USABLE", "UNRELIABLE"]
    extracted_chars: int = Field(ge=0, le=_MAX_EXTRACTED_TEXT)
    replacement_chars: int = Field(ge=0, le=_MAX_EXTRACTED_TEXT)
    meaningful_line_ratio: float = Field(ge=0.0, le=1.0)
    source_parse_status: Literal["USABLE", "SOURCE_PARSE_UNRELIABLE_TEXT"]


class ResumeSourceParseError(ResumeUploadError):
    """The selected source cannot produce trustworthy natural-language text."""

    code: ClassVar[str] = "SOURCE_PARSE_UNRELIABLE_TEXT"

    def __init__(
        self,
        message: str,
        *,
        trace: PDFExtractionTrace | None = None,
    ) -> None:
        super().__init__(message)
        self.trace = trace


class ResumeUploadRole(StrEnum):
    RESUME = "resume"
    JOB_DESCRIPTION = "job_description"
    SUPPORT = "support"


class _VisibleHTMLTextParser(HTMLParser):
    """Extract bounded visible text while ignoring executable/hidden HTML bodies."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._blocked_depth = 0
        self._heading_tag: str | None = None
        self._heading_parts: list[str] = []
        self.headings: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        del attrs
        lowered = tag.casefold()
        if lowered in {"script", "style", "noscript", "template", "svg"}:
            self._blocked_depth += 1
        elif self._blocked_depth == 0 and lowered in {
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
        }:
            self._heading_tag = lowered
            self._heading_parts = []

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered in {"script", "style", "noscript", "template", "svg"}:
            self._blocked_depth = max(0, self._blocked_depth - 1)
            return
        if self._blocked_depth == 0 and lowered == self._heading_tag:
            heading = " ".join("".join(self._heading_parts).split())
            if heading:
                self.headings.append(heading)
            self._heading_tag = None
            self._heading_parts = []

    def handle_data(self, data: str) -> None:
        if self._blocked_depth:
            return
        value = " ".join(data.split())
        if value:
            self.parts.append(value)
            if self._heading_tag is not None:
                self._heading_parts.append(value)


def _text_template_metadata(
    source_format: Literal["txt", "md", "html"],
    text: str,
    *,
    heading_names: list[str] | None = None,
) -> ResumeTemplateMetadata:
    candidates = heading_names or [line.strip() for line in text.splitlines() if line.strip()]
    sections: list[str] = []
    accepted_headings: list[str] = []
    for candidate in candidates:
        canonical = _HEADING_ALIASES.get(" ".join(candidate.upper().rstrip(":：").split()))
        if canonical is not None and canonical not in sections:
            sections.append(canonical)
            accepted_headings.append(candidate)
    return ResumeTemplateMetadata(
        source_format=source_format,
        section_order=sections,
        heading_names=accepted_headings,
    )


@dataclass(frozen=True)
class SelectedResumeFile:
    """One file explicitly selected through the browser file picker."""

    role: ResumeUploadRole
    filename: str
    content_type: str
    content: bytes


class ResumeTemplateMetadata(ContractModel):
    """Strict metadata allowlist; private Office and filesystem metadata is absent."""

    source_format: Literal["docx", "pdf", "txt", "md", "html"]
    section_order: list[str] = Field(default_factory=list, max_length=20)
    heading_names: list[str] = Field(default_factory=list, max_length=30)
    style_ids: list[str] = Field(default_factory=list, max_length=40)
    font_families: list[str] = Field(default_factory=list, max_length=30)
    colors: list[str] = Field(default_factory=list, max_length=30)
    bullet_styles: list[str] = Field(default_factory=list, max_length=20)
    heading_spacing: list[str] = Field(default_factory=list, max_length=20)
    page_count: int | None = Field(default=None, ge=1, le=100)


class ExtractedResumeUpload(ContractModel):
    role: ResumeUploadRole
    source_format: Literal["docx", "pdf", "txt", "md", "html"]
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    text: str = Field(min_length=1, max_length=_MAX_EXTRACTED_TEXT)
    template_metadata: ResumeTemplateMetadata
    source_parse_trace: PDFExtractionTrace | None = None


class GatewayResumeEntry(ContractModel):
    profile_entry_id: str = Field(pattern=r"^PROFILE-\d{2}$")
    text: str = Field(min_length=1, max_length=1_500)


class GatewayCandidateProfile(ContractModel):
    summary: str | None = Field(default=None, max_length=4_000)
    skills: list[str] = Field(default_factory=list, max_length=80)
    entries: list[GatewayResumeEntry] = Field(min_length=1, max_length=80)
    atomic_facts: list[ResumeAtomicFact] = Field(min_length=1, max_length=960)


class GatewaySupportSummary(ContractModel):
    source_format: Literal["docx", "pdf", "txt", "md", "html"]
    summary: str = Field(min_length=1, max_length=4_000)


class GatewayPrivacyControls(ContractModel):
    zero_data_retention: Literal[True] = True
    disallow_prompt_training: Literal[True] = True
    provider_allowlist: list[str] = Field(min_length=1, max_length=8)
    direct_identifiers_removed: Literal[True] = True


class GatewayOutputContract(ContractModel):
    name: Literal["RoleStrategy"] = "RoleStrategy"
    structured_json_required: Literal[True] = True
    schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class GatewayPayload(ContractModel):
    """The only resume payload authorized to cross the model boundary."""

    feature_type: Literal["resume_tailoring"] = "resume_tailoring"
    request_id: str = Field(pattern=r"^resume-request-[a-f0-9]{24}$")
    output_locale: Literal["en-US", "zh-CN"] = "en-US"
    job_description: str = Field(min_length=1, max_length=_MAX_EXTRACTED_TEXT)
    tailoring_instructions: str = Field(default="", max_length=1_200)
    positioning_brief: JDPositioningBrief
    composition_evidence_plan: CompositionEvidencePlan
    candidate_profile: GatewayCandidateProfile
    support_context: list[GatewaySupportSummary] = Field(default_factory=list, max_length=1)
    template_metadata: ResumeTemplateMetadata
    privacy: GatewayPrivacyControls
    output_contract: GatewayOutputContract


@dataclass(frozen=True)
class PreparedGatewayPayload:
    payload: GatewayPayload
    private_replacements: tuple[tuple[str, str], ...]


class ResumeFunnelEventType(StrEnum):
    RESUME_UPLOAD_STARTED = "resume_upload_started"
    RESUME_UPLOAD_COMPLETED = "resume_upload_completed"
    JD_SUPPLIED = "jd_supplied"
    GENERATION_STARTED = "generation_started"
    GENERATION_COMPLETED = "generation_completed"
    PREVIEW_VIEWED = "preview_viewed"
    RESUME_EXPORTED = "resume_exported"
    UNLOCK_LOCAL_SCAN_CLICKED = "unlock_local_scan_clicked"


def _safe_suffix(filename: str) -> str:
    basename = filename.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    if not basename or basename in {".", ".."}:
        raise ResumeUploadError("The selected file has an invalid name")
    suffix = Path(basename).suffix.casefold()
    if suffix not in _ALLOWED_SUFFIXES:
        raise ResumeUploadError("Only PDF, DOCX, TXT, MD, HTML, and HTM files are accepted")
    return suffix


def _reject_disguised_file(content: bytes, suffix: str) -> None:
    executable_magic = (
        b"MZ",
        b"\x7fELF",
        b"\xcf\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xca\xfe\xba\xbe",
    )
    if content.startswith(executable_magic) or content.startswith(b"SQLite format 3\x00"):
        raise ResumeUploadError("Database and executable files are not accepted")
    if content.startswith(b"PK\x03\x04") and suffix != ".docx":
        raise ResumeUploadError("ZIP files are not accepted")
    if suffix == ".pdf" and not content.startswith(b"%PDF-"):
        raise ResumeUploadError("The selected PDF is not a readable PDF file")
    if suffix == ".docx" and not content.startswith(b"PK"):
        raise ResumeUploadError("The selected DOCX is not a readable Word file")
    if suffix in {".txt", ".md", ".html", ".htm"} and (
        content.startswith(b"PK") or content.startswith(b"%PDF-")
    ):
        raise ResumeUploadError("The selected text file has a mismatched file type")


def validate_selected_resume_files(files: list[SelectedResumeFile]) -> None:
    if not files:
        raise ResumeUploadError("Select a resume file")
    if len(files) > MAX_RESUME_UPLOAD_FILES:
        raise ResumeUploadError("Select no more than three files")
    if sum(len(item.content) for item in files) > MAX_RESUME_UPLOAD_BYTES:
        raise ResumeUploadError("Selected files must total 10 MB or less")
    roles = [item.role for item in files]
    if roles.count(ResumeUploadRole.RESUME) != 1:
        raise ResumeUploadError("Select exactly one resume file")
    if roles.count(ResumeUploadRole.JOB_DESCRIPTION) > 1 or roles.count(
        ResumeUploadRole.SUPPORT
    ) > 1:
        raise ResumeUploadError("Select at most one JD and one supporting file")
    for item in files:
        if not item.content:
            raise ResumeUploadError("Selected files must not be empty")
        if len(item.content) > MAX_RESUME_FILE_BYTES:
            raise ResumeUploadError("Each selected file must be 5 MB or smaller")
        suffix = _safe_suffix(item.filename)
        _reject_disguised_file(item.content, suffix)


def _docx_parts(content: bytes) -> tuple[bytes, bytes | None]:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            seen: set[str] = set()
            document: bytes | None = None
            styles: bytes | None = None
            expanded = 0
            for info in archive.infolist():
                path = PurePosixPath(info.filename)
                if path.is_absolute() or ".." in path.parts or info.filename in seen:
                    raise ResumeUploadError("DOCX contains an unsafe package path")
                if info.flag_bits & 0x1:
                    raise ResumeUploadError("Password-protected DOCX files are not accepted")
                seen.add(info.filename)
                expanded += info.file_size
                if expanded > 40 * 1024 * 1024:
                    raise ResumeUploadError("DOCX expands beyond the safety limit")
                if info.filename == _DOCX_DOCUMENT:
                    document = archive.read(info)
                elif info.filename == "word/styles.xml":
                    styles = archive.read(info)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ResumeUploadError("The selected DOCX is not readable") from exc
    if document is None:
        raise ResumeUploadError("DOCX is missing its visible document body")
    return document, styles


def _xml_root(content: bytes, label: str) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise ResumeUploadError(f"{label} XML is malformed") from exc


def _visible_docx_paragraph_text(paragraph: ElementTree.Element) -> str:
    values: list[str] = []
    for run in paragraph.iter(f"{_W}r"):
        properties = run.find(f"{_W}rPr")
        if properties is not None and (
            properties.find(f"{_W}vanish") is not None
            or properties.find(f"{_W}webHidden") is not None
        ):
            continue
        values.extend(node.text or "" for node in run.iter(f"{_W}t"))
    return "".join(values).strip()


def _docx_text_and_metadata(content: bytes) -> tuple[str, ResumeTemplateMetadata]:
    document, styles = _docx_parts(content)
    root = _xml_root(document, "DOCX document")
    paragraphs: list[str] = []
    section_order: list[str] = []
    heading_names: list[str] = []
    style_ids: list[str] = []
    bullet_styles: list[str] = []
    for paragraph in root.iter(f"{_W}p"):
        text = _visible_docx_paragraph_text(paragraph)
        if text:
            paragraphs.append(text)
            canonical = _HEADING_ALIASES.get(" ".join(text.upper().split()))
            if canonical is not None and canonical not in section_order:
                section_order.append(canonical)
                heading_names.append(text)
        properties = paragraph.find(f"{_W}pPr")
        if properties is None:
            continue
        style = properties.find(f"{_W}pStyle")
        if style is not None:
            value = style.get(f"{_W}val")
            if value and value not in style_ids:
                style_ids.append(value)
        numbering = properties.find(f"{_W}numPr")
        if numbering is not None and "numbered" not in bullet_styles:
            bullet_styles.append("numbered")
    fonts: list[str] = []
    colors: list[str] = []
    if styles is not None:
        try:
            styles_root = _xml_root(styles, "DOCX styles")
        except ResumeUploadError:
            styles_root = None
        if styles_root is not None:
            for font_node in styles_root.iter(f"{_W}rFonts"):
                for attribute in ("ascii", "hAnsi", "eastAsia", "cs"):
                    value = font_node.get(f"{_W}{attribute}")
                    if value and value not in fonts:
                        fonts.append(value)
            for color_node in styles_root.iter(f"{_W}color"):
                value = color_node.get(f"{_W}val")
                if value and re.fullmatch(r"[A-Fa-f0-9]{6}", value) and value not in colors:
                    colors.append(value.upper())
    text = "\n".join(paragraphs).strip()
    if not text:
        raise ResumeUploadError("The selected DOCX contains no extractable text")
    return text, ResumeTemplateMetadata(
        source_format="docx",
        section_order=section_order,
        heading_names=heading_names,
        style_ids=style_ids[:40],
        font_families=fonts[:30],
        colors=colors[:30],
        bullet_styles=bullet_styles,
    )


def _decode_pdf_literal(value: bytes) -> str:
    output = bytearray()
    index = 0
    escapes = {
        ord("n"): ord("\n"),
        ord("r"): ord("\r"),
        ord("t"): ord("\t"),
        ord("b"): ord("\b"),
        ord("f"): ord("\f"),
        ord("("): ord("("),
        ord(")"): ord(")"),
        ord("\\"): ord("\\"),
    }
    while index < len(value):
        byte = value[index]
        if byte != ord("\\") or index + 1 >= len(value):
            output.append(byte)
            index += 1
            continue
        index += 1
        escaped = value[index]
        if escaped in escapes:
            output.append(escapes[escaped])
            index += 1
            continue
        if ord("0") <= escaped <= ord("7"):
            end = index + 1
            while end < min(index + 3, len(value)) and ord("0") <= value[end] <= ord("7"):
                end += 1
            output.append(int(value[index:end], 8))
            index = end
            continue
        if escaped in {ord("\r"), ord("\n")}:
            if escaped == ord("\r") and index + 1 < len(value) and value[index + 1] == ord("\n"):
                index += 1
            index += 1
            continue
        output.append(escaped)
        index += 1
    if output.startswith(b"\xfe\xff"):
        return output[2:].decode("utf-16-be", errors="replace")
    return output.decode("utf-8", errors="replace")


def _pdf_stream_data(
    content: bytes,
    match: re.Match[bytes],
    dictionary: bytes,
) -> bytes:
    """Return exact stream bytes when a direct PDF /Length is available."""

    length_matches = list(
        re.finditer(rb"/Length\s+(-?\d+)(?:\s+(\d+)\s+R)?", dictionary)
    )
    if len(length_matches) != 1 or length_matches[0].group(2) is not None:
        return match.group(1)
    direct_length = int(length_matches[0].group(1))
    if direct_length < 0:
        raise ResumeUploadError("PDF stream has an invalid declared length")
    start = match.start(1)
    end = start + direct_length
    if end > len(content):
        raise ResumeUploadError("PDF stream has an invalid declared length")
    if re.match(rb"\r?\nendstream\b", content[end : end + 12]) is None:
        raise ResumeUploadError("PDF stream has an invalid declared boundary")
    return content[start:end]


def _pdf_stream_dictionary(content: bytes, match: re.Match[bytes]) -> bytes:
    """Return the bounded dictionary/header for the stream's current PDF object."""

    stream_start = match.start()
    lower_bound = max(0, stream_start - _MAX_PDF_OBJECT_HEADER_BYTES)
    previous_endobj = content.rfind(b"endobj", lower_bound, stream_start)
    object_start = (
        previous_endobj + len(b"endobj")
        if previous_endobj >= 0
        else lower_bound
    )
    return content[object_start:stream_start]


@dataclass(frozen=True)
class _PDFTextQuality:
    usable: bool
    extracted_chars: int
    replacement_chars: int
    meaningful_line_ratio: float


class _PDFFallbackExtractionError(Exception):
    """The bounded fallback could not recover a complete text result."""


def _primary_pdf_text(content: bytes) -> tuple[str, int | None]:
    if b"/Encrypt" in content:
        raise ResumeUploadError("Password-protected PDF files are not accepted")
    page_count = len(re.findall(rb"/Type\s*/Page\b", content)) or None
    segments: list[bytes] = []
    extracted_bytes = 0
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", content, re.DOTALL):
        dictionary = _pdf_stream_dictionary(content, match)
        stream = _pdf_stream_data(content, match, dictionary)
        if b"/FlateDecode" in dictionary:
            try:
                decompressor = zlib.decompressobj()
                remaining = _MAX_PDF_DECOMPRESSED_BYTES - extracted_bytes
                stream = decompressor.decompress(stream, remaining + 1)
            except zlib.error:
                continue
            if (
                len(stream) > remaining
                or decompressor.unconsumed_tail
                or not decompressor.eof
            ):
                raise ResumeUploadError("PDF content expands beyond the safety limit")
        extracted_bytes += len(stream)
        if extracted_bytes > _MAX_PDF_DECOMPRESSED_BYTES:
            raise ResumeUploadError("PDF content expands beyond the safety limit")
        segments.append(stream)
    if not segments:
        raise ResumeUploadError(
            "The PDF has no extractable text; image-only PDFs and OCR are not supported"
        )
    values: list[str] = []
    for stream in segments:
        literals = re.findall(
            rb"\(((?:\\.|[^\\)])*)\)\s*Tj\b", stream, re.DOTALL
        )
        for array in re.findall(rb"\[(.*?)\]\s*TJ\b", stream, re.DOTALL):
            literals.extend(re.findall(rb"\(((?:\\.|[^\\)])*)\)", array, re.DOTALL))
        for literal in literals:
            text = _decode_pdf_literal(literal).strip()
            if text:
                values.append(text)
        for hexadecimal in re.findall(rb"<([A-Fa-f0-9\s]{4,})>\s*Tj", stream):
            try:
                raw = bytes.fromhex(re.sub(rb"\s+", b"", hexadecimal).decode("ascii"))
            except (ValueError, UnicodeDecodeError):
                continue
            text = (
                raw[2:].decode("utf-16-be", errors="replace")
                if raw.startswith(b"\xfe\xff")
                else raw.decode("utf-8", errors="replace")
            ).strip()
            if text:
                values.append(text)
    text = "\n".join(values).strip()
    if not text:
        raise ResumeUploadError(
            "The PDF has no extractable text; image-only PDFs and OCR are not supported"
        )
    if len(text) > _MAX_EXTRACTED_TEXT:
        raise ResumeUploadError("Extracted text exceeds the bounded processing limit")
    return text, page_count


def _pdf_text_quality(text: str) -> _PDFTextQuality:
    """Evaluate every PDF extractor with the same canonical quality contract."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    characters = [character for character in text if not character.isspace()]
    if not lines or not characters:
        return _PDFTextQuality(
            usable=False,
            extracted_chars=len(text),
            replacement_chars=text.count("\ufffd"),
            meaningful_line_ratio=0.0,
        )

    replacement_chars = text.count("\ufffd")
    replacement_ratio = replacement_chars / len(characters)
    printable_ratio = sum(character.isprintable() for character in characters) / len(
        characters
    )
    textual_ratio = sum(character.isalnum() for character in characters) / len(
        characters
    )
    meaningful_lines = sum(
        len(line) >= 3 and sum(character.isalnum() for character in line) >= 2
        for line in lines
    )
    short_line_ratio = sum(len(line) <= 2 for line in lines) / len(lines)
    wordlike_sequences = re.findall(r"[^\W_]{3,}", text, flags=re.UNICODE)

    fragmented = (
        len(lines) >= 4
        and short_line_ratio >= 0.90
        and meaningful_lines == 0
    )
    replacement_corruption = replacement_ratio >= 0.08
    nontextual = printable_ratio < 0.90 or textual_ratio < 0.35
    no_natural_language = meaningful_lines == 0 and not wordlike_sequences

    usable = not (
        (fragmented and (replacement_corruption or no_natural_language))
        or (replacement_corruption and nontextual)
        or (len(lines) >= 4 and nontextual and no_natural_language)
    )
    return _PDFTextQuality(
        usable=usable,
        extracted_chars=len(text),
        replacement_chars=replacement_chars,
        meaningful_line_ratio=meaningful_lines / len(lines),
    )


def _pdf_extracted_text_is_usable(text: str) -> bool:
    """Reject clear glyph-fragment output before it becomes Resume structure."""

    return _pdf_text_quality(text).usable


def _pypdf_text(content: bytes) -> tuple[str, int]:
    """Recover one bounded complete extraction with pypdf; never merge outputs."""

    try:
        reader = PdfReader(
            io.BytesIO(content),
            strict=True,
            root_object_recovery_limit=1_000,
        )
        if reader.is_encrypted:
            raise ResumeUploadError("Password-protected PDF files are not accepted")
        page_count = len(reader.pages)
        if page_count == 0:
            raise _PDFFallbackExtractionError("PDF contains no pages")
        if page_count > 100:
            raise ResumeUploadError("PDF page count exceeds the bounded processing limit")

        values: list[str] = []
        decoded_bytes = 0
        extracted_chars = 0
        for page in reader.pages:
            contents = page.get_contents()
            if contents is not None:
                decoded_bytes += len(contents.get_data())
                if decoded_bytes > _MAX_PDF_DECOMPRESSED_BYTES:
                    raise ResumeUploadError(
                        "PDF content expands beyond the safety limit"
                    )
            page_text = page.extract_text() or ""
            extracted_chars += len(page_text)
            if extracted_chars > _MAX_EXTRACTED_TEXT:
                raise ResumeUploadError(
                    "Extracted text exceeds the bounded processing limit"
                )
            if page_text.strip():
                values.append(page_text.strip())
    except ResumeUploadError:
        raise
    except Exception as exc:
        raise _PDFFallbackExtractionError from exc

    text = "\n".join(values).strip()
    if not text:
        raise _PDFFallbackExtractionError("PDF contains no extractable text")
    return text, page_count


def _pdf_extraction_trace(
    *,
    primary_quality: _PDFTextQuality,
    fallback_used: bool,
    fallback_quality: _PDFTextQuality | None,
    source_parse_status: Literal["USABLE", "SOURCE_PARSE_UNRELIABLE_TEXT"],
) -> PDFExtractionTrace:
    selected_quality = fallback_quality or primary_quality
    return PDFExtractionTrace(
        primary_quality="USABLE" if primary_quality.usable else "UNRELIABLE",
        fallback_used=fallback_used,
        fallback_extractor="pypdf" if fallback_used else None,
        fallback_quality=(
            "USABLE"
            if fallback_quality is not None and fallback_quality.usable
            else "UNRELIABLE"
            if fallback_used
            else "NOT_USED"
        ),
        extracted_chars=selected_quality.extracted_chars,
        replacement_chars=selected_quality.replacement_chars,
        meaningful_line_ratio=selected_quality.meaningful_line_ratio,
        source_parse_status=source_parse_status,
    )


def _pdf_text(content: bytes) -> tuple[str, int | None, PDFExtractionTrace]:
    primary_text, primary_page_count = _primary_pdf_text(content)
    primary_quality = _pdf_text_quality(primary_text)
    if primary_quality.usable:
        return (
            primary_text,
            primary_page_count,
            _pdf_extraction_trace(
                primary_quality=primary_quality,
                fallback_used=False,
                fallback_quality=None,
                source_parse_status="USABLE",
            ),
        )

    fallback_text: str | None = None
    fallback_page_count: int | None = None
    fallback_quality: _PDFTextQuality | None = None
    try:
        fallback_text, fallback_page_count = _pypdf_text(content)
        fallback_quality = _pdf_text_quality(fallback_text)
    except _PDFFallbackExtractionError:
        pass

    if (
        fallback_text is not None
        and fallback_page_count is not None
        and fallback_quality is not None
        and fallback_quality.usable
    ):
        return (
            fallback_text,
            fallback_page_count,
            _pdf_extraction_trace(
                primary_quality=primary_quality,
                fallback_used=True,
                fallback_quality=fallback_quality,
                source_parse_status="USABLE",
            ),
        )

    trace = _pdf_extraction_trace(
        primary_quality=primary_quality,
        fallback_used=True,
        fallback_quality=fallback_quality,
        source_parse_status="SOURCE_PARSE_UNRELIABLE_TEXT",
    )
    raise ResumeSourceParseError(
        "无法可靠读取此 PDF 中的文字。请改用 DOCX，或使用可复制文本的 PDF。",
        trace=trace,
    )


def extract_selected_resume_file(item: SelectedResumeFile) -> ExtractedResumeUpload:
    validate_selected_resume_files([item]) if item.role is ResumeUploadRole.RESUME else None
    suffix = _safe_suffix(item.filename)
    _reject_disguised_file(item.content, suffix)
    if len(item.content) > MAX_RESUME_FILE_BYTES:
        raise ResumeUploadError("Each selected file must be 5 MB or smaller")
    source_parse_trace: PDFExtractionTrace | None = None
    if suffix == ".docx":
        text, metadata = _docx_text_and_metadata(item.content)
        source_format: Literal["docx", "pdf", "txt", "md", "html"] = "docx"
    elif suffix == ".pdf":
        text, page_count, source_parse_trace = _pdf_text(item.content)
        source_format = "pdf"
        metadata = ResumeTemplateMetadata(source_format="pdf", page_count=page_count)
    else:
        if b"\x00" in item.content:
            raise ResumeUploadError("The selected text file is not valid UTF-8 text")
        try:
            decoded = item.content.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise ResumeUploadError("The selected text file is not valid UTF-8 text") from exc
        source_format = (
            "html" if suffix in {".html", ".htm"} else "md" if suffix == ".md" else "txt"
        )
        if source_format == "html":
            parser = _VisibleHTMLTextParser()
            try:
                parser.feed(decoded)
                parser.close()
            except (AssertionError, ValueError) as exc:
                raise ResumeUploadError("The selected HTML file is malformed") from exc
            text = "\n".join(parser.parts).strip()
            metadata = _text_template_metadata(
                "html", text, heading_names=parser.headings
            )
        else:
            text = decoded
            metadata = _text_template_metadata(source_format, text)
    text = text.strip()
    if not text:
        raise ResumeUploadError("The selected file contains no extractable text")
    if len(text) > _MAX_EXTRACTED_TEXT:
        raise ResumeUploadError("Extracted text exceeds the bounded processing limit")
    return ExtractedResumeUpload(
        role=item.role,
        source_format=source_format,
        content_sha256=hashlib.sha256(item.content).hexdigest(),
        text=text,
        template_metadata=metadata,
        source_parse_trace=source_parse_trace,
    )


def extract_selected_resume_files(
    files: list[SelectedResumeFile],
) -> dict[ResumeUploadRole, ExtractedResumeUpload]:
    validate_selected_resume_files(files)
    return {item.role: extract_selected_resume_file(item) for item in files}


def _canonical_heading(line: str) -> str | None:
    return _HEADING_ALIASES.get(" ".join(line.upper().rstrip(":").split()))


def _xml_10_safe_text(text: str) -> str:
    """Remove control characters that XML 1.0 cannot represent."""

    return "".join(
        character
        for character in text
        if (
            ord(character) in {0x09, 0x0A, 0x0D}
            or 0x20 <= ord(character) <= 0xD7FF
            or 0xE000 <= ord(character) <= 0xFFFD
            or 0x10000 <= ord(character) <= 0x10FFFF
        )
    )


def normalize_text_resume_to_docx(text: str) -> bytes:
    """Create a minimal valid DOCX when the explicitly selected resume is not DOCX."""

    text = _xml_10_safe_text(text)
    raw_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not raw_lines:
        raise ResumeUploadError("The selected resume contains no text")
    has_heading = any(_canonical_heading(line) is not None for line in raw_lines)
    if not has_heading:
        identity = raw_lines[:2]
        summary = raw_lines[2:3]
        remainder = raw_lines[3:] or raw_lines[2:]
        raw_lines = [
            *identity,
            "SUMMARY",
            *summary,
            "PROJECT HIGHLIGHTS",
            "EDUCATION",
            "TECHNICAL SKILLS",
            "WORK EXPERIENCE",
            *(f"- {line}" for line in remainder),
        ]

    normalized: list[tuple[str, bool, bool]] = []
    current_section: str | None = None
    saw_claim_bullet = False
    for raw_line in raw_lines:
        heading = _canonical_heading(raw_line)
        if heading is not None:
            current_section = heading
            normalized.append((heading, False, True))
            continue
        is_explicit_bullet = _BULLET_PREFIX_RE.match(raw_line) is not None
        line = _BULLET_PREFIX_RE.sub("", raw_line).strip()
        bullet = is_explicit_bullet or (
            current_section in {"TECHNICAL SKILLS"} and bool(line)
        )
        if bullet and current_section in {"PROJECT HIGHLIGHTS", "WORK EXPERIENCE"}:
            saw_claim_bullet = True
        normalized.append((line, bullet, False))
    if not saw_claim_bullet:
        for index in range(len(normalized) - 1, -1, -1):
            line, _bullet, is_heading = normalized[index]
            if line and not is_heading:
                normalized[index] = (line, True, False)
                break

    document_root = ElementTree.Element(f"{_W}document")
    body = ElementTree.SubElement(document_root, f"{_W}body")
    for line, bullet, is_heading in normalized:
        paragraph = ElementTree.SubElement(body, f"{_W}p")
        if bullet or is_heading:
            properties = ElementTree.SubElement(paragraph, f"{_W}pPr")
            if is_heading:
                style = ElementTree.SubElement(properties, f"{_W}pStyle")
                style.set(f"{_W}val", "Heading1")
            if bullet:
                numbering = ElementTree.SubElement(properties, f"{_W}numPr")
                level = ElementTree.SubElement(numbering, f"{_W}ilvl")
                level.set(f"{_W}val", "0")
                number_id = ElementTree.SubElement(numbering, f"{_W}numId")
                number_id.set(f"{_W}val", "1")
        run = ElementTree.SubElement(paragraph, f"{_W}r")
        text_node = ElementTree.SubElement(run, f"{_W}t")
        text_node.text = line
    ElementTree.SubElement(body, f"{_W}sectPr")
    document = ElementTree.tostring(document_root, encoding="utf-8", xml_declaration=True)
    content_types = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Default Extension="rels" '
        b'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        b'<Default Extension="xml" ContentType="application/xml"/>'
        b'<Override PartName="/word/document.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.'
        b'document.main+xml"/>'
        b'<Override PartName="/word/styles.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.'
        b'styles+xml"/>'
        b'</Types>'
    )
    package_rels = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<Relationships '
        b'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" '
        b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        b'officeDocument" Target="word/document.xml"/>'
        b'</Relationships>'
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<w:styles xmlns:w="{_W_NS}">'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
        '<w:name w:val="Normal"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading1">'
        '<w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>'
        '</w:style></w:styles>'
    ).encode()
    document_rels = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<Relationships '
        b'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" '
        b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        b'Target="styles.xml"/></Relationships>'
    )
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", package_rels)
        archive.writestr("word/document.xml", document)
        archive.writestr("word/styles.xml", styles)
        archive.writestr("word/_rels/document.xml.rels", document_rels)
    return target.getvalue()


class _DirectIdentifierSanitizer:
    def __init__(self) -> None:
        self._private_by_token: dict[str, str] = {}

    def _token(self, label: str, private_value: str) -> str:
        existing = next(
            (token for token, value in self._private_by_token.items() if value == private_value),
            None,
        )
        if existing is not None:
            return existing
        token = f"__SS_PRIVATE_{label}_{len(self._private_by_token) + 1:02d}__"
        self._private_by_token[token] = private_value
        return token

    def _replace_matches(self, text: str, pattern: re.Pattern[str], label: str) -> str:
        return pattern.sub(lambda match: self._token(label, match.group(0)), text)

    def sanitize(self, text: str, *, known_name: str | None = None) -> str:
        sanitized = text
        if known_name and known_name.strip():
            name = known_name.strip()
            sanitized = re.sub(
                rf"(?<!\w){re.escape(name)}(?!\w)",
                lambda match: self._token("NAME", match.group(0)),
                sanitized,
                flags=re.IGNORECASE,
            )
        sanitized = self._replace_matches(sanitized, _PRIVATE_PATH_RE, "PATH")
        sanitized = self._replace_matches(sanitized, _PRIVATE_PROFILE_URL_RE, "PROFILE")
        sanitized = self._replace_matches(sanitized, _EMAIL_RE, "EMAIL")

        def replace_phone(match: re.Match[str]) -> str:
            digits = re.sub(r"\D", "", match.group(0))
            return (
                self._token("PHONE", match.group(0))
                if 8 <= len(digits) <= 15
                else match.group(0)
            )

        sanitized = _PHONE_CANDIDATE_RE.sub(replace_phone, sanitized)
        sanitized = self._replace_matches(sanitized, _ADDRESS_RE, "ADDRESS")
        return self._replace_matches(sanitized, _CHINESE_ADDRESS_RE, "ADDRESS")

    @property
    def replacements(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._private_by_token.items())


def _support_summary(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    if not lines:
        raise ResumeUploadError("The supporting file contains no usable text")
    summary = "\n".join(lines[:20])
    return summary[:4_000].rstrip()


def _safe_template_metadata(
    metadata: ResumeTemplateMetadata,
) -> ResumeTemplateMetadata:
    """Project parsed layout data onto a compact, identifier-free allowlist."""

    section_order = [
        value
        for value in metadata.section_order
        if value in _SECTION_HEADINGS
    ][:20]
    heading_names: list[str] = []
    for value in metadata.heading_names:
        canonical = _HEADING_ALIASES.get(" ".join(value.upper().split()))
        if canonical is not None and canonical not in heading_names:
            heading_names.append(canonical)
    style_ids = [
        _SAFE_STYLE_IDS[value.casefold()]
        for value in metadata.style_ids
        if value.casefold() in _SAFE_STYLE_IDS
    ][:40]
    font_families = [
        _SAFE_FONT_FAMILIES[value.casefold()]
        for value in metadata.font_families
        if value.casefold() in _SAFE_FONT_FAMILIES
    ][:30]
    colors = [
        value.upper()
        for value in metadata.colors
        if re.fullmatch(r"[A-Fa-f0-9]{6}", value)
    ][:30]
    bullet_styles = [
        value
        for value in metadata.bullet_styles
        if value in _SAFE_BULLET_STYLES
    ][:20]
    heading_spacing = [
        value
        for value in metadata.heading_spacing
        if len(value) <= 16 and _SAFE_SPACING_RE.fullmatch(value)
    ][:20]
    return ResumeTemplateMetadata(
        source_format=metadata.source_format,
        section_order=section_order,
        heading_names=heading_names[:30],
        style_ids=style_ids,
        font_families=font_families,
        colors=colors,
        bullet_styles=bullet_styles,
        heading_spacing=heading_spacing,
        page_count=metadata.page_count,
    )


def prepare_resume_gateway_payload(
    *,
    profile: CandidateProfile,
    job_description: str,
    tailoring_instructions: str,
    template_metadata: ResumeTemplateMetadata,
    support_upload: ExtractedResumeUpload | None = None,
    provider_allowlist: tuple[str, ...] = ("zai",),
    request_id: str | None = None,
    output_schema: type[BaseModel] = RoleStrategy,
    candidate_evidence_pack: CandidateEvidencePack | None = None,
    output_locale: Literal["en-US", "zh-CN"] = "en-US",
) -> PreparedGatewayPayload:
    """Build the sole typed payload after deterministic direct-identifier removal."""

    entries = profile.experience_bullets + profile.project_bullets
    if not entries:
        raise ResumeUploadError("The selected resume has no supported experience bullets")
    sanitizer = _DirectIdentifierSanitizer()
    known_name = profile.full_name
    sanitized_job = sanitizer.sanitize(job_description, known_name=known_name).strip()
    sanitized_instructions = sanitizer.sanitize(
        tailoring_instructions, known_name=known_name
    ).strip()
    sanitized_summary = (
        sanitizer.sanitize(profile.summary, known_name=known_name)
        if profile.summary
        else None
    )
    sanitized_skills = [
        sanitizer.sanitize(value, known_name=known_name) for value in profile.skills
    ]
    sanitized_entries = [
        GatewayResumeEntry(
            profile_entry_id=f"PROFILE-{index:02d}",
            text=sanitizer.sanitize(value, known_name=known_name),
        )
        for index, value in enumerate(entries, start=1)
    ]
    sanitized_profile = CandidateProfile(
        summary=sanitized_summary,
        skills=sanitized_skills,
        experience_bullets=[
            item.text for item in sanitized_entries[: len(profile.experience_bullets)]
        ],
        project_bullets=[
            item.text for item in sanitized_entries[len(profile.experience_bullets) :]
        ],
    )
    sanitized_profile_facts = {
        fact.fact_id: fact for fact in build_resume_atomic_facts(sanitized_profile)
    }
    source_facts = (
        candidate_evidence_pack.atomic_facts
        if candidate_evidence_pack is not None
        else build_resume_atomic_facts(profile)
    )
    sanitized_facts: list[ResumeAtomicFact] = []
    for fact in source_facts:
        if fact.source_kind == "PROFILE_ENTRY":
            sanitized_facts.append(sanitized_profile_facts[fact.fact_id])
            continue
        sanitized_text = sanitizer.sanitize(fact.text, known_name=known_name)
        sanitized_facts.append(
            ResumeAtomicFact(
                fact_id=fact.fact_id,
                profile_entry_id=fact.profile_entry_id,
                evidence_id=fact.evidence_id,
                source_kind=fact.source_kind,
                project=(
                    sanitizer.sanitize(fact.project, known_name=known_name)
                    if fact.project
                    else None
                ),
                capability_tags=fact.capability_tags,
                metric=(
                    sanitizer.sanitize(fact.metric, known_name=known_name)
                    if fact.metric
                    else None
                ),
                allowed_numbers=fact.allowed_numbers,
                source_refs=fact.source_refs,
                text=sanitized_text,
                source_sha256=fact.source_sha256,
                fact_sha256=hashlib.sha256(
                    f"{fact.fact_id}\0{fact.profile_entry_id}\0{sanitized_text}".encode()
                ).hexdigest(),
            )
        )
    support_context: list[GatewaySupportSummary] = []
    if support_upload is not None:
        support_context.append(
            GatewaySupportSummary(
                source_format=support_upload.source_format,
                summary=sanitizer.sanitize(
                    _support_summary(support_upload.text), known_name=known_name
                ),
            )
        )
    schema = output_schema.model_json_schema()
    schema_sha256 = hashlib.sha256(
        json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload = GatewayPayload(
        request_id=request_id or f"resume-request-{secrets.token_hex(12)}",
        output_locale=output_locale,
        job_description=sanitized_job,
        tailoring_instructions=sanitized_instructions,
        positioning_brief=build_jd_positioning_brief(
            sanitized_job, sanitized_facts
        ),
        composition_evidence_plan=build_composition_evidence_plan(
            sanitized_job, sanitized_facts
        ),
        candidate_profile=GatewayCandidateProfile(
            summary=sanitized_summary,
            skills=sanitized_skills,
            entries=sanitized_entries,
            atomic_facts=sanitized_facts,
        ),
        support_context=support_context,
        template_metadata=_safe_template_metadata(template_metadata),
        privacy=GatewayPrivacyControls(provider_allowlist=list(provider_allowlist)),
        output_contract=GatewayOutputContract(schema_sha256=schema_sha256),
    )
    return PreparedGatewayPayload(
        payload=payload,
        private_replacements=sanitizer.replacements,
    )


def restore_role_strategy(
    strategy: RoleStrategy,
    replacements: tuple[tuple[str, str], ...],
) -> RoleStrategy:
    """Reinsert only identifiers that were deterministically removed before the call."""

    payload = strategy.model_dump(mode="json")

    def restore(value: object) -> object:
        if isinstance(value, str):
            restored = value
            for token, private_value in replacements:
                restored = restored.replace(token, private_value)
            return restored
        if isinstance(value, list):
            return [restore(item) for item in value]
        if isinstance(value, dict):
            return {key: restore(item) for key, item in value.items()}
        return value

    return RoleStrategy.model_validate(restore(payload))


def validate_role_strategy_placeholders(
    strategy: RoleStrategy,
    prepared: PreparedGatewayPayload,
) -> None:
    """Require exact placeholder preservation before any private value is reinserted."""

    allowed_tokens = {token for token, _value in prepared.private_replacements}
    serialized = strategy.model_dump_json()
    returned_tokens = set(_PRIVATE_TOKEN_RE.findall(serialized))
    if returned_tokens - allowed_tokens:
        raise ResumeUploadError("Model output contains an unknown private placeholder")
    fact_by_id = {
        item.fact_id: item for item in prepared.payload.candidate_profile.atomic_facts
    }
    for rewrite in strategy.bullet_rewrites:
        source_tokens = set(
            _PRIVATE_TOKEN_RE.findall(
                " ".join(
                    fact_by_id[fact_id].text
                    for fact_id in rewrite.source_fact_ids
                    if fact_id in fact_by_id
                )
            )
        )
        rewrite_tokens = set(_PRIVATE_TOKEN_RE.findall(rewrite.text))
        if not rewrite_tokens <= source_tokens:
            raise ResumeUploadError(
                "Model output did not preserve direct-identifier placeholders"
            )
    if strategy.summary_rewrite is not None:
        summary_source_tokens = set(
            _PRIVATE_TOKEN_RE.findall(
                " ".join(
                    fact_by_id[fact_id].text
                    for fact_id in strategy.summary_rewrite.source_fact_ids
                    if fact_id in fact_by_id
                )
            )
        )
        summary_tokens = set(_PRIVATE_TOKEN_RE.findall(strategy.summary_rewrite.text))
        if not summary_tokens <= summary_source_tokens:
            raise ResumeUploadError(
                "Model output did not preserve direct-identifier placeholders"
            )


def record_resume_funnel_event(
    data_root: Path,
    event_type: ResumeFunnelEventType,
    *,
    run_id: str | None = None,
) -> Path:
    """Persist body-free local funnel metadata as one private write-once event."""

    events_root = data_root / "analytics" / "resume-funnel"
    events_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(events_root, 0o700)
    event_id = f"event-{secrets.token_hex(12)}"
    target = events_root / f"{event_id}.json"
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "event_id": event_id,
        "event_type": event_type.value,
        "occurred_at": utc_now().isoformat(),
    }
    if run_id is not None:
        payload["run_id"] = run_id
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
    return target
