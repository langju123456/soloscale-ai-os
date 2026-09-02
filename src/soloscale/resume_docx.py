"""Truth-bounded DOCX tailoring for the local Resume user flow.

The uploaded resume is the fact boundary. AI may prioritize, compress, rewrite, or
synthesize approved facts, while deterministic validation owns export eligibility.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import time
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal
from xml.etree import ElementTree

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    create_model,
    model_validator,
)

from soloscale.model_gateway import ModelCallProfile, ModelGateway
from soloscale.resume_evidence_pack import (
    build_candidate_evidence_pack,
    build_jd_positioning_brief,
    deterministic_hiring_signals,
)
from soloscale.resume_gateway_boundary import (
    ExtractedResumeUpload,
    ResumeTemplateMetadata,
    prepare_resume_gateway_payload,
    restore_role_strategy,
    validate_role_strategy_placeholders,
)
from soloscale.resume_models import (
    CandidateEvidencePack,
    CandidateProfile,
    GroundedResumeBulletRewrite,
    JDPositioningBrief,
    ResumeAtomicFact,
    ResumeEvidenceAdoptionTrace,
    ResumeEvidenceRetrievalTrace,
    ResumeExpertReviewResult,
    RoleStrategy,
    build_resume_atomic_facts,
)

_MAX_DOCX_BYTES = 10 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
_DOCUMENT_PART = "word/document.xml"
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W = f"{{{_W_NS}}}"

_NAMESPACES = {
    "w": _W_NS,
    "wpc": "http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas",
    "mo": "http://schemas.microsoft.com/office/mac/office/2008/main",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "o": "urn:schemas-microsoft-com:office:office",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "v": "urn:schemas-microsoft-com:vml",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "wp14": "http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing",
    "w10": "urn:schemas-microsoft-com:office:word",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
    "w15": "http://schemas.microsoft.com/office/word/2012/wordml",
    "w16cex": "http://schemas.microsoft.com/office/word/2018/wordml/cex",
    "w16cid": "http://schemas.microsoft.com/office/word/2016/wordml/cid",
    "w16": "http://schemas.microsoft.com/office/word/2018/wordml",
    "w16du": "http://schemas.microsoft.com/office/word/2023/wordml/word16du",
    "wps": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
}
for _prefix, _uri in _NAMESPACES.items():
    ElementTree.register_namespace(_prefix, _uri)

_SECTION_HEADINGS = {
    "SUMMARY",
    "PROJECT HIGHLIGHTS",
    "EDUCATION",
    "TECHNICAL SKILLS",
    "WORK EXPERIENCE",
}
_CANONICAL_SECTION_HEADINGS = {
    "SUMMARY": "SUMMARY",
    "PROJECT HIGHLIGHTS": "PROJECT HIGHLIGHTS",
    "EDUCATION": "EDUCATION",
    "TECHNICAL SKILLS": "TECHNICAL SKILLS",
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
_ZH_SECTION_HEADINGS = {
    "SUMMARY": "个人简介",
    "PROJECT HIGHLIGHTS": "项目经历",
    "EDUCATION": "教育经历",
    "TECHNICAL SKILLS": "技术技能",
    "WORK EXPERIENCE": "工作经历",
}
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#./-]{2,}")
_STOP_WORDS = {
    "and",
    "are",
    "for",
    "from",
    "have",
    "job",
    "must",
    "our",
    "preferred",
    "required",
    "role",
    "that",
    "the",
    "this",
    "with",
    "you",
    "your",
}


class ResumeValidationRuleCode(StrEnum):
    """Stable, body-free rejection categories for Resume model evaluation."""

    SCHEMA_PROVIDER_REJECTED = "SCHEMA_PROVIDER_REJECTED"
    OUTPUT_SCHEMA_INVALID = "OUTPUT_SCHEMA_INVALID"
    HIRING_SIGNAL_NOT_SOURCE_GROUNDED = "HIRING_SIGNAL_NOT_SOURCE_GROUNDED"
    HIRING_SIGNAL_DUPLICATE = "HIRING_SIGNAL_DUPLICATE"
    GAP_NOT_SOURCE_GROUNDED = "GAP_NOT_SOURCE_GROUNDED"
    CLAIM_NO_EVIDENCE = "CLAIM_NO_EVIDENCE"
    CLAIM_SOURCE_MISMATCH = "CLAIM_SOURCE_MISMATCH"
    CLAIM_ROLE_INFLATION = "CLAIM_ROLE_INFLATION"
    CLAIM_CLIENT_INFLATION = "CLAIM_CLIENT_INFLATION"
    CLAIM_SCALE_INFLATION = "CLAIM_SCALE_INFLATION"
    CLAIM_OUTCOME_INFLATION = "CLAIM_OUTCOME_INFLATION"
    CLAIM_TECHNOLOGY_INFLATION = "CLAIM_TECHNOLOGY_INFLATION"
    CLAIM_NEW_NUMBER = "CLAIM_NEW_NUMBER"
    CLAIM_CONTRADICTED = "CLAIM_CONTRADICTED"
    REWRITE_NOT_MATERIAL = "REWRITE_NOT_MATERIAL"
    REWRITE_FACT_MUTATION = "REWRITE_FACT_MUTATION"
    OUTPUT_DUPLICATE = "OUTPUT_DUPLICATE"


@dataclass(frozen=True)
class ResumeValidationFailure:
    rule_code: ResumeValidationRuleCode
    json_path: str
    claim_id: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "rule_code": self.rule_code.value,
            "json_path": self.json_path,
            "claim_id": self.claim_id,
        }


@dataclass(frozen=True)
class ResumeValidationDiagnostics:
    failures: tuple[ResumeValidationFailure, ...]
    candidate_count: int
    verified_count: int
    supported_count: int
    rejected_count: int
    duplicate_count: int
    source_span_failure_count: int
    validator_status: str = "rejected"

    @property
    def failure_count(self) -> int:
        return len(self.failures)

    def as_dict(self) -> dict[str, object]:
        return {
            "validator_status": self.validator_status,
            "failure_count": self.failure_count,
            "failures": [failure.as_dict() for failure in self.failures],
            "candidate_count": self.candidate_count,
            "verified_count": self.verified_count,
            "supported_count": self.supported_count,
            "rejected_count": self.rejected_count,
            "duplicate_count": self.duplicate_count,
            "source_span_failure_count": self.source_span_failure_count,
        }


class ResumeTemplateError(ValueError):
    """Raised when Resume input or generated output violates its contract."""

    def __init__(
        self,
        message: str,
        *,
        validation_diagnostics: ResumeValidationDiagnostics | None = None,
    ) -> None:
        super().__init__(message)
        self.validation_diagnostics = validation_diagnostics


@dataclass(frozen=True)
class TemplateParagraph:
    text: str
    is_bullet: bool


@dataclass(frozen=True)
class TailoredDocx:
    content: bytes
    template_sha256: str
    output_sha256: str
    project_blocks_reordered: int
    skill_bullets_reordered: int
    source_paragraph_count: int
    claims_preserved: bool
    grounded_rewrites: int = 0
    synthesized_rewrites: int = 0
    summary_rewritten: bool = False
    rejected_rewrites: int = 0
    generation_mode: str = "template"
    provider: str | None = None
    model: str | None = None
    output_locale: Literal["en-US", "zh-CN"] = "en-US"
    model_call_profile: dict[str, object] | None = None
    validation_diagnostics: ResumeValidationDiagnostics | None = None
    role_strategy: RoleStrategy | None = None
    candidate_evidence_pack: CandidateEvidencePack | None = None
    positioning_brief: JDPositioningBrief | None = None
    evidence_retrieval_trace: ResumeEvidenceRetrievalTrace | None = None
    evidence_adoption: tuple[ResumeEvidenceAdoptionTrace, ...] = ()
    role_strategy_fallback_applied: bool = False
    role_strategy_fallback_code: str | None = None
    expert_review: ResumeExpertReviewResult | None = None
    expert_review_attempted: bool = False
    expert_review_skipped_code: str | None = None
    expert_rewrites: int = 0
    expert_provider: str | None = None
    expert_model: str | None = None


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    return "".join(node.text or "" for node in paragraph.iter(f"{_W}t")).strip()


def _remove_trailing_empty_paragraphs(body: ElementTree.Element) -> int:
    """Remove body-final empty layout paragraphs that can create a blank page."""

    removed = 0
    blocking_tags = {f"{_W}{name}" for name in ("br", "drawing", "object", "pict")}
    children = list(body)
    section_index = next(
        (index for index, child in enumerate(children) if child.tag == f"{_W}sectPr"),
        len(children),
    )
    for child in reversed(children[:section_index]):
        if child.tag != f"{_W}p":
            continue
        if _paragraph_text(child) or any(node.tag in blocking_tags for node in child.iter()):
            break
        body.remove(child)
        removed += 1
    return removed


def _is_bullet(paragraph: ElementTree.Element) -> bool:
    properties = paragraph.find(f"{_W}pPr")
    return properties is not None and properties.find(f"{_W}numPr") is not None


def _validate_member(info: zipfile.ZipInfo) -> None:
    name = PurePosixPath(info.filename)
    if name.is_absolute() or ".." in name.parts or not info.filename:
        raise ResumeTemplateError("DOCX contains an unsafe package path")
    if info.flag_bits & 0x1:
        raise ResumeTemplateError("Encrypted DOCX files are not supported")


def _read_package(template: bytes) -> tuple[list[tuple[zipfile.ZipInfo, bytes]], bytes]:
    if not template:
        raise ResumeTemplateError("Please upload a DOCX resume template")
    if len(template) > _MAX_DOCX_BYTES:
        raise ResumeTemplateError("DOCX template must be 10 MB or smaller")
    try:
        with zipfile.ZipFile(io.BytesIO(template)) as archive:
            names: set[str] = set()
            total_size = 0
            members: list[tuple[zipfile.ZipInfo, bytes]] = []
            for info in archive.infolist():
                _validate_member(info)
                if info.filename in names:
                    raise ResumeTemplateError("DOCX contains duplicate package entries")
                names.add(info.filename)
                total_size += info.file_size
                if total_size > _MAX_UNCOMPRESSED_BYTES:
                    raise ResumeTemplateError("DOCX expands beyond the 50 MB safety limit")
                members.append((info, archive.read(info)))
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ResumeTemplateError("The uploaded file is not a readable DOCX") from exc
    document = next((content for info, content in members if info.filename == _DOCUMENT_PART), None)
    if document is None:
        raise ResumeTemplateError("DOCX is missing word/document.xml")
    return members, document


def _parse_document(document: bytes) -> tuple[ElementTree.Element, ElementTree.Element]:
    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as exc:
        raise ResumeTemplateError("DOCX document XML is malformed") from exc
    body = root.find(f"{_W}body")
    if body is None:
        raise ResumeTemplateError("DOCX does not contain a Word document body")
    return root, body


def read_template_paragraphs(template: bytes) -> list[TemplateParagraph]:
    """Extract visible body paragraphs without macros, relationships, or external calls."""
    _, document = _read_package(template)
    _, body = _parse_document(document)
    return [
        TemplateParagraph(text=_paragraph_text(child), is_bullet=_is_bullet(child))
        for child in body
        if child.tag == f"{_W}p"
    ]


def _heading(value: str) -> str:
    normalized = " ".join(value.upper().rstrip(":：").split())
    return _CANONICAL_SECTION_HEADINGS.get(normalized, normalized)


def _canonical_section_heading(value: str) -> str | None:
    normalized = " ".join(value.upper().rstrip(":：").split())
    return _CANONICAL_SECTION_HEADINGS.get(normalized)


def apply_resume_template_structure(
    template: bytes, section_order: list[str]
) -> bytes:
    """Apply a confirmed section order without importing any template copy."""

    requested: list[str] = []
    for value in section_order:
        if value in _SECTION_HEADINGS and value not in requested:
            requested.append(value)
    if not requested:
        return template
    members, document = _read_package(template)
    root, body = _parse_document(document)
    children = list(body)
    section_markers = [
        (index, _canonical_section_heading(_paragraph_text(child)))
        for index, child in enumerate(children)
        if child.tag == f"{_W}p"
        and _canonical_section_heading(_paragraph_text(child)) is not None
    ]
    if not section_markers:
        return template
    first_section = section_markers[0][0]
    section_end = next(
        (
            index
            for index, child in enumerate(children[first_section:], start=first_section)
            if child.tag == f"{_W}sectPr"
        ),
        len(children),
    )
    prefix = children[:first_section]
    suffix = children[section_end:]
    blocks: dict[str, list[ElementTree.Element]] = {}
    current_order: list[str] = []
    for marker_index, (start, canonical) in enumerate(section_markers):
        if canonical is None or start >= section_end:
            continue
        end = (
            section_markers[marker_index + 1][0]
            if marker_index + 1 < len(section_markers)
            else section_end
        )
        blocks[canonical] = children[start:end]
        current_order.append(canonical)
    final_order = [value for value in requested if value in blocks]
    final_order.extend(value for value in current_order if value not in final_order)
    replacement = prefix + [child for value in final_order for child in blocks[value]] + suffix
    if replacement == children:
        return template
    for child in list(body):
        body.remove(child)
    for child in replacement:
        body.append(child)
    tailored_document = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        for info, content in members:
            archive.writestr(
                info, tailored_document if info.filename == _DOCUMENT_PART else content
            )
    return target.getvalue()


def _localize_resume_headings(
    body: ElementTree.Element, output_locale: Literal["en-US", "zh-CN"]
) -> None:
    if output_locale != "zh-CN":
        return
    for paragraph in body:
        if paragraph.tag != f"{_W}p":
            continue
        canonical = _canonical_section_heading(_paragraph_text(paragraph))
        replacement = _ZH_SECTION_HEADINGS.get(canonical or "")
        if replacement is None:
            continue
        nodes = list(paragraph.iter(f"{_W}t"))
        if not nodes:
            continue
        nodes[0].text = replacement
        for node in nodes[1:]:
            node.text = ""


def _section_slice(
    paragraphs: list[TemplateParagraph], start_heading: str
) -> list[TemplateParagraph]:
    start = next(
        (
            index
            for index, paragraph in enumerate(paragraphs)
            if _heading(paragraph.text) == start_heading
        ),
        None,
    )
    if start is None:
        return []
    end = next(
        (
            index
            for index, paragraph in enumerate(paragraphs[start + 1 :], start=start + 1)
            if _heading(paragraph.text) in _SECTION_HEADINGS
        ),
        len(paragraphs),
    )
    return paragraphs[start + 1 : end]


def _nonblank_text(paragraphs: Iterable[TemplateParagraph]) -> list[str]:
    return [paragraph.text for paragraph in paragraphs if paragraph.text]


def extract_candidate_profile(template: bytes) -> CandidateProfile:
    """Build operator-supplied profile facts from the uploaded resume only."""
    paragraphs = read_template_paragraphs(template)
    summary_index = next(
        (
            index
            for index, paragraph in enumerate(paragraphs)
            if _heading(paragraph.text) == "SUMMARY"
        ),
        len(paragraphs),
    )
    identity = [paragraph.text for paragraph in paragraphs[:summary_index] if paragraph.text]
    summary = _nonblank_text(_section_slice(paragraphs, "SUMMARY"))
    projects = _section_slice(paragraphs, "PROJECT HIGHLIGHTS")
    education = _nonblank_text(_section_slice(paragraphs, "EDUCATION"))
    skills = _section_slice(paragraphs, "TECHNICAL SKILLS")
    experience = _section_slice(paragraphs, "WORK EXPERIENCE")
    return CandidateProfile(
        full_name=identity[0] if identity else None,
        headline=identity[1] if len(identity) > 1 else None,
        summary=summary[0] if summary else None,
        skills=[paragraph.text for paragraph in skills if paragraph.text and paragraph.is_bullet],
        project_bullets=[
            paragraph.text for paragraph in projects if paragraph.text and paragraph.is_bullet
        ],
        experience_bullets=[
            paragraph.text for paragraph in experience if paragraph.text and paragraph.is_bullet
        ],
        education=education,
    )


def _terms(text: str) -> set[str]:
    return {
        token.casefold().strip("./-")
        for token in _TOKEN_RE.findall(text)
        if token.casefold().strip("./-") not in _STOP_WORDS
    }


def _score(text: str, job_terms: set[str]) -> int:
    paragraph_terms = _terms(text)
    return sum(term in paragraph_terms for term in job_terms)


def _section_bounds(
    children: list[ElementTree.Element], start_heading: str
) -> tuple[int, int] | None:
    start = next(
        (
            index
            for index, child in enumerate(children)
            if child.tag == f"{_W}p" and _heading(_paragraph_text(child)) == start_heading
        ),
        None,
    )
    if start is None:
        return None
    end = next(
        (
            index
            for index, child in enumerate(children[start + 1 :], start=start + 1)
            if child.tag == f"{_W}sectPr"
            or (
                child.tag == f"{_W}p"
                and _heading(_paragraph_text(child)) in _SECTION_HEADINGS
            )
        ),
        len(children),
    )
    return start + 1, end


def _replace_range(
    body: ElementTree.Element,
    start: int,
    end: int,
    replacement: list[ElementTree.Element],
) -> None:
    current = list(body)[start:end]
    for child in current:
        body.remove(child)
    for offset, child in enumerate(replacement):
        body.insert(start + offset, child)


def _reorder_project_blocks(
    body: ElementTree.Element, children: list[ElementTree.Element], job_terms: set[str]
) -> int:
    bounds = _section_bounds(children, "PROJECT HIGHLIGHTS")
    if bounds is None:
        return 0
    start, end = bounds
    section = children[start:end]
    prefix: list[ElementTree.Element] = []
    blocks: list[list[ElementTree.Element]] = []
    current: list[ElementTree.Element] = []
    for child in section:
        text = _paragraph_text(child) if child.tag == f"{_W}p" else ""
        if text and not _is_bullet(child):
            if current:
                blocks.append(current)
            current = [child]
        elif current:
            current.append(child)
        else:
            prefix.append(child)
    if current:
        blocks.append(current)
    if len(blocks) < 2:
        return 0
    ranked = sorted(
        enumerate(blocks),
        key=lambda item: (
            -_score(" ".join(_paragraph_text(child) for child in item[1]), job_terms),
            item[0],
        ),
    )
    reordered_count = sum(index != original for index, (original, _) in enumerate(ranked))
    if reordered_count:
        replacement = prefix + [child for _, block in ranked for child in block]
        _replace_range(body, start, end, replacement)
    return reordered_count


def _reorder_skill_bullets(
    body: ElementTree.Element, children: list[ElementTree.Element], job_terms: set[str]
) -> int:
    bounds = _section_bounds(children, "TECHNICAL SKILLS")
    if bounds is None:
        return 0
    start, end = bounds
    section = children[start:end]
    bullet_positions = [index for index, child in enumerate(section) if _is_bullet(child)]
    if len(bullet_positions) < 2:
        return 0
    bullets = [section[index] for index in bullet_positions]
    ranked = sorted(
        enumerate(bullets),
        key=lambda item: (-_score(_paragraph_text(item[1]), job_terms), item[0]),
    )
    reordered_count = sum(index != original for index, (original, _) in enumerate(ranked))
    if reordered_count:
        replacement = list(section)
        for position, (_, bullet) in zip(bullet_positions, ranked, strict=True):
            replacement[position] = bullet
        _replace_range(body, start, end, replacement)
    return reordered_count


def _profile_entries(profile: CandidateProfile) -> dict[str, str]:
    claims = profile.experience_bullets + profile.project_bullets
    if len(set(claims)) != len(claims):
        raise ResumeTemplateError(
            "AI tailoring requires each approved resume bullet to have unique text"
        )
    return {f"PROFILE-{index:02d}": text for index, text in enumerate(claims, start=1)}


def _profile_skills(profile: CandidateProfile) -> dict[str, str]:
    if len(set(profile.skills)) != len(profile.skills):
        raise ResumeTemplateError(
            "AI tailoring requires each approved skill line to have unique text"
        )
    return {
        f"SKILL-{index:02d}": text
        for index, text in enumerate(profile.skills, start=1)
    }


def _exact_priority_validator(
    allowed_values: tuple[str, ...], *, field_name: str
) -> Callable[[list[str]], list[str]]:
    allowed = frozenset(allowed_values)

    def validate(values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError(f"{field_name} values must be unique")
        if set(values) != allowed:
            raise ValueError(f"{field_name} must contain every allowed value exactly once")
        return values

    return validate


def _allowed_id_list_validator(
    allowed_values: tuple[str, ...], *, minimum: int
) -> Callable[[list[str]], list[str]]:
    allowed = frozenset(allowed_values)

    def validate(values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("referenced identities must be unique")
        if len(values) < minimum or not set(values) <= allowed:
            raise ValueError("referenced identities must be approved request IDs")
        return values

    return validate


class _ExactRoleStrategyResponse(BaseModel):
    """Current wire contract with one ignored legacy model-authored field."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_hiring_signals(cls, value: object) -> object:
        if isinstance(value, dict) and "top_hiring_signals" in value:
            cleaned = dict(value)
            cleaned.pop("top_hiring_signals", None)
            return cleaned
        return value


def _exact_role_strategy_model(
    entry_ids: list[str],
    *,
    skill_ids: list[str],
    fact_ids: list[str],
    include_summary: bool,
) -> type[BaseModel]:
    """Build a request-specific schema whose rewrite keys cannot be omitted."""

    evidence_schema_extra: dict[str, Any] = {
        "items": {"enum": entry_ids, "type": "string"},
        "uniqueItems": True,
    }
    skill_items: dict[str, Any] = {"type": "string"}
    if skill_ids:
        skill_items["enum"] = skill_ids
    skill_schema_extra: dict[str, Any] = {
        "items": skill_items,
        "uniqueItems": True,
    }

    source_fact_id_schema: dict[str, Any] = {
        "items": {"enum": fact_ids, "type": "string"},
        "uniqueItems": True,
    }
    rewrite_body = create_model(
        "GroundedRewriteBody",
        __config__=ConfigDict(extra="forbid"),
        kind=(Literal["REWRITE", "SYNTHESIS"], ...),
        text=(str, Field(min_length=1, max_length=600)),
        source_fact_ids=(
            Annotated[
                list[str],
                AfterValidator(
                    _allowed_id_list_validator(tuple(fact_ids), minimum=1)
                ),
            ],
            Field(
                min_length=1,
                max_length=32,
                json_schema_extra=source_fact_id_schema,
            ),
        ),
    )
    summary_body = create_model(
        "GroundedSummaryBody",
        __config__=ConfigDict(extra="forbid"),
        text=(str, Field(min_length=1, max_length=800)),
        source_fact_ids=(
            Annotated[
                list[str],
                AfterValidator(
                    _allowed_id_list_validator(tuple(fact_ids), minimum=2)
                ),
            ],
            Field(
                min_length=2,
                max_length=32,
                json_schema_extra=source_fact_id_schema,
            ),
        ),
    )
    rewrite_fields: dict[str, Any] = {
        entry_id: (rewrite_body, ...) for entry_id in entry_ids
    }
    rewrite_map = create_model(
        "ExactBulletRewrites",
        __config__=ConfigDict(extra="forbid"),
        **rewrite_fields,
    )
    return create_model(
        "ExactRoleStrategy",
        __base__=_ExactRoleStrategyResponse,
        role_summary=(str, Field(min_length=1, max_length=500)),
        evidence_priority=(
            Annotated[
                list[str],
                AfterValidator(
                    _exact_priority_validator(
                        tuple(entry_ids), field_name="evidence_priority"
                    )
                ),
            ],
            Field(
                min_length=len(entry_ids),
                max_length=len(entry_ids),
                json_schema_extra=evidence_schema_extra,
            ),
        ),
        skill_priority=(
            Annotated[
                list[str],
                AfterValidator(
                    _exact_priority_validator(
                        tuple(skill_ids), field_name="skill_priority"
                    )
                ),
            ],
            Field(
                min_length=len(skill_ids),
                max_length=len(skill_ids),
                json_schema_extra=skill_schema_extra,
            ),
        ),
        bullet_rewrites=(rewrite_map, ...),
        summary_rewrite=(
            (summary_body | None) if include_summary else type(None),
            ...,
        ),
        unsupported_requirements=(
            list[str],
            Field(default_factory=list, max_length=16),
        ),
        rewrite_guidance=(str, Field(min_length=1, max_length=800)),
    )


def _role_strategy_from_exact(
    response: BaseModel,
    *,
    entry_ids: list[str],
    sanitized_skill_by_id: dict[str, str],
    fact_source_by_id: dict[str, str],
    top_hiring_signals: list[str],
) -> RoleStrategy:
    payload = response.model_dump(mode="json")
    payload["top_hiring_signals"] = top_hiring_signals
    rewrite_map = payload.pop("bullet_rewrites", None)
    if not isinstance(rewrite_map, dict):
        raise ResumeTemplateError("AI role strategy returned an invalid rewrite map")
    rewrites: list[dict[str, object]] = []
    for entry_id in entry_ids:
        body = rewrite_map.get(entry_id)
        if not isinstance(body, dict):
            raise ResumeTemplateError(
                "AI role strategy omitted an approved profile entry"
            )
        fact_ids = body.get("source_fact_ids")
        if not isinstance(fact_ids, list) or any(
            not isinstance(value, str) for value in fact_ids
        ):
            raise ResumeTemplateError(
                "AI role strategy returned invalid atomic fact references"
            )
        try:
            source_ids = list(
                dict.fromkeys(fact_source_by_id[fact_id] for fact_id in fact_ids)
            )
        except KeyError as error:
            raise ResumeTemplateError(
                "AI role strategy referenced an unknown atomic fact"
            ) from error
        body["source_profile_entry_ids"] = source_ids
        rewrites.append({"profile_entry_id": entry_id, **body})
    payload["bullet_rewrites"] = rewrites
    skill_priority = payload.get("skill_priority")
    if not isinstance(skill_priority, list) or any(
        not isinstance(value, str) for value in skill_priority
    ):
        raise ResumeTemplateError("AI role strategy returned an invalid skill priority")
    try:
        payload["skill_priority"] = [
            sanitized_skill_by_id[skill_id] for skill_id in skill_priority
        ]
    except KeyError as error:
        raise ResumeTemplateError(
            "AI role strategy referenced an unknown approved skill"
        ) from error
    summary = payload.get("summary_rewrite")
    if isinstance(summary, dict):
        summary_fact_ids = summary.get("source_fact_ids")
        if not isinstance(summary_fact_ids, list) or any(
            not isinstance(value, str) for value in summary_fact_ids
        ):
            raise ResumeTemplateError(
                "AI role strategy returned invalid Summary fact references"
            )
        try:
            summary["source_profile_entry_ids"] = list(
                dict.fromkeys(
                    fact_source_by_id[fact_id] for fact_id in summary_fact_ids
                )
            )
        except KeyError as error:
            raise ResumeTemplateError(
                "AI role strategy referenced an unknown Summary atomic fact"
            ) from error
    return RoleStrategy.model_validate(payload)


_HIRING_SIGNAL_HEADINGS = {
    "about the job",
    "job description",
    "job responsibilities",
    "preferred qualifications",
    "qualifications",
    "requirements",
    "responsibilities",
}


def _deterministic_hiring_signals(job_description: str) -> list[str]:
    """Select bounded exact JD spans without asking the model to restate them."""
    if not job_description.strip():
        raise ResumeTemplateError("Job description must not be empty")
    return deterministic_hiring_signals(job_description)


_GENERIC_JD_TERMS = {
    "ability",
    "build",
    "building",
    "candidate",
    "development",
    "engineer",
    "engineering",
    "experience",
    "responsibilities",
    "skills",
    "strong",
    "team",
    "work",
}
_OWNERSHIP_TERMS = {
    "directed",
    "founded",
    "headed",
    "led",
    "managed",
    "owned",
    "spearheaded",
}
_CLIENT_TERMS = {"client", "clients", "customer", "customers"}
_SCALE_TERMS = {
    "enterprise",
    "global",
    "large-scale",
    "millions",
    "mission-critical",
    "production",
    "users",
}
_OUTCOME_TERMS = {
    "accelerated",
    "boosted",
    "grew",
    "improved",
    "increased",
    "optimized",
    "reduced",
    "saved",
}
_TECHNOLOGY_TERMS = {
    "autogen",
    "aws",
    "chroma",
    "cloud-run",
    "django",
    "docker",
    "fastapi",
    "flask",
    "gcp",
    "kafka",
    "kubernetes",
    "langchain",
    "langgraph",
    "llamaindex",
    "mysql",
    "postgres",
    "postgresql",
    "pubsub",
    "python",
    "react",
    "redis",
    "sql",
    "vertex",
}
_FACT_ACTION_TERMS = {
    "added",
    "built",
    "created",
    "delivered",
    "designed",
    "developed",
    "engineered",
    "implemented",
    "made",
    "shipped",
    "supported",
    "validated",
}
_NUMBER_RE = re.compile(r"(?<!\w)\d+(?:[.,]\d+)?%?\+?(?!\w)")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#./-]*")
_ZH_INFLATION_TERMS: tuple[
    tuple[tuple[str, ...], ResumeValidationRuleCode], ...
] = (
    (("主导", "领导", "带领", "全权负责"), ResumeValidationRuleCode.CLAIM_ROLE_INFLATION),
    (("客户", "付费用户"), ResumeValidationRuleCode.CLAIM_CLIENT_INFLATION),
    (
        ("百万级", "大规模", "企业级", "生产级"),
        ResumeValidationRuleCode.CLAIM_SCALE_INFLATION,
    ),
    (
        ("显著提升", "大幅提升", "大幅降低", "收入增长", "节省成本"),
        ResumeValidationRuleCode.CLAIM_OUTCOME_INFLATION,
    ),
)
_ZH_FACT_ANCHOR_ALIASES: dict[str, tuple[str, ...]] = {
    "architecture": ("架构",),
    "client": ("客户",),
    "customer": ("客户",),
    "customer-facing": ("面向客户", "客户场景"),
    "delivery": ("交付",),
    "development": ("开发",),
    "engineer": ("工程师",),
    "evaluation": ("评估",),
    "evidence-grounded": ("证据驱动", "证据支撑"),
    "orchestration": ("编排",),
    "improved": ("改进", "提升"),
    "reliability": ("可靠性",),
    "requirements": ("需求",),
    "retrieval": ("检索",),
    "stakeholder": ("利益相关方", "协作方"),
    "workflow": ("工作流",),
}


def _normalized_words(text: str) -> set[str]:
    return {
        token.casefold()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9+#.]*", text)
    }


def _fact_anchor_terms(text: str) -> set[str]:
    """Keep the stable subjects/objects of a fact while allowing verb paraphrases."""

    terms = _terms(text)
    anchors = terms - _FACT_ACTION_TERMS - _GENERIC_JD_TERMS
    return anchors or terms


def _cross_locale_fact_match(
    *,
    source_fact: str,
    candidate_claim: str,
    target_locale: Literal["en-US", "zh-CN"],
    source_anchors: set[str],
    candidate_terms: set[str],
) -> bool:
    """Match only curated semantic anchors when source and target languages differ."""

    source_has_chinese = any(
        "\u4e00" <= character <= "\u9fff" for character in source_fact
    )
    if target_locale == "zh-CN" and not source_has_chinese:
        return any(
            alias in candidate_claim
            for anchor in source_anchors
            for alias in _ZH_FACT_ANCHOR_ALIASES.get(anchor, ())
        )
    if target_locale == "en-US" and source_has_chinese:
        return any(
            anchor in candidate_terms
            and any(alias in source_fact for alias in chinese_aliases)
            for anchor, chinese_aliases in _ZH_FACT_ANCHOR_ALIASES.items()
        )
    return False


def _source_terms_for_target_locale(
    source_text: str, target_locale: Literal["en-US", "zh-CN"]
) -> set[str]:
    terms = _normalized_words(source_text) | _terms(source_text)
    if target_locale == "en-US":
        terms.update(
            anchor
            for anchor, chinese_aliases in _ZH_FACT_ANCHOR_ALIASES.items()
            if any(alias in source_text for alias in chinese_aliases)
        )
    return terms


def _protected_facts(text: str) -> set[str]:
    """Collect numbers and name/technology-like tokens that rewrites cannot invent."""
    protected = {match.group(0).casefold() for match in _NUMBER_RE.finditer(text)}
    words = list(_WORD_RE.finditer(text))
    for index, match in enumerate(words):
        token = match.group(0).strip("./-")
        if not token:
            continue
        if (
            any(character in token for character in "+#./")
            or any(character.isdigit() for character in token)
            or (len(token) > 1 and token.isupper())
            or any(character.isupper() for character in token[1:])
            or (index > 0 and token[0].isupper())
        ):
            protected.add(token.casefold())
    return protected


def _duplicate_indices(values: list[str], *, casefold: bool = False) -> list[int]:
    seen: set[str] = set()
    duplicates: list[int] = []
    for index, value in enumerate(values):
        key = value.casefold() if casefold else value
        if key in seen:
            duplicates.append(index)
        else:
            seen.add(key)
    return duplicates


def _validate_role_strategy(
    strategy: RoleStrategy,
    *,
    profile: CandidateProfile,
    job_description: str,
    atomic_facts: list[ResumeAtomicFact] | None = None,
    output_locale: Literal["en-US", "zh-CN"] = "en-US",
) -> dict[str, str]:
    entries = _profile_entries(profile)
    atomic_fact_by_id = {
        item.fact_id: item
        for item in (atomic_facts or build_resume_atomic_facts(profile))
    }
    expected_ids = set(entries)
    failures: list[ResumeValidationFailure] = []
    claim_failure_ids: set[str] = set()
    duplicate_count = 0
    source_span_failure_count = 0

    def reject(
        rule_code: ResumeValidationRuleCode,
        json_path: str,
        *,
        claim_id: str | None = None,
        duplicate: bool = False,
        source_span: bool = False,
    ) -> None:
        nonlocal duplicate_count, source_span_failure_count
        failures.append(
            ResumeValidationFailure(
                rule_code=rule_code,
                json_path=json_path,
                claim_id=claim_id,
            )
        )
        if claim_id in expected_ids or claim_id == "SUMMARY":
            claim_failure_ids.add(claim_id)
        if duplicate:
            duplicate_count += 1
        if source_span:
            source_span_failure_count += 1

    for index in _duplicate_indices(strategy.evidence_priority):
        evidence_id = strategy.evidence_priority[index]
        reject(
            ResumeValidationRuleCode.OUTPUT_DUPLICATE,
            f"$.evidence_priority[{index}]",
            claim_id=evidence_id if evidence_id in expected_ids else None,
            duplicate=True,
        )
    if set(strategy.evidence_priority) != expected_ids:
        reject(
            ResumeValidationRuleCode.OUTPUT_SCHEMA_INVALID,
            "$.evidence_priority",
        )
        claim_failure_ids.update(expected_ids - set(strategy.evidence_priority))

    expected_skills = set(profile.skills)
    for index in _duplicate_indices(strategy.skill_priority):
        reject(
            ResumeValidationRuleCode.OUTPUT_DUPLICATE,
            f"$.skill_priority[{index}]",
            duplicate=True,
        )
    if set(strategy.skill_priority) != expected_skills:
        reject(
            ResumeValidationRuleCode.OUTPUT_SCHEMA_INVALID,
            "$.skill_priority",
        )

    rewrite_ids = [item.profile_entry_id for item in strategy.bullet_rewrites]
    for index in _duplicate_indices(rewrite_ids):
        reject(
            ResumeValidationRuleCode.OUTPUT_DUPLICATE,
            f"$.bullet_rewrites[{index}].profile_entry_id",
            claim_id=rewrite_ids[index],
            duplicate=True,
        )
    rewrites = {item.profile_entry_id: item for item in strategy.bullet_rewrites}
    if set(rewrites) != expected_ids:
        reject(
            ResumeValidationRuleCode.OUTPUT_SCHEMA_INVALID,
            "$.bullet_rewrites",
        )
        claim_failure_ids.update(expected_ids - set(rewrites))

    jd_terms = _terms(job_description) - _GENERIC_JD_TERMS

    def validate_grounded_text(
        *,
        text: str,
        source_ids: list[str],
        source_fact_ids: list[str],
        json_path: str,
        claim_id: str,
    ) -> None:
        if not source_ids or any(source_id not in entries for source_id in source_ids):
            reject(ResumeValidationRuleCode.OUTPUT_SCHEMA_INVALID, json_path)
            return
        resolved_facts = []
        missing_fact_reference = False
        for fact_index, fact_id in enumerate(source_fact_ids):
            fact = atomic_fact_by_id.get(fact_id)
            if fact is None:
                missing_fact_reference = True
                reject(
                    ResumeValidationRuleCode.CLAIM_SOURCE_MISMATCH,
                    f"{json_path}.source_fact_ids[{fact_index}]",
                    claim_id=claim_id,
                    source_span=True,
                )
                continue
            resolved_facts.append(fact)
        resolved_source_ids = list(
            dict.fromkeys(fact.profile_entry_id for fact in resolved_facts)
        )
        if not missing_fact_reference and resolved_source_ids != source_ids:
            reject(
                ResumeValidationRuleCode.CLAIM_SOURCE_MISMATCH,
                f"{json_path}.source_fact_ids",
                claim_id=claim_id,
                source_span=True,
            )

        output_terms = _terms(text)
        for source_id in source_ids:
            attributed = [
                fact
                for fact in resolved_facts
                if fact.profile_entry_id == source_id
            ]
            if not attributed:
                reject(
                    ResumeValidationRuleCode.CLAIM_NO_EVIDENCE,
                    f"{json_path}.text",
                    claim_id=claim_id,
                )
                break
        for fact in resolved_facts:
            fact_anchors = _fact_anchor_terms(fact.text)
            if output_locale == "zh-CN":
                fact_anchors = {
                    value
                    for value in fact_anchors
                    if value in _TECHNOLOGY_TERMS
                    or any(character.isdigit() for character in value)
                    or len(value) >= 4
                }
            cross_locale_anchor_present = _cross_locale_fact_match(
                source_fact=fact.text,
                candidate_claim=text,
                target_locale=output_locale,
                source_anchors=fact_anchors,
                candidate_terms=output_terms,
            )
            if (
                not (fact_anchors & output_terms)
                and not cross_locale_anchor_present
            ):
                reject(
                    ResumeValidationRuleCode.CLAIM_NO_EVIDENCE,
                    f"{json_path}.text",
                    claim_id=claim_id,
                )
                break

        source_union = " ".join(fact.text for fact in resolved_facts)
        allowed_numbers = {
            value.casefold() for fact in resolved_facts for value in fact.allowed_numbers
        }
        source_words = _source_terms_for_target_locale(source_union, output_locale)
        localized_protected_allowlist: set[str] = set()
        if output_locale == "zh-CN" and any(
            value == "ai" or value.startswith("ai-") or value.endswith("ai")
            for value in source_words
        ):
            localized_protected_allowlist.add("ai")
        invented_protected = (
            _protected_facts(text)
            - _protected_facts(source_union)
            - allowed_numbers
            - localized_protected_allowlist
        )
        invented_numbers = {
            match.group(0).casefold() for match in _NUMBER_RE.finditer(text)
        } - {
            match.group(0).casefold()
            for match in _NUMBER_RE.finditer(source_union)
        } - allowed_numbers
        output_words = _normalized_words(text) | _terms(text)
        introduced_words = output_words - source_words
        if invented_numbers:
            reject(
                ResumeValidationRuleCode.CLAIM_NEW_NUMBER,
                f"{json_path}.text",
                claim_id=claim_id,
            )
        if invented_protected - invented_numbers:
            reject(
                ResumeValidationRuleCode.REWRITE_FACT_MUTATION,
                f"{json_path}.text",
                claim_id=claim_id,
            )
        if (_terms(text) & jd_terms) - source_words:
            reject(
                ResumeValidationRuleCode.CLAIM_ROLE_INFLATION,
                f"{json_path}.text",
                claim_id=claim_id,
            )
        for vocabulary, rule_code in (
            (_OWNERSHIP_TERMS, ResumeValidationRuleCode.CLAIM_ROLE_INFLATION),
            (_CLIENT_TERMS, ResumeValidationRuleCode.CLAIM_CLIENT_INFLATION),
            (_SCALE_TERMS, ResumeValidationRuleCode.CLAIM_SCALE_INFLATION),
            (_OUTCOME_TERMS, ResumeValidationRuleCode.CLAIM_OUTCOME_INFLATION),
            (
                _TECHNOLOGY_TERMS,
                ResumeValidationRuleCode.CLAIM_TECHNOLOGY_INFLATION,
            ),
        ):
            if introduced_words & vocabulary:
                reject(rule_code, f"{json_path}.text", claim_id=claim_id)
        if output_locale == "zh-CN":
            for phrases, rule_code in _ZH_INFLATION_TERMS:
                if any(phrase in text and phrase not in source_union for phrase in phrases):
                    reject(rule_code, f"{json_path}.text", claim_id=claim_id)

    for index, rewrite in enumerate(strategy.bullet_rewrites):
        entry_id = rewrite.profile_entry_id
        if entry_id not in entries:
            continue
        if rewrite.kind == "REWRITE" and any(
            atomic_fact_by_id.get(fact_id) is not None
            and atomic_fact_by_id[fact_id].source_kind != "PROFILE_ENTRY"
            for fact_id in rewrite.source_fact_ids
        ):
            reject(
                ResumeValidationRuleCode.CLAIM_SOURCE_MISMATCH,
                f"$.bullet_rewrites[{index}].source_fact_ids",
                claim_id=entry_id,
                source_span=True,
            )
        validate_grounded_text(
            text=rewrite.text,
            source_ids=rewrite.source_profile_entry_ids,
            source_fact_ids=rewrite.source_fact_ids,
            json_path=f"$.bullet_rewrites[{index}]",
            claim_id=entry_id,
        )

    if strategy.summary_rewrite is not None:
        if profile.summary is None:
            reject(ResumeValidationRuleCode.OUTPUT_SCHEMA_INVALID, "$.summary_rewrite")
        else:
            validate_grounded_text(
                text=strategy.summary_rewrite.text,
                source_ids=strategy.summary_rewrite.source_profile_entry_ids,
                source_fact_ids=strategy.summary_rewrite.source_fact_ids,
                json_path="$.summary_rewrite",
                claim_id="SUMMARY",
            )

    job_description_folded = job_description.casefold()
    for index, item in enumerate(strategy.unsupported_requirements):
        if item.casefold() not in job_description_folded:
            reject(
                ResumeValidationRuleCode.GAP_NOT_SOURCE_GROUNDED,
                f"$.unsupported_requirements[{index}]",
                source_span=True,
            )
    for index in _duplicate_indices(strategy.top_hiring_signals, casefold=True):
        reject(
            ResumeValidationRuleCode.HIRING_SIGNAL_DUPLICATE,
            f"$.top_hiring_signals[{index}]",
            duplicate=True,
        )
    for index, item in enumerate(strategy.top_hiring_signals):
        if item.casefold() not in job_description_folded:
            reject(
                ResumeValidationRuleCode.HIRING_SIGNAL_NOT_SOURCE_GROUNDED,
                f"$.top_hiring_signals[{index}]",
                source_span=True,
            )

    if failures:
        summary_accepted = profile.summary is not None and "SUMMARY" not in claim_failure_ids
        verified_count = int(summary_accepted and strategy.summary_rewrite is None)
        supported_count = int(summary_accepted and strategy.summary_rewrite is not None)
        for entry_id in expected_ids - claim_failure_ids:
            selected_rewrite = rewrites.get(entry_id)
            if selected_rewrite is None:
                continue
            if selected_rewrite.text == entries[entry_id]:
                verified_count += 1
            else:
                supported_count += 1
        raise ResumeTemplateError(
            "AI role strategy failed deterministic truth validation",
            validation_diagnostics=ResumeValidationDiagnostics(
                failures=tuple(failures),
                candidate_count=len(entries) + int(profile.summary is not None),
                verified_count=verified_count,
                supported_count=supported_count,
                rejected_count=len(claim_failure_ids),
                duplicate_count=duplicate_count,
                source_span_failure_count=source_span_failure_count,
            ),
        )
    return entries


_SELECTIVE_REWRITE_FAILURES = {
    ResumeValidationRuleCode.CLAIM_NO_EVIDENCE,
    ResumeValidationRuleCode.CLAIM_SOURCE_MISMATCH,
    ResumeValidationRuleCode.CLAIM_ROLE_INFLATION,
    ResumeValidationRuleCode.CLAIM_CLIENT_INFLATION,
    ResumeValidationRuleCode.CLAIM_SCALE_INFLATION,
    ResumeValidationRuleCode.CLAIM_OUTCOME_INFLATION,
    ResumeValidationRuleCode.CLAIM_TECHNOLOGY_INFLATION,
    ResumeValidationRuleCode.CLAIM_NEW_NUMBER,
    ResumeValidationRuleCode.CLAIM_CONTRADICTED,
    ResumeValidationRuleCode.REWRITE_NOT_MATERIAL,
    ResumeValidationRuleCode.REWRITE_FACT_MUTATION,
}


def _select_safe_rewrites(
    strategy: RoleStrategy,
    *,
    profile: CandidateProfile,
    job_description: str,
    atomic_facts: list[ResumeAtomicFact] | None = None,
    output_locale: Literal["en-US", "zh-CN"] = "en-US",
) -> tuple[RoleStrategy, dict[str, str], ResumeValidationDiagnostics]:
    """Keep each supported rewrite and restore only rejected claims to source."""

    entries = _profile_entries(profile)
    try:
        _validate_role_strategy(
            strategy,
            profile=profile,
            job_description=job_description,
            atomic_facts=atomic_facts,
            output_locale=output_locale,
        )
    except ResumeTemplateError as error:
        diagnostics = error.validation_diagnostics
        if diagnostics is None or any(
            failure.rule_code not in _SELECTIVE_REWRITE_FAILURES
            or failure.claim_id is None
            for failure in diagnostics.failures
        ):
            raise
        rejected_ids = {
            failure.claim_id
            for failure in diagnostics.failures
            if failure.claim_id is not None
        }
        fallback_fact_ids_by_source: dict[str, list[str]] = {}
        for fact in build_resume_atomic_facts(profile):
            fallback_fact_ids_by_source.setdefault(fact.profile_entry_id, []).append(
                fact.fact_id
            )
        selected_rewrites = [
            GroundedResumeBulletRewrite(
                profile_entry_id=rewrite.profile_entry_id,
                kind="REWRITE",
                text=entries[rewrite.profile_entry_id],
                source_profile_entry_ids=[rewrite.profile_entry_id],
                source_fact_ids=fallback_fact_ids_by_source[rewrite.profile_entry_id],
            )
            if rewrite.profile_entry_id in rejected_ids
            else rewrite
            for rewrite in strategy.bullet_rewrites
        ]
        selected = strategy.model_copy(
            update={
                "bullet_rewrites": selected_rewrites,
                "summary_rewrite": (
                    None if "SUMMARY" in rejected_ids else strategy.summary_rewrite
                ),
            }
        )
        _validate_role_strategy(
            selected,
            profile=profile,
            job_description=job_description,
            atomic_facts=atomic_facts,
            output_locale=output_locale,
        )
        return (
            selected,
            entries,
            replace(diagnostics, validator_status="selective_pass"),
        )
    verified_count = sum(
        rewrite.text == entries[rewrite.profile_entry_id]
        for rewrite in strategy.bullet_rewrites
    ) + int(profile.summary is not None and strategy.summary_rewrite is None)
    supported_count = len(entries) - sum(
        rewrite.text == entries[rewrite.profile_entry_id]
        for rewrite in strategy.bullet_rewrites
    ) + int(strategy.summary_rewrite is not None)
    return (
        strategy,
        entries,
        ResumeValidationDiagnostics(
            failures=(),
            candidate_count=len(entries) + int(profile.summary is not None),
            verified_count=verified_count,
            supported_count=supported_count,
            rejected_count=0,
            duplicate_count=0,
            source_span_failure_count=0,
            validator_status="accepted",
        ),
    )


def _deterministic_role_strategy_fallback(
    *,
    profile: CandidateProfile,
    job_description: str,
) -> RoleStrategy:
    """Preserve the approved Resume when model positioning is globally unsafe."""

    entries = _profile_entries(profile)
    fact_ids_by_entry: dict[str, list[str]] = {}
    for fact in build_resume_atomic_facts(profile):
        fact_ids_by_entry.setdefault(fact.profile_entry_id, []).append(fact.fact_id)
    signals = deterministic_hiring_signals(job_description)
    strategy = RoleStrategy(
        role_summary=signals[0],
        top_hiring_signals=signals,
        evidence_priority=list(entries),
        skill_priority=list(profile.skills),
        bullet_rewrites=[
            GroundedResumeBulletRewrite(
                profile_entry_id=entry_id,
                kind="REWRITE",
                text=text,
                source_profile_entry_ids=[entry_id],
                source_fact_ids=fact_ids_by_entry[entry_id],
            )
            for entry_id, text in entries.items()
        ],
        summary_rewrite=None,
        unsupported_requirements=[],
        rewrite_guidance=(
            "Preserve every operator-approved claim and use exact JD signals only for "
            "deterministic ordering."
        ),
    )
    _validate_role_strategy(
        strategy,
        profile=profile,
        job_description=job_description,
    )
    return strategy


def _build_evidence_adoption_trace(
    *,
    facts: list[ResumeAtomicFact],
    raw_strategy: RoleStrategy,
    selected_strategy: RoleStrategy,
    entries: dict[str, str],
    diagnostics: ResumeValidationDiagnostics,
    summary_rendered: bool,
) -> tuple[ResumeEvidenceAdoptionTrace, ...]:
    """Summarize fact adoption without retaining Resume or model-output bodies."""

    raw_fact_ids_by_claim = {
        rewrite.profile_entry_id: set(rewrite.source_fact_ids)
        for rewrite in raw_strategy.bullet_rewrites
    }
    if raw_strategy.summary_rewrite is not None:
        raw_fact_ids_by_claim["SUMMARY"] = set(
            raw_strategy.summary_rewrite.source_fact_ids
        )
    proposed_fact_ids = (
        set().union(*raw_fact_ids_by_claim.values())
        if raw_fact_ids_by_claim
        else set()
    )
    accepted_fact_ids: set[str] = set()
    for rewrite in selected_strategy.bullet_rewrites:
        if rewrite.text != entries[rewrite.profile_entry_id]:
            accepted_fact_ids.update(rewrite.source_fact_ids)
    if summary_rendered and selected_strategy.summary_rewrite is not None:
        accepted_fact_ids.update(selected_strategy.summary_rewrite.source_fact_ids)
    rejection_codes: dict[str, list[str]] = {}
    for failure in diagnostics.failures:
        if failure.claim_id is None:
            continue
        for fact_id in raw_fact_ids_by_claim.get(failure.claim_id, set()):
            rejection_codes.setdefault(fact_id, []).append(failure.rule_code.value)
    return tuple(
        ResumeEvidenceAdoptionTrace(
            fact_id=fact.fact_id,
            admitted=True,
            sent_to_model=True,
            proposed=fact.fact_id in proposed_fact_ids,
            accepted=fact.fact_id in accepted_fact_ids,
            rendered=fact.fact_id in accepted_fact_ids,
            rejection_rule_codes=list(
                dict.fromkeys(rejection_codes.get(fact.fact_id, []))
            ),
        )
        for fact in facts
    )


def _reorder_project_blocks_by_priority(
    body: ElementTree.Element,
    children: list[ElementTree.Element],
    priority_by_text: dict[str, int],
) -> int:
    bounds = _section_bounds(children, "PROJECT HIGHLIGHTS")
    if bounds is None:
        return 0
    start, end = bounds
    section = children[start:end]
    prefix: list[ElementTree.Element] = []
    blocks: list[list[ElementTree.Element]] = []
    current: list[ElementTree.Element] = []
    for child in section:
        text = _paragraph_text(child) if child.tag == f"{_W}p" else ""
        if text and not _is_bullet(child):
            if current:
                blocks.append(current)
            current = [child]
        elif current:
            current.append(child)
        else:
            prefix.append(child)
    if current:
        blocks.append(current)
    ranked = sorted(
        enumerate(blocks),
        key=lambda item: (
            min(
                (
                    priority_by_text.get(_paragraph_text(child), len(priority_by_text))
                    for child in item[1]
                    if child.tag == f"{_W}p" and _is_bullet(child)
                ),
                default=len(priority_by_text),
            ),
            item[0],
        ),
    )
    reordered_count = sum(index != original for index, (original, _) in enumerate(ranked))
    if reordered_count:
        _replace_range(body, start, end, prefix + [child for _, block in ranked for child in block])
    return reordered_count


def _reorder_skill_bullets_by_priority(
    body: ElementTree.Element,
    children: list[ElementTree.Element],
    skill_priority: list[str],
) -> int:
    bounds = _section_bounds(children, "TECHNICAL SKILLS")
    if bounds is None:
        return 0
    start, end = bounds
    section = children[start:end]
    bullet_positions = [index for index, child in enumerate(section) if _is_bullet(child)]
    bullets_by_text = {
        _paragraph_text(section[index]): section[index] for index in bullet_positions
    }
    if set(bullets_by_text) != set(skill_priority):
        raise ResumeTemplateError("Approved skill lines do not map cleanly to the DOCX")
    reordered = [bullets_by_text[text] for text in skill_priority]
    reordered_count = sum(
        section[position] is not bullet
        for position, bullet in zip(bullet_positions, reordered, strict=True)
    )
    if reordered_count:
        replacement = list(section)
        for position, bullet in zip(bullet_positions, reordered, strict=True):
            replacement[position] = bullet
        _replace_range(body, start, end, replacement)
    return reordered_count


def _replace_bullet_text(
    body: ElementTree.Element,
    *,
    source_text: str,
    replacement_text: str,
) -> None:
    matches = [
        child
        for child in body
        if child.tag == f"{_W}p" and _is_bullet(child) and _paragraph_text(child) == source_text
    ]
    if len(matches) != 1:
        raise ResumeTemplateError("Approved profile entry does not map uniquely to the DOCX")
    text_nodes = list(matches[0].iter(f"{_W}t"))
    if not text_nodes:
        raise ResumeTemplateError("Approved DOCX bullet has no writable text")
    text_nodes[0].text = replacement_text
    for node in text_nodes[1:]:
        node.text = ""


def _replace_summary_text(
    body: ElementTree.Element,
    *,
    source_text: str,
    replacement_text: str,
) -> bool:
    """Replace one unambiguous Summary paragraph or preserve it unchanged."""

    children = list(body)
    heading_index = next(
        (
            index
            for index, child in enumerate(children)
            if child.tag == f"{_W}p" and _heading(_paragraph_text(child)) == "SUMMARY"
        ),
        None,
    )
    if heading_index is None:
        return False
    end = next(
        (
            index
            for index, child in enumerate(children[heading_index + 1 :], heading_index + 1)
            if child.tag == f"{_W}p"
            and _heading(_paragraph_text(child)) in _SECTION_HEADINGS
        ),
        len(children),
    )
    matches = [
        child
        for child in children[heading_index + 1 : end]
        if child.tag == f"{_W}p"
        and not _is_bullet(child)
        and _paragraph_text(child) == source_text
    ]
    if len(matches) != 1:
        return False
    text_nodes = list(matches[0].iter(f"{_W}t"))
    if not text_nodes:
        return False
    text_nodes[0].text = replacement_text
    for node in text_nodes[1:]:
        node.text = ""
    return True


def _structured_candidate_artifact(
    strategy: RoleStrategy,
    *,
    status: str,
    diagnostics: ResumeValidationDiagnostics | None,
) -> dict[str, object]:
    """Keep the raw model candidate inspectable but never submission-eligible."""

    return {
        "schema_version": "1.0",
        "artifact_type": "RESUME_STRUCTURED_CANDIDATE",
        "status": status,
        "submission_status": "NOT_FOR_SUBMISSION",
        "label": (
            "REJECTED / NOT FOR SUBMISSION"
            if status == "REJECTED"
            else "NOT FOR SUBMISSION"
        ),
        "structured_candidate": strategy.model_dump(mode="json"),
        "validation_diagnostics": (
            diagnostics.as_dict() if diagnostics is not None else None
        ),
    }


def tailor_resume_docx_with_gateway(
    template: bytes,
    job_description: str,
    *,
    gateway: ModelGateway,
    tailoring_instructions: str = "",
    template_metadata: ResumeTemplateMetadata | None = None,
    support_upload: ExtractedResumeUpload | None = None,
    candidate_recorder: Callable[[dict[str, object]], None] | None = None,
    candidate_evidence_pack: CandidateEvidencePack | None = None,
    output_locale: Literal["en-US", "zh-CN"] = "en-US",
) -> TailoredDocx:
    """Use one explicit provider to rank and ground rewrites against approved bullets."""
    if not job_description.strip():
        raise ResumeTemplateError("Job description must not be empty")
    profile = extract_candidate_profile(template)
    entries = _profile_entries(profile)
    skills = _profile_skills(profile)
    entry_ids = list(entries)
    skill_ids = list(skills)
    candidate_evidence_pack = candidate_evidence_pack or build_candidate_evidence_pack(
        profile
    )
    atomic_facts = candidate_evidence_pack.atomic_facts
    fact_ids = [item.fact_id for item in atomic_facts]
    summary_paragraphs = [
        paragraph.text
        for paragraph in _section_slice(read_template_paragraphs(template), "SUMMARY")
        if paragraph.text and not paragraph.is_bullet
    ]
    include_summary = len(summary_paragraphs) == 1 and profile.summary is not None
    response_schema = _exact_role_strategy_model(
        entry_ids,
        skill_ids=skill_ids,
        fact_ids=fact_ids,
        include_summary=include_summary,
    )
    prepared = prepare_resume_gateway_payload(
        profile=profile,
        job_description=job_description,
        tailoring_instructions=tailoring_instructions,
        template_metadata=template_metadata
        or ResumeTemplateMetadata(source_format="docx"),
        support_upload=support_upload,
        output_schema=response_schema,
        candidate_evidence_pack=candidate_evidence_pack,
        output_locale=output_locale,
    )
    if [
        item.fact_id for item in prepared.payload.candidate_profile.atomic_facts
    ] != fact_ids:
        raise ResumeTemplateError(
            "Sanitized Resume facts do not match the approved atomic fact identities"
        )
    sanitized_skill_by_id = dict(
        zip(
            skill_ids,
            prepared.payload.candidate_profile.skills,
            strict=True,
        )
    )
    wire_positioning_brief = prepared.payload.positioning_brief
    positioning_brief = build_jd_positioning_brief(job_description, atomic_facts)
    deterministic_signals = wire_positioning_brief.top_hiring_signals
    model_user = prepared.payload.model_dump_json()
    model_system = (
        "Construct the strongest truthful one-page Application Resume for this JD "
        "using only approved Candidate Profile facts and the verified Candidate "
        "Evidence Pack in candidate_profile. "
        "The current Resume is a layout and work-history input, not the ceiling of "
        "what may be expressed. Return the required JSON. Rank every profile entry "
        "and every exact skill line once. "
        "skill_priority must contain every request-specific SKILL key exactly once; "
        "SKILL-01 maps to the first candidate_profile.skills item, and so on. "
        "bullet_rewrites is an object whose required PROFILE keys are fixed by the "
        "schema; provide one body for every key. Cite immutable atomic facts only through "
        "their source_fact_ids from candidate_profile.atomic_facts. Wording may change; "
        "do not repeat source sentences merely to pass validation. A REWRITE may cite "
        "facts only from its target PROFILE entry. A SYNTHESIS must cite at least two "
        "atomic facts, may combine verified Candidate Evidence assigned to its target, "
        "and must express a real component from every cited fact. Preserve the meaning "
        "of every cited fact while professionally paraphrasing it. Use SYNTHESIS to "
        "surface high-value verified engineering work, compress redundancy, and create "
        "a higher-information-density bullet. When summary_rewrite is available, "
        "synthesize it from at least two atomic facts; otherwise return null. Never add "
        "a technology, company/client, metric, ownership, scale, or outcome absent from "
        "the cited sources. Do not omit template paragraphs. Use the deterministic hiring "
        "signals below as read-only targeting "
        "context; do not return or restate them. Unsupported requirements must be exact "
        "quotes from the JD. Preserve every __SS_PRIVATE_*__ placeholder exactly. "
        "The positioning_brief in the user payload is deterministic, read-only targeting "
        "context. Follow composition_evidence_plan: use primary facts first, then "
        "secondary facts only when they add a distinct supported component. Do not "
        "force facts into a requirement with no allocation. Every factual number must "
        "appear in a cited fact's text, metric, or allowed_numbers. "
        "Write all editable narrative fields in "
        + (
            "natural professional Chinese"
            if output_locale == "zh-CN"
            else "natural professional US English"
        )
        + ". Keep proper nouns and technical names when translation would reduce precision. "
        "This is editorial composition from the same immutable FACT identities, not a "
        "translation of another locale variant. Deterministic hiring signals: "
        + json.dumps(deterministic_signals, ensure_ascii=False)
    )
    model_started = time.perf_counter()
    exact_strategy = gateway.complete(
        response_schema,
        system=model_system,
        user=model_user,
        reasoning_effort="none",
    )
    strategy = _role_strategy_from_exact(
        exact_strategy,
        entry_ids=entry_ids,
        sanitized_skill_by_id=sanitized_skill_by_id,
        fact_source_by_id={
            item.fact_id: item.profile_entry_id
            for item in prepared.payload.candidate_profile.atomic_facts
        },
        top_hiring_signals=deterministic_signals,
    )
    model_call_profile: dict[str, object] = {
        "schema_version": "1.0",
        "model_call_count": 1,
        "output_contract": "evidence_backed_resume_composition_v0.1",
        "reasoning_effort": "none",
        "gateway_wall_ms": int((time.perf_counter() - model_started) * 1000),
        "system_chars": len(model_system),
        "user_chars": len(model_user),
        "schema_chars": len(
            json.dumps(
                response_schema.model_json_schema(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        ),
    }
    provider_profile = getattr(gateway, "last_call_profile", None)
    if isinstance(provider_profile, ModelCallProfile):
        model_call_profile["provider_metrics"] = provider_profile.model_dump(
            mode="json"
        )
    validate_role_strategy_placeholders(strategy, prepared)
    strategy = restore_role_strategy(strategy, prepared.private_replacements)
    raw_strategy = strategy
    if candidate_recorder is not None:
        candidate_recorder(
            _structured_candidate_artifact(
                raw_strategy,
                status="PENDING_VALIDATION",
                diagnostics=None,
            )
        )
    role_strategy_fallback_applied = False
    role_strategy_fallback_code: str | None = None
    try:
        strategy, validated_entries, validation_diagnostics = _select_safe_rewrites(
            raw_strategy,
            profile=profile,
            job_description=job_description,
            atomic_facts=atomic_facts,
            output_locale=output_locale,
        )
    except ResumeTemplateError as error:
        if error.validation_diagnostics is None:
            if candidate_recorder is not None:
                candidate_recorder(
                    _structured_candidate_artifact(
                        raw_strategy,
                        status="REJECTED",
                        diagnostics=None,
                    )
                )
            raise
        strategy = _deterministic_role_strategy_fallback(
            profile=profile,
            job_description=job_description,
        )
        validated_entries = entries
        validation_diagnostics = replace(
            error.validation_diagnostics,
            validator_status="fallback_pass",
        )
        role_strategy_fallback_applied = True
        role_strategy_fallback_code = "ROLE_STRATEGY_TRUTH_REJECTED"
    if candidate_recorder is not None:
        candidate_recorder(
            _structured_candidate_artifact(
                raw_strategy,
                status=(
                    "REJECTED"
                    if role_strategy_fallback_applied
                    or validation_diagnostics.rejected_count
                    else "VALIDATED"
                ),
                diagnostics=validation_diagnostics,
            )
        )
    members, document = _read_package(template)
    root, body = _parse_document(document)
    source_paragraphs = [
        _paragraph_text(child) for child in body if child.tag == f"{_W}p" and _paragraph_text(child)
    ]
    rank_by_id = {entry_id: index for index, entry_id in enumerate(strategy.evidence_priority)}
    priority_by_text = {
        validated_entries[entry_id]: rank for entry_id, rank in rank_by_id.items()
    }
    project_count = _reorder_project_blocks_by_priority(body, list(body), priority_by_text)
    skill_count = _reorder_skill_bullets_by_priority(
        body, list(body), strategy.skill_priority
    )
    summary_rewritten = False
    if strategy.summary_rewrite is not None and profile.summary is not None:
        summary_rewritten = _replace_summary_text(
            body,
            source_text=profile.summary,
            replacement_text=strategy.summary_rewrite.text,
        )
        if not summary_rewritten:
            strategy = strategy.model_copy(update={"summary_rewrite": None})
            validation_diagnostics = replace(
                validation_diagnostics,
                verified_count=validation_diagnostics.verified_count + 1,
                supported_count=max(0, validation_diagnostics.supported_count - 1),
                rejected_count=validation_diagnostics.rejected_count + 1,
                validator_status="selective_pass",
            )
    rewrite_by_id = {item.profile_entry_id: item.text for item in strategy.bullet_rewrites}
    for entry_id, source_text in validated_entries.items():
        _replace_bullet_text(
            body,
            source_text=source_text,
            replacement_text=rewrite_by_id[entry_id],
        )
    _localize_resume_headings(body, output_locale)
    _remove_trailing_empty_paragraphs(body)
    if len(source_paragraphs) != sum(
        1 for child in body if child.tag == f"{_W}p" and _paragraph_text(child)
    ):
        raise ResumeTemplateError("AI tailoring changed the DOCX paragraph structure")
    tailored_document = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        for info, content in members:
            archive.writestr(
                info, tailored_document if info.filename == _DOCUMENT_PART else content
            )
    output = target.getvalue()
    evidence_adoption = _build_evidence_adoption_trace(
        facts=atomic_facts,
        raw_strategy=raw_strategy,
        selected_strategy=strategy,
        entries=entries,
        diagnostics=validation_diagnostics,
        summary_rendered=summary_rewritten,
    )
    return TailoredDocx(
        content=output,
        template_sha256=hashlib.sha256(template).hexdigest(),
        output_sha256=hashlib.sha256(output).hexdigest(),
        project_blocks_reordered=project_count,
        skill_bullets_reordered=skill_count,
        source_paragraph_count=len(source_paragraphs),
        claims_preserved=True,
        grounded_rewrites=validation_diagnostics.supported_count,
        synthesized_rewrites=sum(
            rewrite.kind == "SYNTHESIS"
            and rewrite.text != validated_entries[rewrite.profile_entry_id]
            for rewrite in strategy.bullet_rewrites
        ),
        summary_rewritten=summary_rewritten,
        rejected_rewrites=validation_diagnostics.rejected_count,
        generation_mode="ai",
        provider=gateway.descriptor.provider.value,
        model=gateway.descriptor.model,
        output_locale=output_locale,
        model_call_profile=model_call_profile,
        validation_diagnostics=validation_diagnostics,
        role_strategy=strategy,
        candidate_evidence_pack=candidate_evidence_pack,
        positioning_brief=positioning_brief,
        evidence_adoption=evidence_adoption,
        role_strategy_fallback_applied=role_strategy_fallback_applied,
        role_strategy_fallback_code=role_strategy_fallback_code,
    )


def request_resume_expert_review(
    tailored: TailoredDocx,
    *,
    profile: CandidateProfile,
    gateway: ModelGateway,
) -> ResumeExpertReviewResult:
    """Send only hiring signals, source fragments, and the current draft."""

    strategy = tailored.role_strategy
    if strategy is None:
        raise ResumeTemplateError(
            "Expert review requires an evidence-grounded AI draft"
        )
    approved_entries = _profile_entries(profile)
    atomic_fact_by_id = {
        item.fact_id: item
        for item in (
            tailored.candidate_evidence_pack.atomic_facts
            if tailored.candidate_evidence_pack is not None
            else build_resume_atomic_facts(profile)
        )
    }
    rewrite_by_id = {
        rewrite.profile_entry_id: rewrite for rewrite in strategy.bullet_rewrites
    }
    if set(rewrite_by_id) != set(approved_entries):
        raise ResumeTemplateError("Expert review cannot resolve the approved draft")
    return gateway.complete(
        ResumeExpertReviewResult,
        system=(
            "Review a resume as a hiring manager. Return patches only. Improve clarity, "
            "technical accuracy, ATS alignment, ordering emphasis, and one-page density "
            "without adding scope, ownership, technologies, metrics, or outcomes. Every "
            "patch must identify the exact PROFILE key and SHA-256 of its current text. "
            "List every proposed new factual claim explicitly in new_factual_claims; the "
            "application will reject any non-empty list. Do not request or infer other "
            "local evidence."
        ),
        user=json.dumps(
            {
                "hiring_signals": strategy.top_hiring_signals,
                "evidence_bundle": {
                    entry_id: {
                        "source_profile_entry_ids": rewrite_by_id[
                            entry_id
                        ].source_profile_entry_ids,
                        "source_facts": [
                            atomic_fact_by_id[fact_id].model_dump(mode="json")
                            for fact_id in rewrite_by_id[entry_id].source_fact_ids
                        ],
                        "approved_source_sha256": hashlib.sha256(
                            approved_entries[entry_id].encode("utf-8")
                        ).hexdigest(),
                    }
                    for entry_id in strategy.evidence_priority
                },
                "draft_resume_bullets": {
                    entry_id: rewrite_by_id[entry_id].text
                    for entry_id in strategy.evidence_priority
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        reasoning_effort="low",
    )


def apply_resume_expert_review(
    tailored: TailoredDocx,
    *,
    profile: CandidateProfile,
    job_description: str,
    review: ResumeExpertReviewResult,
    expert_provider: str,
    expert_model: str | None,
) -> TailoredDocx:
    """Apply a patch-only expert review and re-run the original fact validator."""

    strategy = tailored.role_strategy
    if strategy is None:
        raise ResumeTemplateError(
            "Expert review requires an evidence-grounded AI draft"
        )
    approved_entries = _profile_entries(profile)
    rewrite_by_id = {
        rewrite.profile_entry_id: rewrite for rewrite in strategy.bullet_rewrites
    }
    if set(rewrite_by_id) != set(approved_entries):
        raise ResumeTemplateError("Expert review cannot resolve the approved draft")
    allowed_ids = set(approved_entries)
    if any(
        patch.profile_entry_id not in allowed_ids
        or patch.before_sha256
        != hashlib.sha256(
            rewrite_by_id[patch.profile_entry_id].text.encode("utf-8")
        ).hexdigest()
        for patch in review.patches
    ):
        raise ResumeTemplateError("Expert review patch does not match the approved draft")
    if any(patch.new_factual_claims for patch in review.patches):
        raise ResumeTemplateError("Expert review proposed unsupported factual claims")
    if any(
        entry_id not in allowed_ids
        for entry_id in review.omitted_high_value_profile_entry_ids
    ):
        raise ResumeTemplateError("Expert review referenced an unknown approved fact")

    patch_by_id = {patch.profile_entry_id: patch for patch in review.patches}
    updated_rewrites = [
        rewrite.model_copy(
            update={"text": patch_by_id[rewrite.profile_entry_id].after}
        )
        if rewrite.profile_entry_id in patch_by_id
        else rewrite
        for rewrite in strategy.bullet_rewrites
    ]
    reviewed_strategy = strategy.model_copy(update={"bullet_rewrites": updated_rewrites})
    _validate_role_strategy(
        reviewed_strategy,
        profile=profile,
        job_description=job_description,
        atomic_facts=(
            tailored.candidate_evidence_pack.atomic_facts
            if tailored.candidate_evidence_pack is not None
            else None
        ),
    )

    members, document = _read_package(tailored.content)
    root, body = _parse_document(document)
    source_paragraph_count = sum(
        1 for child in body if child.tag == f"{_W}p" and _paragraph_text(child)
    )
    for entry_id, patch in patch_by_id.items():
        _replace_bullet_text(
            body,
            source_text=rewrite_by_id[entry_id].text,
            replacement_text=patch.after,
        )
    if source_paragraph_count != sum(
        1 for child in body if child.tag == f"{_W}p" and _paragraph_text(child)
    ):
        raise ResumeTemplateError("Expert review changed the DOCX paragraph structure")
    reviewed_document = ElementTree.tostring(
        root, encoding="utf-8", xml_declaration=True
    )
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        for info, content in members:
            archive.writestr(
                info,
                reviewed_document if info.filename == _DOCUMENT_PART else content,
            )
    output = target.getvalue()
    return replace(
        tailored,
        content=output,
        output_sha256=hashlib.sha256(output).hexdigest(),
        grounded_rewrites=len(reviewed_strategy.bullet_rewrites),
        role_strategy=reviewed_strategy,
        expert_review=review,
        expert_review_attempted=True,
        expert_review_skipped_code=None,
        expert_rewrites=len(review.patches),
        expert_provider=expert_provider,
        expert_model=expert_model,
    )


def tailor_resume_docx(template: bytes, job_description: str) -> TailoredDocx:
    """Return a style-preserving DOCX whose source claims are only reordered."""
    if not job_description.strip():
        raise ResumeTemplateError("Job description must not be empty")
    members, document = _read_package(template)
    root, body = _parse_document(document)
    source_paragraphs = [
        _paragraph_text(child) for child in body if child.tag == f"{_W}p" and _paragraph_text(child)
    ]
    job_terms = _terms(job_description)
    children = list(body)
    project_count = _reorder_project_blocks(body, children, job_terms)
    children = list(body)
    skill_count = _reorder_skill_bullets(body, children, job_terms)
    output_paragraphs = [
        _paragraph_text(child) for child in body if child.tag == f"{_W}p" and _paragraph_text(child)
    ]
    claims_preserved = sorted(source_paragraphs) == sorted(output_paragraphs)
    if not claims_preserved:
        raise ResumeTemplateError("Tailoring changed candidate text; output was rejected")
    tailored_document = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        for info, content in members:
            output_content = tailored_document if info.filename == _DOCUMENT_PART else content
            archive.writestr(info, output_content)
    output = target.getvalue()
    return TailoredDocx(
        content=output,
        template_sha256=hashlib.sha256(template).hexdigest(),
        output_sha256=hashlib.sha256(output).hexdigest(),
        project_blocks_reordered=project_count,
        skill_bullets_reordered=skill_count,
        source_paragraph_count=len(source_paragraphs),
        claims_preserved=claims_preserved,
    )
