from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReferenceSourceKind(StrEnum):
    MANUAL_TEXT = "manual_text"
    LOCAL_VIDEO = "local_video"


class ReferenceAsset(_StrictModel):
    """Private metadata for an operator-supplied inspiration source."""

    reference_id: str = Field(pattern=r"^reference-[a-f0-9]{16}$")
    source_kind: ReferenceSourceKind = ReferenceSourceKind.MANUAL_TEXT
    title: str | None = Field(default=None, max_length=180)
    author: str | None = Field(default=None, max_length=120)
    raw_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    raw_character_count: int = Field(ge=0, le=200_000)
    source_filename: str | None = Field(default=None, max_length=240)
    source_bytes: int | None = Field(default=None, ge=1, le=200 * 1024 * 1024)
    duration_seconds: float | None = Field(default=None, gt=0, le=3_600)
    width: int | None = Field(default=None, gt=0, le=16_384)
    height: int | None = Field(default=None, gt=0, le=16_384)
    frames_per_second: float | None = Field(default=None, gt=0, le=240)
    has_audio: bool | None = None
    transcript_status: Literal["not_applicable", "complete", "not_available"] = (
        "not_applicable"
    )
    transcript_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    raw_retained_private: Literal[True] = True
    external_facts_authorized: Literal[False] = False

    @model_validator(mode="after")
    def source_metadata_matches_kind(self) -> ReferenceAsset:
        if self.source_kind is ReferenceSourceKind.MANUAL_TEXT:
            if self.raw_character_count < 40:
                raise ValueError("Manual reference text requires at least 40 characters")
            if self.source_filename is not None or self.source_bytes is not None:
                raise ValueError("Manual reference text cannot include video metadata")
        else:
            required = (
                self.source_filename,
                self.source_bytes,
                self.duration_seconds,
                self.width,
                self.height,
                self.frames_per_second,
                self.has_audio,
            )
            if any(value is None for value in required):
                raise ValueError("Local video reference metadata is incomplete")
            if self.transcript_status == "complete" and self.transcript_sha256 is None:
                raise ValueError("A completed transcript requires its SHA-256")
        return self


class ReferenceContentPattern(_StrictModel):
    content_shape: str = Field(min_length=1, max_length=120)
    thesis_shape: str = Field(min_length=1, max_length=120)
    information_density: Literal["light", "moderate", "dense"]
    external_examples_reusable: Literal[False] = False


class ReferenceStructurePattern(_StrictModel):
    hook: str = Field(min_length=1, max_length=120)
    progression: list[str] = Field(min_length=2, max_length=6)
    climax: str = Field(min_length=1, max_length=120)
    ending: str = Field(min_length=1, max_length=120)
    cta: str = Field(min_length=1, max_length=120)


class ReferenceLanguagePattern(_StrictModel):
    sentence_length: Literal["short", "mixed", "long"]
    tone: str = Field(min_length=1, max_length=120)
    rhetorical_devices: list[str] = Field(default_factory=list, max_length=6)
    tension: str = Field(min_length=1, max_length=120)


class ReferenceVideoPattern(_StrictModel):
    estimated_duration_seconds: int = Field(ge=1, le=3_600)
    shot_cadence: Literal["fast", "moderate", "steady"]
    visual_elements: list[str] = Field(default_factory=list, max_length=8)
    captions: Literal["observed", "not_observed", "unknown"] = "unknown"
    transitions: Literal["observed", "not_observed", "unknown"] = "unknown"
    measured_duration_seconds: float | None = Field(default=None, gt=0, le=3_600)
    shot_count: int | None = Field(default=None, ge=1, le=10_000)
    shot_timestamps_seconds: list[float] = Field(default_factory=list, max_length=256)
    shot_durations_seconds: list[float] = Field(default_factory=list, max_length=256)
    average_shot_duration_seconds: float | None = Field(default=None, gt=0, le=3_600)
    cuts_per_minute: float | None = Field(default=None, ge=0, le=3_600)
    opening_segment_seconds: float | None = Field(default=None, gt=0, le=3_600)
    ending_segment_seconds: float | None = Field(default=None, gt=0, le=3_600)
    aspect_ratio: str | None = Field(default=None, max_length=32)
    frame_sample_count: int = Field(default=0, ge=0, le=24)
    has_audio: bool | None = None
    dominant_visual_type: Literal[
        "talking_head", "screen", "diagram", "b_roll", "UNKNOWN"
    ] = "UNKNOWN"


class ReferenceAudiencePattern(_StrictModel):
    target_persona: Literal["not_inferred"] = "not_inferred"
    pain_point: Literal["not_inferred"] = "not_inferred"
    sophistication: Literal["not_inferred"] = "not_inferred"
    expected_reaction: str = Field(min_length=1, max_length=120)


class ReferencePerformancePattern(_StrictModel):
    metrics_available: Literal[False] = False
    recurring_comment_themes: list[str] = Field(default_factory=list, max_length=8)


class ContentPattern(_StrictModel):
    """High-level expression guidance. It is never factual authority."""

    pattern_id: str = Field(pattern=r"^pattern-[a-f0-9]{16}$")
    reference_id: str = Field(pattern=r"^reference-[a-f0-9]{16}$")
    content: ReferenceContentPattern
    structure: ReferenceStructurePattern
    language: ReferenceLanguagePattern
    video: ReferenceVideoPattern
    audience: ReferenceAudiencePattern
    performance: ReferencePerformancePattern = Field(
        default_factory=ReferencePerformancePattern
    )
    facts_source: Literal["operator_claim_ledger_only"] = (
        "operator_claim_ledger_only"
    )
    distinctive_expression_reuse_allowed: Literal[False] = False
    unknowns: list[str] = Field(default_factory=list, max_length=12)


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？])\s*|\n+")
_ENGLISH_WORD = re.compile(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)?")
_CJK_CLAUSE = re.compile(r"[\u3400-\u9fff]{24,}")


def normalize_reference_text(raw: str) -> str:
    normalized = "\n".join(
        line.rstrip() for line in raw.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    ).strip()
    if len(normalized) < 40:
        raise ValueError("Reference text must contain at least 40 characters")
    if len(normalized) > 20_000:
        raise ValueError("Reference text must not exceed 20,000 characters")
    return normalized


def _hook_pattern(opening: str) -> str:
    lowered = opening.casefold()
    if "?" in opening or "？" in opening:
        return "question-led hook"
    if re.search(r"\b(?:but|however|instead|not)\b|但是|却|不是|反而", lowered):
        return "contrarian contrast"
    if re.search(r"\b(?:i|my)\b|我", lowered) and re.search(
        r"\b(?:failed|mistake|bug|problem)\b|失败|错误|问题|踩坑", lowered
    ):
        return "personal problem or failure"
    if re.search(r"\d", opening):
        return "specific-number opening"
    return "direct assertion"


def _cta_pattern(ending: str) -> tuple[str, str]:
    lowered = ending.casefold()
    if "?" in ending or "？" in ending:
        return "soft discussion prompt", "invite reflection or discussion"
    if re.search(r"\b(?:follow|subscribe|comment|reply)\b|关注|评论|回复", lowered):
        return "audience response request", "invite an audience response"
    if re.search(r"\b(?:try|use|start|build)\b|试试|开始|使用", lowered):
        return "concrete action prompt", "encourage one concrete action"
    return "no explicit CTA observed", "leave the reader with the central takeaway"


def _visual_elements(notes: str) -> list[str]:
    lowered = notes.casefold()
    candidates = (
        (r"screen|录屏|屏幕", "screen recording"),
        (r"screenshot|截图", "screenshots"),
        (r"talking head|口播|真人", "talking head"),
        (r"diagram|architecture|图表|架构图", "diagram"),
        (r"code|terminal|代码|终端", "code or terminal close-up"),
        (r"b[- ]?roll|素材", "B-roll"),
        (r"subtitle|caption|字幕", "large captions"),
    )
    return [label for pattern, label in candidates if re.search(pattern, lowered)][:8]


def extract_content_pattern(
    raw_text: str,
    *,
    title: str = "",
    author: str = "",
    visual_notes: str = "",
) -> tuple[ReferenceAsset, ContentPattern, str]:
    """Distill reusable presentation patterns without promoting external facts."""

    normalized = normalize_reference_text(raw_text)
    raw_sha256 = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    reference_id = f"reference-{raw_sha256[:16]}"
    sentences = [
        sentence.strip()
        for sentence in _SENTENCE_SPLIT.split(normalized)
        if sentence.strip()
    ]
    if not sentences:
        sentences = [normalized]
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", normalized) if part.strip()]
    average_sentence = sum(len(sentence) for sentence in sentences) / len(sentences)
    sentence_length: Literal["short", "mixed", "long"]
    if average_sentence <= 55:
        sentence_length = "short"
    elif average_sentence <= 115:
        sentence_length = "mixed"
    else:
        sentence_length = "long"
    density: Literal["light", "moderate", "dense"]
    density = "light" if len(sentences) <= 5 else "moderate" if len(sentences) <= 12 else "dense"
    cadence: Literal["fast", "moderate", "steady"]
    cadence = (
        "fast"
        if sentence_length == "short"
        else "moderate"
        if sentence_length == "mixed"
        else "steady"
    )

    opening = " ".join(sentences[:2])
    ending = sentences[-1]
    hook = _hook_pattern(opening)
    cta, expected_reaction = _cta_pattern(ending)
    lowered = normalized.casefold()
    rhetorical_devices: list[str] = []
    if "?" in normalized or "？" in normalized:
        rhetorical_devices.append("rhetorical question")
    if re.search(r"\b(?:but|however|instead)\b|但是|却|反而", lowered):
        rhetorical_devices.append("contrast")
    if re.search(r"\b(?:i|my)\b|我", lowered):
        rhetorical_devices.append("first-person framing")
    if re.search(r"(?:^|\n)\s*[0-9]+[.)、]", normalized):
        rhetorical_devices.append("numbered progression")
    tone = (
        "peer-to-peer reflective"
        if re.search(r"\b(?:i|my)\b|我", lowered)
        else "direct explanatory"
    )
    problem_language = bool(
        re.search(r"\b(?:failed|mistake|bug|problem|root cause)\b|失败|错误|问题|根因", lowered)
    )
    progression = (
        ["hook", "personal context", "investigation", "lesson", "next action"]
        if len(paragraphs) >= 4
        else ["hook", "development", "lesson or action"]
    )
    climax = (
        "surprising root-cause turn"
        if re.search(r"\b(?:root cause|turned out|realized)\b|根因|原来|发现", lowered)
        else "central insight"
    )
    english_word_count = len(_ENGLISH_WORD.findall(normalized))
    cjk_count = len(re.findall(r"[\u3400-\u9fff]", normalized))
    duration = round(max(english_word_count / 2.5, cjk_count / 4.0, 20))
    duration = max(20, min(180, duration))
    visuals = _visual_elements(visual_notes)
    captions: Literal["observed", "not_observed", "unknown"] = (
        "observed" if "large captions" in visuals else "unknown"
    )
    asset = ReferenceAsset(
        reference_id=reference_id,
        title=title.strip() or None,
        author=author.strip() or None,
        raw_sha256=raw_sha256,
        raw_character_count=len(normalized),
    )
    content = ReferenceContentPattern(
        content_shape="problem-to-insight" if problem_language else "idea-to-takeaway",
        thesis_shape=(
            "personal discovery" if tone.startswith("peer") else "direct explanation"
        ),
        information_density=density,
    )
    structure = ReferenceStructurePattern(
        hook=hook,
        progression=progression,
        climax=climax,
        ending=(
            "action-oriented"
            if cta != "no explicit CTA observed"
            else "takeaway-led"
        ),
        cta=cta,
    )
    language = ReferenceLanguagePattern(
        sentence_length=sentence_length,
        tone=tone,
        rhetorical_devices=rhetorical_devices,
        tension=(
            "problem-resolution tension" if problem_language else "low explicit tension"
        ),
    )
    video = ReferenceVideoPattern(
        estimated_duration_seconds=duration,
        shot_cadence=cadence,
        visual_elements=visuals,
        captions=captions,
        transitions="unknown",
    )
    audience = ReferenceAudiencePattern(expected_reaction=expected_reaction)
    unknowns = [
        "Audience persona and sophistication were not inferred from pasted text.",
        "Likes, comments, saves, shares, and comment themes were not supplied.",
        "Visual details are unknown unless the operator added visual notes.",
    ]
    pattern_payload = {
        "reference_id": reference_id,
        "content": content.model_dump(mode="json"),
        "structure": structure.model_dump(mode="json"),
        "language": language.model_dump(mode="json"),
        "video": video.model_dump(mode="json"),
        "audience": audience.model_dump(mode="json"),
        "unknowns": unknowns,
    }
    pattern_sha256 = hashlib.sha256(
        json.dumps(
            pattern_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    pattern = ContentPattern(
        pattern_id=f"pattern-{pattern_sha256[:16]}",
        reference_id=reference_id,
        content=content,
        structure=structure,
        language=language,
        video=video,
        audience=audience,
        unknowns=unknowns,
    )
    return asset, pattern, normalized


def reject_distinctive_reference_reuse(
    reference_text: str,
    generated_values: Sequence[str],
) -> None:
    """Fail closed when a generated artifact repeats a long reference expression."""

    generated = "\n".join(generated_values).casefold()
    words = [word.casefold() for word in _ENGLISH_WORD.findall(reference_text)]
    for index in range(max(0, len(words) - 7)):
        phrase = " ".join(words[index : index + 8])
        if phrase and phrase in " ".join(_ENGLISH_WORD.findall(generated)).casefold():
            raise ValueError("Generated content reuses a distinctive reference phrase")
    compact_generated = re.sub(r"[^\u3400-\u9fff]", "", generated)
    for clause in _CJK_CLAUSE.findall(reference_text):
        for index in range(len(clause) - 23):
            if clause[index : index + 24] in compact_generated:
                raise ValueError("Generated content reuses a distinctive reference phrase")
