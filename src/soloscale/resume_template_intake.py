"""Deterministic Resume layout-template intake with no claim ingestion.

Template sources contribute only section order and presentation metadata.  Visible
template copy is never returned from this module and therefore cannot become a
candidate fact by accident.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from enum import StrEnum
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field

from soloscale.models import ContractModel
from soloscale.resume_gateway_boundary import (
    MAX_RESUME_FILE_BYTES,
    ResumeTemplateMetadata,
    ResumeUploadError,
    ResumeUploadRole,
    SelectedResumeFile,
    extract_selected_resume_file,
)

_MAX_TEMPLATE_HTML_BYTES = 1 * 1024 * 1024
_MAX_TEMPLATE_REDIRECTS = 3
_PREVIEW_ID_PREFIX = "resume-template-"
_PREVIEW_ID_LENGTH = 24
_SECTION_ALIASES = {
    "SUMMARY": "SUMMARY",
    "PROFILE": "SUMMARY",
    "PROFESSIONAL SUMMARY": "SUMMARY",
    "个人简介": "SUMMARY",
    "职业概述": "SUMMARY",
    "个人总结": "SUMMARY",
    "PROJECTS": "PROJECT HIGHLIGHTS",
    "PROJECT HIGHLIGHTS": "PROJECT HIGHLIGHTS",
    "项目": "PROJECT HIGHLIGHTS",
    "项目经历": "PROJECT HIGHLIGHTS",
    "EDUCATION": "EDUCATION",
    "教育": "EDUCATION",
    "教育经历": "EDUCATION",
    "SKILLS": "TECHNICAL SKILLS",
    "TECHNICAL SKILLS": "TECHNICAL SKILLS",
    "技能": "TECHNICAL SKILLS",
    "技术技能": "TECHNICAL SKILLS",
    "专业技能": "TECHNICAL SKILLS",
    "EXPERIENCE": "WORK EXPERIENCE",
    "PROFESSIONAL EXPERIENCE": "WORK EXPERIENCE",
    "WORK EXPERIENCE": "WORK EXPERIENCE",
    "经历": "WORK EXPERIENCE",
    "工作经历": "WORK EXPERIENCE",
}


class ResumeTemplateSourceType(StrEnum):
    FILE = "FILE"
    HTML = "HTML"
    URL = "URL"


class ResumeTemplateReceipt(ContractModel):
    """Body-free provenance for one operator-confirmed layout template."""

    schema_version: Literal["0.1"] = "0.1"
    preview_id: str = Field(pattern=r"^resume-template-[a-f0-9]{24}$")
    source_type: ResumeTemplateSourceType
    source_format: Literal["docx", "pdf", "txt", "md", "html"]
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_url: str | None = Field(default=None, max_length=2_000)
    detected_sections: list[str] = Field(default_factory=list, max_length=20)
    heading_names: list[str] = Field(default_factory=list, max_length=30)
    layout: Literal["single_column"] = "single_column"
    detected_language: Literal["en-US", "zh-CN", "mixed", "unknown"] = "unknown"
    retrieved_at: str
    source_text_retained: Literal[False] = False
    candidate_facts_imported: Literal[False] = False


class _TemplateHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._blocked_depth = 0
        self._heading_tag: str | None = None
        self._heading_parts: list[str] = []
        self.headings: list[str] = []
        self.visible_character_count = 0

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
                self.headings.append(heading[:200])
            self._heading_tag = None
            self._heading_parts = []

    def handle_data(self, data: str) -> None:
        if self._blocked_depth:
            return
        self.visible_character_count += len(data.strip())
        if self._heading_tag is not None:
            self._heading_parts.append(data)


def _canonical_heading(value: str) -> str | None:
    normalized = " ".join(value.strip().rstrip(":：").upper().split())
    return _SECTION_ALIASES.get(normalized)


def _deduplicated_sections(headings: list[str]) -> list[str]:
    sections: list[str] = []
    for heading in headings:
        canonical = _canonical_heading(heading)
        if canonical is not None and canonical not in sections:
            sections.append(canonical)
    return sections


def detect_resume_language(
    values: list[str],
) -> Literal["en-US", "zh-CN", "mixed", "unknown"]:
    """Classify source text for trace/display only, never output eligibility."""

    joined = " ".join(values)
    has_zh = any("\u4e00" <= character <= "\u9fff" for character in joined)
    has_en = any(character.isascii() and character.isalpha() for character in joined)
    if has_zh and has_en:
        return "mixed"
    if has_zh:
        return "zh-CN"
    if has_en:
        return "en-US"
    return "unknown"


def _receipt(
    *,
    source_type: ResumeTemplateSourceType,
    source_format: Literal["docx", "pdf", "txt", "md", "html"],
    source_sha256: str,
    headings: list[str],
    source_url: str | None = None,
) -> ResumeTemplateReceipt:
    sections = _deduplicated_sections(headings)
    if not sections:
        raise ResumeUploadError(
            "The selected template has no recognizable Resume section headings"
        )
    if source_format == "pdf" and len(sections) < 3:
        raise ResumeUploadError("UNSUPPORTED_TEMPLATE_STRUCTURE")
    return ResumeTemplateReceipt(
        preview_id=f"{_PREVIEW_ID_PREFIX}{uuid4().hex[:_PREVIEW_ID_LENGTH]}",
        source_type=source_type,
        source_format=source_format,
        source_sha256=source_sha256,
        source_url=source_url,
        detected_sections=sections,
        heading_names=headings[:30],
        detected_language=detect_resume_language(headings),
        retrieved_at=datetime.now(UTC).isoformat(),
    )


def inspect_template_file(filename: str, content: bytes) -> ResumeTemplateReceipt:
    """Read only structural metadata from one explicitly selected template file."""

    suffix = Path(filename.replace("\\", "/")).suffix.casefold()
    if suffix in {".html", ".htm"}:
        return inspect_template_html(content, source_type=ResumeTemplateSourceType.FILE)
    if suffix not in {".docx", ".pdf", ".txt", ".md"}:
        raise ResumeUploadError("Template files must be DOCX, HTML, HTM, PDF, TXT, or MD")
    if not content or len(content) > MAX_RESUME_FILE_BYTES:
        raise ResumeUploadError("Template files must be non-empty and 5 MB or smaller")
    extracted = extract_selected_resume_file(
        SelectedResumeFile(
            role=ResumeUploadRole.RESUME,
            filename=filename,
            content_type="application/octet-stream",
            content=content,
        )
    )
    headings = list(extracted.template_metadata.heading_names)
    if not headings:
        headings = [line.strip() for line in extracted.text.splitlines() if line.strip()]
    return _receipt(
        source_type=ResumeTemplateSourceType.FILE,
        source_format=extracted.source_format,
        source_sha256=extracted.content_sha256,
        headings=headings,
    )


def inspect_template_html(
    content: bytes | str,
    *,
    source_type: ResumeTemplateSourceType = ResumeTemplateSourceType.HTML,
    source_url: str | None = None,
) -> ResumeTemplateReceipt:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    if not raw or len(raw) > _MAX_TEMPLATE_HTML_BYTES:
        raise ResumeUploadError("Template HTML must be non-empty and 1 MB or smaller")
    try:
        markup = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ResumeUploadError("Template HTML must be valid UTF-8") from exc
    parser = _TemplateHTMLParser()
    try:
        parser.feed(markup)
        parser.close()
    except (AssertionError, ValueError) as exc:
        raise ResumeUploadError("Template HTML is malformed") from exc
    if parser.visible_character_count > 250_000:
        raise ResumeUploadError("Template HTML exceeds the bounded processing limit")
    return _receipt(
        source_type=source_type,
        source_format="html",
        source_sha256=hashlib.sha256(raw).hexdigest(),
        headings=parser.headings,
        source_url=source_url,
    )


def _validated_public_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value.strip())
    except ValueError as exc:
        raise ResumeUploadError("Template URL is invalid") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ResumeUploadError("Template URL must use HTTP or HTTPS")
    if parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
        raise ResumeUploadError("Template URL credentials and custom ports are not allowed")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or parsed.scheme)
    except OSError as exc:
        raise ResumeUploadError("Template URL host could not be resolved") from exc
    for address in addresses:
        candidate = ipaddress.ip_address(address[4][0])
        if not candidate.is_global:
            raise ResumeUploadError("Template URL must resolve to a public host")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, "")
    )


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self) -> None:
        super().__init__()
        self.redirect_count = 0

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        del fp, msg, headers
        self.redirect_count += 1
        if self.redirect_count > _MAX_TEMPLATE_REDIRECTS:
            raise ResumeUploadError("Template URL redirected too many times")
        safe_url = _validated_public_url(urllib.parse.urljoin(req.full_url, newurl))
        return urllib.request.Request(
            safe_url,
            headers={"Accept": "text/html", "User-Agent": "SoloScale-Template-Intake/0.1"},
            method="GET",
        )


def inspect_template_url(value: str) -> ResumeTemplateReceipt:
    """Fetch one public HTML page without cookies, credentials, or private-network access."""

    safe_url = _validated_public_url(value)
    redirect_handler = _SafeRedirectHandler()
    opener = urllib.request.build_opener(redirect_handler)
    request = urllib.request.Request(
        safe_url,
        headers={"Accept": "text/html", "User-Agent": "SoloScale-Template-Intake/0.1"},
        method="GET",
    )
    try:
        with opener.open(request, timeout=8) as response:
            content_type = response.headers.get_content_type()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                raise ResumeUploadError("Template URL did not return HTML")
            raw = response.read(_MAX_TEMPLATE_HTML_BYTES + 1)
            final_url = _validated_public_url(response.geturl())
    except ResumeUploadError:
        raise
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        raise ResumeUploadError("Template URL could not be read") from exc
    if len(raw) > _MAX_TEMPLATE_HTML_BYTES:
        raise ResumeUploadError("Template URL returned more than 1 MB")
    parsed = urllib.parse.urlsplit(final_url)
    receipt_url = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path or "/", "", "")
    )
    return inspect_template_html(
        raw,
        source_type=ResumeTemplateSourceType.URL,
        source_url=receipt_url,
    )


def template_metadata_from_receipt(
    receipt: ResumeTemplateReceipt,
) -> ResumeTemplateMetadata:
    return ResumeTemplateMetadata(
        source_format=receipt.source_format,
        section_order=list(receipt.detected_sections),
        heading_names=list(receipt.heading_names),
    )
