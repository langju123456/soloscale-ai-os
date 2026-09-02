from __future__ import annotations

import hashlib
import json
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError

from soloscale.content_models import (
    ClaimStatus,
    ContentBrief,
    ContentClaim,
    ContentDrafts,
    ContentReviewDecision,
    ContentReviewReceipt,
    ContentRun,
    StoryboardScene,
    StoryLocaleVariant,
)
from soloscale.editorial_models import EditorialRole, ProviderIdentity, ProviderKind
from soloscale.editorial_pipeline import make_provenance
from soloscale.evidence_agent import Reasoner
from soloscale.evidence_capture import capture_assets
from soloscale.evidence_hub import EvidenceHub
from soloscale.model_gateway import (
    GatewayConfigurationState,
    ModelCallProfile,
    ModelGateway,
    ModelGatewayInvalidResponse,
    ModelGatewayNotConfigured,
    ModelGatewayTransportError,
    ModelProviderId,
    OllamaModelGateway,
)
from soloscale.reference_intelligence import (
    ReferenceSourceKind,
    normalize_reference_text,
    reject_distinctive_reference_reuse,
)
from soloscale.resume_workspace import (
    ResumeWorkspaceStorageError,
    _atomic_private_write,
    _ensure_private_directory,
    _reject_symlink_ancestry,
)

_CONTENT_RUN_ID = re.compile(r"content-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{10}")
_PRIVATE_PATH = re.compile(
    r"(?:file://|(?:^|[\s'\"])/(?:Users|home|private|var|tmp)/|[A-Za-z]:\\)", re.I
)
_SECRET = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{12,}|AKIA[A-Z0-9]{12,}|"
    r"Bearer\s+[A-Za-z0-9._~+/=-]{12,})"
)
_CLAIM_ID = re.compile(r"CLAIM-[0-9]{2}")
_CLAIM_STATUS_NAME = r"(?:VERIFIED|OBSERVED|HYPOTHESIS|PLANNED)"
_CLAIM_MARKER_TOKEN = re.compile(
    rf"\[?{_CLAIM_STATUS_NAME} · CLAIM-[0-9]{{2}}\]?|\[CLAIM-[0-9]{{2}}\]"
)
_CLAIM_MARKER_LINE_PREFIX = re.compile(
    rf"(?m)^[ \t]*{_CLAIM_STATUS_NAME} · CLAIM-[0-9]{{2}} — ?"
)
_INTERNAL_LABEL_LINE = re.compile(
    r"(?m)^[ \t]*(?:- |\* )?(?:Claim anchors|Facts source|Reference pattern applied):[^\n]*[ \t]*$"
)
_CLAIM_MAP_LINE = re.compile(r"(?m)^[ \t]*(?:- |\* )?CLAIM-[0-9]{2}: [^\n]*$")
_INTERNAL_LIMIT_INLINE = re.compile(
    r"(?:^|[\s·•–—-]+)(?:Limit|边界)\s*:[^\n]*"
)
_INTERNAL_LIMIT_LABEL_LINE = re.compile(
    r"(?m)^[ \t]*(?:- |\* )?(?:Limit|边界)\s*:[^\n]*[ \t]*$"
)
_INTERNAL_BOUNDARY_HEADING = re.compile(
    r"(?m)^[ \t]*(?:Explicit boundaries?|明确边界)\s*:[ \t]*$"
)
_INTERNAL_EVIDENCE_MAP_HEADING = re.compile(
    r"(?m)^[ \t]*(?:Evidence map|证据索引)[^\n]*[ \t]*$"
)
_INTERNAL_PROOF_LABEL_LINE = re.compile(
    r"(?m)^[ \t]*(?:- |\* )?(?:Proof links?|证据链接)[^\n]*[ \t]*$"
)
_SEPARATOR_EDGE = re.compile(r"^[\s·•–—-]+|[\s·•–—-]+$")
_OLLAMA_PROMPT_VERSION = "content-ollama-writer-v1"
_OLLAMA_SYSTEM_PROMPT = """You are the SoloScale evidence-bound content writer.
Return only JSON matching the supplied schema. Write one canonical story and derive a
LinkedIn draft, X thread, standalone X post, blog draft, 4–6 minute YouTube script,
short-video script, and storyboard from that same story in the requested language.
Create a native editorial adaptation for the requested locale. Do not translate another
locale's finished copy literally, and do not mix Chinese and English narration in one variant.

Truth rules:
- Use only facts present in the supplied claim ledger. Never invent numbers, tools,
  results, employers, customers, user feedback, or publication outcomes.
- Every supplied claim must appear in the canonical story, LinkedIn, X thread, blog,
  and video script with the exact marker
  `STATUS · CLAIM-ID`, for example `VERIFIED · CLAIM-01`.
- The standalone X post must use at least the first supplied claim marker and may not
  introduce an unknown claim ID.
- Preserve each claim's VERIFIED, OBSERVED, HYPOTHESIS, or PLANNED classification.
- Preserve stated limits and evidence gaps. Do not turn them into positive claims.
- A supplied ContentPattern is expression guidance only. Use it for high-level structure,
  pacing, tone, and presentation. Facts must still come only from the claim ledger.
- Never reuse distinctive phrases, examples, or unique creative expression from a
  reference. The raw reference is deliberately not included in this request.
- Use the supplied CTA verbatim. Do not include private paths, credentials, or new URLs.
- Number each X post exactly `N/TOTAL ` and keep every complete post at 280 characters
  or fewer.
- Storyboard claim_ids may contain only supplied claim IDs. Include every claim ID in
  at least one scene; a final CTA scene may have no claim_ids.
- Human review is still required. Do not claim that anything was published.
"""


class _OllamaStoryboardScene(BaseModel):
    """Ollama-compatible transport schema; strict limits are enforced afterwards."""

    model_config = ConfigDict(extra="forbid")

    id: str
    start_second: int
    end_second: int
    purpose: str
    visual: str
    voiceover: str
    on_screen_text: str
    claim_ids: list[str]


class _OllamaContentDrafts(BaseModel):
    """Avoid JSON-Schema constraints unsupported by Ollama's grammar parser."""

    model_config = ConfigDict(extra="forbid")

    canonical_story: str
    linkedin: str
    x_thread: list[str]
    x_post: str
    blog: str
    youtube_script: str
    video_script: str
    storyboard: list[_OllamaStoryboardScene]
_DOWNLOADS = {
    "canonical-story.md": "15_canonical_story.md",
    "linkedin.md": "02_linkedin.md",
    "x-thread.md": "03_x_thread.md",
    "x-post.md": "03_x_post.md",
    "blog.md": "16_blog.md",
    "youtube-script.md": "20_youtube_script.md",
    "video-script.md": "04_video_script.md",
    "storyboard.json": "05_storyboard.json",
    "creator-video.mp4": "10_creator_video.mp4",
    "youtube-video.mp4": "21_creator_video_youtube.mp4",
    "video-thumbnail.png": "22_creator_video_thumbnail.png",
    "heygen-handoff.json": "23_heygen_handoff.json",
    "avatar-segments.json": "24_avatar_segments.json",
    "video-subtitles.srt": "25_creator_video_subtitles.srt",
    "distribution-package.json": "26_distribution_package.json",
    "youtube-upload.json": "27_youtube_upload.json",
    "media-quality-review.json": "28_media_quality_review.json",
    "creator-video-render.json": "11_creator_video_render.json",
    "publish-pack.json": "06_publish_pack.json",
    "provenance.json": "07_provenance.json",
    "editorial-provenance.json": "12_editorial_provenance.json",
    "evidence-context.json": "14_evidence_context.json",
    "reference-pattern.json": "18_content_pattern.json",
}

_REVIEW_ARTIFACTS = {
    "canonical_story": "canonical-story.md",
    "linkedin": "linkedin.md",
    "x_thread": "x-thread.md",
    "x_post": "x-post.md",
    "blog": "blog.md",
    "youtube_script": "youtube-script.md",
    "video_script": "video-script.md",
}
_DOWNLOAD_REVIEW_KEYS = {
    "canonical-story.md": "canonical_story",
    "linkedin.md": "linkedin",
    "x-thread.md": "x_thread",
    "x-post.md": "x_post",
    "blog.md": "blog",
    "youtube-script.md": "youtube_script",
    "video-script.md": "video_script",
}


class ContentWorkspaceError(ValueError):
    """Raised when a content draft would cross the local review boundary."""


def parse_claim_ledger(raw: str) -> list[ContentClaim]:
    """Parse `STATUS | claim | receipt | limits` lines into a strict claim ledger."""

    claims: list[ContentClaim] = []
    for line_number, raw_line in enumerate(raw.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split("|", maxsplit=3)]
        if len(parts) < 2:
            raise ContentWorkspaceError(
                f"Claim line {line_number} must use STATUS | claim | receipt | limits"
            )
        try:
            status = ClaimStatus(parts[0].upper())
        except ValueError:
            raise ContentWorkspaceError(f"Claim line {line_number} has an unknown status") from None
        receipt = parts[2] if len(parts) >= 3 and parts[2] else None
        limits = parts[3] if len(parts) >= 4 and parts[3] else None
        try:
            claim = ContentClaim(
                id=f"CLAIM-{len(claims) + 1:02d}",
                text=parts[1],
                status=status,
                receipt=receipt,
                limits=limits,
            )
        except ValidationError as exc:
            raise ContentWorkspaceError(
                f"Claim line {line_number} does not satisfy the content contract"
            ) from exc
        claims.append(claim)
    if not claims:
        raise ContentWorkspaceError("At least one claim is required")
    if len(claims) > 8:
        raise ContentWorkspaceError("A content run accepts at most 8 claims")
    return claims


def _reject_private_output(value: str, *, field: str) -> None:
    if _PRIVATE_PATH.search(value):
        raise ContentWorkspaceError(f"{field} contains a private absolute path")
    if _SECRET.search(value):
        raise ContentWorkspaceError(f"{field} contains a credential-like value")


def _validate_public_fields(brief: ContentBrief) -> None:
    values = {
        "topic": brief.topic,
        "audience": brief.audience,
        "call_to_action": brief.call_to_action,
        "source_label": brief.source_label,
    }
    for claim in brief.claims:
        values[f"{claim.id}.text"] = claim.text
        values[f"{claim.id}.receipt"] = claim.receipt or ""
        values[f"{claim.id}.limits"] = claim.limits or ""
    for index, gap in enumerate(brief.evidence_gaps):
        values[f"evidence gap {index + 1}"] = gap
    if brief.reference_asset is not None:
        values["reference metadata"] = _canonical_json(
            brief.reference_asset.model_dump(mode="json")
        )
    if brief.content_pattern is not None:
        values["reference pattern"] = _canonical_json(
            brief.content_pattern.model_dump(mode="json")
        )
    for field, value in values.items():
        _reject_private_output(value, field=field)


def _validate_reference_source(
    brief: ContentBrief,
    reference_source_text: str | None,
) -> str | None:
    if brief.reference_asset is None or brief.content_pattern is None:
        if reference_source_text is not None:
            raise ContentWorkspaceError(
                "Reference source text requires a ReferenceAsset and ContentPattern"
            )
        return None
    if brief.reference_asset.source_kind is ReferenceSourceKind.LOCAL_VIDEO:
        if reference_source_text is not None:
            raise ContentWorkspaceError(
                "Raw local-video transcripts must remain in the private reference library"
            )
        return None
    if reference_source_text is None:
        raise ContentWorkspaceError(
            "Reference source text is required for a new reference-guided run"
        )
    try:
        normalized = normalize_reference_text(reference_source_text)
    except ValueError as exc:
        raise ContentWorkspaceError(str(exc)) from exc
    _reject_private_output(normalized, field="reference source")
    reference_sha256 = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    if reference_sha256 != brief.reference_asset.raw_sha256:
        raise ContentWorkspaceError("Reference source does not match its metadata")
    return normalized


def _status_heading(status: ClaimStatus, language: str) -> str:
    if language == "中文":
        return {
            ClaimStatus.VERIFIED: "已验证",
            ClaimStatus.OBSERVED: "已观察",
            ClaimStatus.HYPOTHESIS: "待验证假设",
            ClaimStatus.PLANNED: "下一步计划",
        }[status]
    return {
        ClaimStatus.VERIFIED: "Verified",
        ClaimStatus.OBSERVED: "Observed",
        ClaimStatus.HYPOTHESIS: "Hypothesis",
        ClaimStatus.PLANNED: "Planned",
    }[status]


def _claim_line(claim: ContentClaim, language: str) -> str:
    del language
    return f"- [{claim.status.value} · {claim.id}] {claim.text}"


def _render_linkedin(brief: ContentBrief) -> str:
    first_claim = brief.claims[0]
    grouped = {
        status: [claim for claim in brief.claims[1:] if claim.status is status]
        for status in ClaimStatus
    }
    opening = f"{first_claim.text}\n[{first_claim.status.value} · {first_claim.id}]"
    if brief.language == "中文":
        sections = [opening, "", f"我正在记录：{brief.topic}", ""]
        section_titles = {
            ClaimStatus.VERIFIED: "现在能被证据支持的事实：",
            ClaimStatus.OBSERVED: "目前的个人观察：",
            ClaimStatus.HYPOTHESIS: "仍需验证的假设：",
            ClaimStatus.PLANNED: "接下来会做的实验：",
        }
    else:
        sections = [opening, "", f"I am documenting: {brief.topic}", ""]
        section_titles = {
            ClaimStatus.VERIFIED: "What the evidence supports today:",
            ClaimStatus.OBSERVED: "What I have observed:",
            ClaimStatus.HYPOTHESIS: "What remains a hypothesis:",
            ClaimStatus.PLANNED: "What I will test next:",
        }
    for status in ClaimStatus:
        claims = grouped[status]
        if not claims:
            continue
        sections.extend(
            [
                section_titles[status],
                *[_claim_line(claim, brief.language) for claim in claims],
                "",
            ]
        )
    limits = [claim for claim in brief.claims if claim.limits]
    if limits:
        sections.extend([*[f"- [{claim.id}] {claim.limits}" for claim in limits], ""])
    sections.append(brief.call_to_action)
    return "\n".join(sections).strip() + "\n"


def _render_x_thread(brief: ContentBrief) -> list[str]:
    first_claim = brief.claims[0]
    posts = [f"{first_claim.text}\n[{first_claim.status.value} · {first_claim.id}]"]
    for claim in brief.claims[1:]:
        post = f"[{claim.status.value} · {claim.id}] {claim.text}"
        if claim.limits:
            post += f"\n{claim.limits}"
        if len(post) > 280:
            raise ContentWorkspaceError(f"{claim.id} is too long for an X post")
        posts.append(post)
    proof_note = (
        "Human review is still required before posting."
        if brief.language == "English"
        else "发布前仍需人工复核。"
    )
    posts.extend([proof_note, brief.call_to_action])
    if any(len(post) > 280 for post in posts):
        raise ContentWorkspaceError("Hook or call to action is too long for X")
    return [f"{index}/{len(posts)} {post}" for index, post in enumerate(posts, start=1)]


def _render_x_post(brief: ContentBrief) -> str:
    claim = brief.claims[0]
    body = f"{claim.text}\n[{claim.status.value} · {claim.id}]"
    with_cta = f"{body}\n\n{brief.call_to_action}"
    return (with_cta if len(with_cta) <= 280 else body).strip() + "\n"


def _render_canonical_story(brief: ContentBrief) -> str:
    if brief.language == "中文":
        headings = (
            "我想做什么",
            "发生了什么",
            "哪里发生了变化 / 仍有边界",
            "我学到了什么",
            "其他构建者可能用得上的地方",
            "下一步",
        )
        lesson = "可复用的约束是：事实保留证据锚点，假设与计划保持原标签。"
    else:
        headings = (
            "What I was trying to do",
            "What happened",
            "What changed or remains bounded",
            "What I learned",
            "What another builder may find useful",
            "What I will do next",
        )
        lesson = (
            "The reusable constraint is simple: keep facts attached to evidence, and "
            "keep hypotheses and plans labeled as such."
        )
    boundaries = [
        f"- [{claim.id}] {claim.limits}" for claim in brief.claims if claim.limits
    ]
    boundaries.extend(f"- {gap}" for gap in brief.evidence_gaps)
    if not boundaries:
        boundaries.append("- No external outcome is claimed by this private artifact.")
    planned = [
        _claim_line(claim, brief.language)
        for claim in brief.claims
        if claim.status is ClaimStatus.PLANNED
    ]
    if not planned:
        planned = [f"- {brief.call_to_action}"]
    lines = [
        f"# {brief.topic}",
        "",
        f"## {headings[0]}",
        brief.topic,
        "",
        f"## {headings[1]}",
        *[_claim_line(claim, brief.language) for claim in brief.claims],
        "",
        f"## {headings[2]}",
        *boundaries,
        "",
        f"## {headings[3]}",
        lesson,
        "",
        f"## {headings[4]}",
        f"Evidence source: {brief.source_label}",
        "",
        f"## {headings[5]}",
        *planned,
        "",
        brief.call_to_action,
    ]
    return "\n".join(lines).strip() + "\n"


def _render_blog(brief: ContentBrief, canonical_story: str) -> str:
    intro = (
        "This is a working note from one evidence-bounded owner workflow. It reports "
        "what the current receipts support and keeps the remaining limits visible."
        if brief.language == "English"
        else (
            "这是一份来自真实 owner workflow 的证据边界记录："
            "只写当前回执能支持的事实，并保留仍未证明的边界。"
        )
    )
    takeaway = (
        "## Practical takeaway\n\nUse one evidence package to create multiple "
        "adaptations, then review the exact text before publication."
        if brief.language == "English"
        else "## 可执行结论\n\n用同一个 Evidence Package 生成不同渠道适配，并在发布前审核最终文本。"
    )
    return f"{canonical_story.strip()}\n\n{intro}\n\n{takeaway}\n\n{brief.call_to_action}\n"


def _render_storyboard(brief: ContentBrief) -> list[StoryboardScene]:
    scenes: list[StoryboardScene] = []
    pattern = brief.content_pattern
    target_duration = pattern.video.estimated_duration_seconds if pattern else 100
    seconds = max(6, min(20, target_duration // (len(brief.claims) + 2)))
    reference_visuals = (
        ", ".join(pattern.video.visual_elements) if pattern else ""
    )
    for index, claim in enumerate(brief.claims, start=1):
        start = (index - 1) * seconds
        limit = f" · Limit: {claim.limits}" if claim.limits else ""
        if pattern is not None:
            progression = pattern.structure.progression[
                min(index - 1, len(pattern.structure.progression) - 1)
            ]
            purpose = (
                f"{pattern.structure.hook} · {progression}"
                if index == 1
                else progression
            )
            visual = (
                f"Reference-guided {pattern.video.shot_cadence} pacing"
                + (f" with {reference_visuals}" if reference_visuals else "")
                + "; use only operator-owned evidence visuals"
            )
        else:
            purpose = "Hook" if index == 1 else "Evidence"
            visual = (
                "HookCard with SourceBadge"
                if index == 1
                else "Evidence card with source badge and restrained motion"
            )
        scenes.append(
            StoryboardScene(
                id=f"SCENE-{index:02d}",
                start_second=start,
                end_second=start + seconds,
                purpose=purpose,
                visual=visual,
                voiceover=claim.text,
                on_screen_text=f"{claim.status.value} · {claim.id}{limit}",
                claim_ids=[claim.id],
            )
        )
    start = len(scenes) * seconds
    scenes.append(
        StoryboardScene(
            id=f"SCENE-{len(scenes) + 1:02d}",
            start_second=start,
            end_second=start + seconds,
            purpose="Evidence boundary",
            visual="Show the real SoloScale project screen or receipt metadata; hide private paths",
            voiceover=(
                "These receipts support the process and artifact, not an external business outcome."
                if brief.language == "English"
                else "这些回执只支持过程和产物，不代表已经获得外部业务结果。"
            ),
            on_screen_text="Evidence-backed · external outcome not claimed",
            claim_ids=[],
        )
    )
    start += seconds
    scenes.append(
        StoryboardScene(
            id=f"SCENE-{len(scenes) + 1:02d}",
            start_second=start,
            end_second=start + seconds,
            purpose="CTA",
            visual="CTA card; no unsupported metric",
            voiceover=brief.call_to_action,
            on_screen_text=brief.call_to_action,
            claim_ids=[],
        )
    )
    return scenes


def _render_video_script(brief: ContentBrief, scenes: list[StoryboardScene]) -> str:
    lines = [f"# {brief.topic}", "", f"Audience: {brief.audience}", ""]
    if brief.content_pattern is not None:
        pattern = brief.content_pattern
        lines.extend(
            [
                (
                    "Reference pattern applied: "
                    f"{pattern.structure.hook}; {pattern.video.shot_cadence} pacing; "
                    f"{pattern.language.tone}."
                ),
                (
                    "Facts source: operator claim ledger only; reference facts and "
                    "distinctive expression are excluded."
                ),
                "",
            ]
        )
    for scene in scenes:
        lines.extend(
            [
                f"## {scene.start_second:02d}–{scene.end_second:02d}s · {scene.purpose}",
                "",
                f"- Voiceover: {scene.voiceover}",
                f"- Visual: {scene.visual}",
                f"- On-screen: {scene.on_screen_text}",
                f"- Claim anchors: {', '.join(scene.claim_ids) if scene.claim_ids else 'none'}",
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def _render_youtube_script(brief: ContentBrief) -> str:
    """Render a safe long-form fallback from the same grounded claim ledger."""

    if brief.language == "中文":
        opening = (
            "这不是一个成功学故事，而是一次真实工程过程的复盘。"
            "下面每个事实都保留 SoloScale 的证据状态与 claim 锚点。"
        )
        sections = (
            "开场：真正的问题",
            "发生了什么",
            "架构和决策",
            "失败与意外",
            "可以复用的结论",
            "下一步",
        )
    else:
        opening = (
            "This is not a victory story. It is a grounded account of a real engineering "
            "process, with every factual statement retaining its SoloScale claim anchor."
        )
        sections = (
            "Hook: the real problem",
            "What happened",
            "Architecture and decisions",
            "Failure and surprise",
            "The reusable lesson",
            "What happens next",
        )
    grouped: list[list[ContentClaim]] = [[] for _ in sections]
    for index, claim in enumerate(brief.claims):
        grouped[min(index, len(sections) - 1)].append(claim)
    lines = [f"# {brief.topic}", "", "Format: 4–6 minute YouTube narration", "", opening]
    for heading, claims in zip(sections, grouped, strict=True):
        lines.extend(["", f"## {heading}", ""])
        if claims:
            lines.extend(_claim_line(claim, brief.language) for claim in claims)
        else:
            lines.append(
                "- Keep this transition grounded in the preceding verified material."
            )
    lines.extend(["", brief.call_to_action, ""])
    return "\n".join(lines)


def build_content_drafts(brief: ContentBrief) -> ContentDrafts:
    _validate_public_fields(brief)
    canonical_story = _render_canonical_story(brief)
    storyboard = _render_storyboard(brief)
    drafts = ContentDrafts(
        canonical_story=canonical_story,
        linkedin=_render_linkedin(brief),
        x_thread=_render_x_thread(brief),
        x_post=_render_x_post(brief),
        blog=_render_blog(brief, canonical_story),
        youtube_script=_render_youtube_script(brief),
        video_script=_render_video_script(brief, storyboard),
        storyboard=storyboard,
    )
    _validate_generated_drafts(brief, drafts)
    return _strip_public_claim_markers(drafts)


def _strip_claim_markers(value: str) -> str:
    """Remove internal claim truth markers from public-facing text (A8 boundary)."""

    stripped = _INTERNAL_LABEL_LINE.sub("", value)
    stripped = _CLAIM_MAP_LINE.sub("", stripped)
    stripped = _CLAIM_MARKER_LINE_PREFIX.sub("", stripped)
    stripped = _INTERNAL_BOUNDARY_HEADING.sub("", stripped)
    stripped = _INTERNAL_EVIDENCE_MAP_HEADING.sub("", stripped)
    stripped = _INTERNAL_PROOF_LABEL_LINE.sub("", stripped)
    stripped = _INTERNAL_LIMIT_LABEL_LINE.sub("", stripped)
    stripped = _CLAIM_MARKER_TOKEN.sub("", stripped)
    stripped = _INTERNAL_LIMIT_INLINE.sub("", stripped)
    stripped = _SEPARATOR_EDGE.sub("", stripped)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    trailing_newline = "\n" if value.endswith(("\n", "\r\n")) else ""
    return stripped.strip() + trailing_newline


def _strip_public_claim_markers(drafts: ContentDrafts) -> ContentDrafts:
    """Return a public copy of the drafts without internal provenance markers.

    Validation has already proven every claim is present; the storyboard keeps its
    claim_ids so lineage stays internal while the rendered text becomes publish-safe.
    """

    def stripped_scene(scene: StoryboardScene) -> StoryboardScene:
        voiceover = _strip_claim_markers(scene.voiceover) or scene.voiceover
        on_screen_text = _strip_claim_markers(scene.on_screen_text) or voiceover
        purpose = _strip_claim_markers(scene.purpose) or "Evidence anchor"
        return scene.model_copy(
            update={
                "purpose": purpose,
                "visual": _strip_claim_markers(scene.visual) or scene.visual,
                "voiceover": voiceover,
                "on_screen_text": on_screen_text,
            }
        )

    return drafts.model_copy(
        update={
            "canonical_story": _strip_claim_markers(drafts.canonical_story),
            "linkedin": _strip_claim_markers(drafts.linkedin),
            "x_thread": [_strip_claim_markers(post) for post in drafts.x_thread],
            "x_post": _strip_claim_markers(drafts.x_post),
            "blog": _strip_claim_markers(drafts.blog),
            "youtube_script": _strip_claim_markers(drafts.youtube_script),
            "video_script": _strip_claim_markers(drafts.video_script),
            "storyboard": [stripped_scene(scene) for scene in drafts.storyboard],
        }
    )


def _ground_ollama_drafts(
    brief: ContentBrief, transport: _OllamaContentDrafts
) -> ContentDrafts:
    """Apply deterministic truth anchors to model prose without adding new claims."""

    allowed_ids = {claim.id for claim in brief.claims}
    raw_text = [
        transport.canonical_story,
        transport.linkedin,
        *transport.x_thread,
        transport.x_post,
        transport.blog,
        transport.youtube_script,
        transport.video_script,
    ]
    for scene in transport.storyboard:
        raw_text.extend(
            [scene.purpose, scene.visual, scene.voiceover, scene.on_screen_text]
        )
        if set(scene.claim_ids) - allowed_ids:
            raise ContentWorkspaceError("Local Ollama returned an unknown claim ID")
    joined_raw = "\n".join(raw_text)
    _reject_private_output(joined_raw, field="Local Ollama draft")
    if set(_CLAIM_ID.findall(joined_raw)) - allowed_ids:
        raise ContentWorkspaceError("Local Ollama returned an unknown claim ID")

    required_lines = [
        f"{claim.status.value} · {claim.id} — {claim.text}" for claim in brief.claims
    ]

    def anchored_channel(value: str) -> str:
        result = value.strip()
        for claim, line in zip(brief.claims, required_lines, strict=True):
            marker = f"{claim.status.value} · {claim.id}"
            if marker not in result:
                result = f"{result}\n\n{line}".strip()
        if brief.call_to_action not in result:
            result = f"{result}\n\n{brief.call_to_action}".strip()
        return result

    def anchored_x_post(value: str) -> str:
        result = value.strip()
        first = brief.claims[0]
        marker = f"{first.status.value} · {first.id}"
        if not result or marker not in result or len(result) > 280:
            return _render_x_post(brief).strip()
        if set(_CLAIM_ID.findall(result)) - allowed_ids:
            raise ContentWorkspaceError("Local Ollama returned an unknown claim ID")
        return result

    x_bodies: list[str] = []
    for post in transport.x_thread:
        body = re.sub(
            r"^\s*(?:N|[0-9]+)/(?:TOTAL|[0-9]+)\s+",
            "",
            post.strip(),
            flags=re.I,
        )
        if body and body not in x_bodies:
            x_bodies.append(body)
    joined_x = "\n".join(x_bodies)
    for claim, line in zip(brief.claims, required_lines, strict=True):
        marker = f"{claim.status.value} · {claim.id}"
        if marker not in joined_x:
            x_bodies.append(line)
    if brief.call_to_action not in "\n".join(x_bodies):
        x_bodies.append(brief.call_to_action)
    if len(x_bodies) > 12:
        raise ContentWorkspaceError("Local Ollama returned too many X posts")
    total = len(x_bodies)
    x_thread = [
        f"{index}/{total} {body}" for index, body in enumerate(x_bodies, start=1)
    ]

    scenes = list(transport.storyboard)
    for claim, line in zip(brief.claims, required_lines, strict=True):
        anchored = next(
            (scene for scene in scenes if claim.id in scene.claim_ids), None
        )
        if anchored is None:
            scenes.append(
                _OllamaStoryboardScene(
                    id="",
                    start_second=0,
                    end_second=1,
                    purpose="Evidence anchor",
                    visual="Evidence card",
                    voiceover=claim.text,
                    on_screen_text=line,
                    claim_ids=[claim.id],
                )
            )
        else:
            scene_text = "\n".join(
                [
                    anchored.purpose,
                    anchored.visual,
                    anchored.voiceover,
                    anchored.on_screen_text,
                ]
            )
            marker = f"{claim.status.value} · {claim.id}"
            if marker not in scene_text:
                anchored.on_screen_text = line
    storyboard_text = "\n".join(
        value
        for scene in scenes
        for value in (
            scene.purpose,
            scene.visual,
            scene.voiceover,
            scene.on_screen_text,
        )
    )
    if brief.call_to_action not in storyboard_text:
        scenes.append(
            _OllamaStoryboardScene(
                id="",
                start_second=0,
                end_second=1,
                purpose="CTA",
                visual="CTA card",
                voiceover=brief.call_to_action,
                on_screen_text=brief.call_to_action,
                claim_ids=[],
            )
        )
    if len(scenes) > 12:
        raise ContentWorkspaceError("Local Ollama returned too many storyboard scenes")
    normalized_scenes = [
        scene.model_copy(
            update={
                "id": f"SCENE-{index:02d}",
                "start_second": (index - 1) * 6,
                "end_second": index * 6,
            }
        ).model_dump(mode="json")
        for index, scene in enumerate(scenes, start=1)
    ]

    try:
        return ContentDrafts.model_validate(
            {
                "canonical_story": anchored_channel(transport.canonical_story),
                "linkedin": anchored_channel(transport.linkedin),
                "x_thread": x_thread,
                "x_post": anchored_x_post(transport.x_post),
                "blog": anchored_channel(transport.blog),
                "youtube_script": anchored_channel(transport.youtube_script),
                "video_script": anchored_channel(transport.video_script),
                "storyboard": normalized_scenes,
            }
        )
    except ValidationError as exc:
        raise ContentWorkspaceError(
            "Local Ollama returned a draft outside the required content schema."
        ) from exc


def generate_content_drafts_with_gateway(
    brief: ContentBrief,
    *,
    gateway: ModelGateway,
) -> ContentDrafts:
    """Generate one schema-constrained draft set through an explicit provider."""

    _validate_public_fields(brief)
    descriptor = gateway.descriptor
    if descriptor.configuration_state is not GatewayConfigurationState.CONFIGURED:
        raise ModelGatewayNotConfigured(
            f"{descriptor.display_name} is not configured in this build"
        )
    prompt = _canonical_json(
        {
            "brief": brief.model_dump(mode="json", exclude={"reference_asset"}),
            "locale_policy": {
                "locale": "zh-CN" if brief.language == "中文" else "en-US",
                "adaptation": "native editorial variant, not literal translation",
                "single_locale_output": True,
                "shared_facts_only": True,
            },
            "required_claim_markers": [
                f"{claim.status.value} · {claim.id}" for claim in brief.claims
            ],
            "required_cta": brief.call_to_action,
            "reference_policy": {
                "facts_source": "operator_claim_ledger_only",
                "allowed_use": ["high-level structure", "pacing", "tone", "visual pattern"],
                "forbidden_use": [
                    "reference facts",
                    "distinctive phrases",
                    "reference examples",
                    "unique creative expression",
                ],
                "raw_reference_included": False,
            },
        }
    )
    try:
        transport_drafts = gateway.complete(
            _OllamaContentDrafts,
            system=_OLLAMA_SYSTEM_PROMPT,
            user=prompt,
        )
    except ModelGatewayTransportError as exc:
        if descriptor.provider is ModelProviderId.OLLAMA:
            raise ContentWorkspaceError(
                f"Cannot reach local Ollama or model '{descriptor.model}'. "
                "Start Ollama and make sure the model is installed."
            ) from exc
        raise ContentWorkspaceError("The selected AI provider could not be reached") from exc
    except ModelGatewayInvalidResponse as exc:
        if descriptor.provider is ModelProviderId.OLLAMA:
            raise ContentWorkspaceError(
                "Local Ollama returned a draft outside the required content schema."
            ) from exc
        raise ContentWorkspaceError(
            "The selected AI provider returned invalid structured output"
        ) from exc
    drafts = _ground_ollama_drafts(brief, transport_drafts)
    _validate_generated_drafts(brief, drafts)
    return _strip_public_claim_markers(drafts)


def generate_content_drafts_with_ollama(
    brief: ContentBrief,
    *,
    endpoint: str = "http://127.0.0.1:11434",
    model: str = "qwen3:8b",
    reasoner: Reasoner | None = None,
) -> ContentDrafts:
    """Compatibility seam for the optional loopback-only Ollama adapter."""

    try:
        gateway = OllamaModelGateway(
            endpoint=endpoint,
            model=model,
            reasoner=reasoner,
        )
    except ValueError as exc:
        raise ContentWorkspaceError(str(exc)) from exc
    return generate_content_drafts_with_gateway(brief, gateway=gateway)


def run_content_workspace_with_gateway(
    *,
    data_root: Path,
    brief: ContentBrief,
    gateway: ModelGateway,
    evidence_hub: EvidenceHub | None = None,
    reference_source_text: str | None = None,
) -> ContentRun:
    """Generate through one selected provider, then persist truthful provenance."""

    normalized_reference = _validate_reference_source(
        brief, reference_source_text
    )
    drafts = generate_content_drafts_with_gateway(brief, gateway=gateway)
    descriptor = gateway.descriptor
    model_call_profile = getattr(gateway, "last_call_profile", None)
    provider_kinds = {
        ModelProviderId.SOLOSCALE_HOSTED: ProviderKind.SOLOSCALE_HOSTED,
        ModelProviderId.OLLAMA: ProviderKind.OLLAMA,
        ModelProviderId.OPENAI_COMPATIBLE: ProviderKind.OPENAI_COMPATIBLE,
    }
    prompt_versions = {
        ModelProviderId.SOLOSCALE_HOSTED: "content-hosted-writer-v1",
        ModelProviderId.OLLAMA: _OLLAMA_PROMPT_VERSION,
        ModelProviderId.OPENAI_COMPATIBLE: "content-openai-compatible-writer-v1",
    }
    return run_content_workspace(
        data_root=data_root,
        brief=brief,
        evidence_hub=evidence_hub,
        drafts=drafts,
        provider=ProviderIdentity(
            kind=provider_kinds[descriptor.provider],
            provider=descriptor.provider.value,
            model=descriptor.model,
            base_url=descriptor.base_url,
        ),
        prompt_version=prompt_versions[descriptor.provider],
        network_used=True,
        reference_source_text=normalized_reference,
        model_call_profile=model_call_profile,
    )


def run_content_workspace_with_ollama(
    *,
    data_root: Path,
    brief: ContentBrief,
    model: str = "qwen3:8b",
    endpoint: str = "http://127.0.0.1:11434",
    reasoner: Reasoner | None = None,
    evidence_hub: EvidenceHub | None = None,
    reference_source_text: str | None = None,
) -> ContentRun:
    """Compatibility seam for existing local Ollama callers and receipts."""

    try:
        gateway = OllamaModelGateway(
            endpoint=endpoint,
            model=model,
            reasoner=reasoner,
        )
    except ValueError as exc:
        raise ContentWorkspaceError(str(exc)) from exc
    return run_content_workspace_with_gateway(
        data_root=data_root,
        brief=brief,
        gateway=gateway,
        evidence_hub=evidence_hub,
        reference_source_text=reference_source_text,
    )


def _validate_generated_drafts(brief: ContentBrief, drafts: ContentDrafts) -> None:
    allowed_ids = {claim.id for claim in brief.claims}
    text_artifacts = {
        "canonical story": drafts.canonical_story,
        "LinkedIn draft": drafts.linkedin,
        "X thread": "\n\n".join(drafts.x_thread),
        "blog draft": drafts.blog,
        "YouTube script": drafts.youtube_script,
        "video script": drafts.video_script,
    }
    for field, value in text_artifacts.items():
        _reject_private_output(value, field=field)
        referenced = set(_CLAIM_ID.findall(value))
        if referenced != allowed_ids:
            raise ContentWorkspaceError(
                f"{field} must retain every supplied claim ID and no unknown claim IDs"
            )
        for claim in brief.claims:
            marker = f"{claim.status.value} · {claim.id}"
            if marker not in value:
                raise ContentWorkspaceError(
                    f"{field} must retain the truth marker for {claim.id}"
                )
        if brief.call_to_action not in value:
            raise ContentWorkspaceError(f"{field} must preserve the supplied CTA")

    _reject_private_output(drafts.x_post, field="standalone X post")
    x_post_ids = set(_CLAIM_ID.findall(drafts.x_post))
    if x_post_ids - allowed_ids:
        raise ContentWorkspaceError("Standalone X post contains an unknown claim ID")
    first_claim = brief.claims[0]
    first_marker = f"{first_claim.status.value} · {first_claim.id}"
    if first_marker not in drafts.x_post:
        raise ContentWorkspaceError(
            "Standalone X post must retain the first supplied truth marker"
        )

    total = len(drafts.x_thread)
    for index, post in enumerate(drafts.x_thread, start=1):
        if not post.startswith(f"{index}/{total} "):
            raise ContentWorkspaceError("X thread numbering must be consecutive and exact")

    scene_ids = [scene.id for scene in drafts.storyboard]
    if len(scene_ids) != len(set(scene_ids)):
        raise ContentWorkspaceError("Storyboard scene IDs must be unique")
    storyboard_claim_ids: set[str] = set()
    previous_end = 0
    storyboard_text: list[str] = []
    for scene in drafts.storyboard:
        if scene.start_second < previous_end:
            raise ContentWorkspaceError("Storyboard scenes must be ordered and non-overlapping")
        previous_end = scene.end_second
        unknown = set(scene.claim_ids) - allowed_ids
        if unknown:
            raise ContentWorkspaceError("Storyboard contains an unknown claim ID")
        storyboard_claim_ids.update(scene.claim_ids)
        storyboard_text.extend(
            [scene.purpose, scene.visual, scene.voiceover, scene.on_screen_text]
        )
    if storyboard_claim_ids != allowed_ids:
        raise ContentWorkspaceError("Storyboard must anchor every supplied claim ID")
    joined_storyboard = "\n".join(storyboard_text)
    _reject_private_output(joined_storyboard, field="storyboard")
    for claim in brief.claims:
        marker = f"{claim.status.value} · {claim.id}"
        if marker not in joined_storyboard:
            raise ContentWorkspaceError(
                f"Storyboard must retain the truth marker for {claim.id}"
            )
    if brief.call_to_action not in joined_storyboard:
        raise ContentWorkspaceError("Storyboard must preserve the supplied CTA")


def _validate_reviewed_drafts(brief: ContentBrief, drafts: ContentDrafts) -> None:
    """Review-time contract for human-edited drafts.

    Generated drafts are marker-free by A8, so review validation preserves the
    invariants that still matter: no unknown claim IDs, no private output, the
    supplied CTA stays intact, and X thread numbering remains exact.
    """

    allowed_ids = {claim.id for claim in brief.claims}
    text_artifacts = {
        "canonical story": drafts.canonical_story,
        "LinkedIn draft": drafts.linkedin,
        "X thread": "\n\n".join(drafts.x_thread),
        "blog draft": drafts.blog,
        "YouTube script": drafts.youtube_script,
        "video script": drafts.video_script,
    }
    for field, value in text_artifacts.items():
        _reject_private_output(value, field=field)
        referenced = set(_CLAIM_ID.findall(value))
        if referenced - allowed_ids:
            raise ContentWorkspaceError(
                f"{field} must not introduce unknown claim IDs"
            )
        if brief.call_to_action not in value:
            raise ContentWorkspaceError(f"{field} must preserve the supplied CTA")

    _reject_private_output(drafts.x_post, field="standalone X post")
    if set(_CLAIM_ID.findall(drafts.x_post)) - allowed_ids:
        raise ContentWorkspaceError("Standalone X post contains an unknown claim ID")

    total = len(drafts.x_thread)
    for index, post in enumerate(drafts.x_thread, start=1):
        if not post.startswith(f"{index}/{total} "):
            raise ContentWorkspaceError("X thread numbering must be consecutive and exact")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _sha256(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _story_locale_variant(brief: ContentBrief) -> StoryLocaleVariant:
    locale: Literal["zh-CN", "en-US"] = (
        "zh-CN" if brief.language == "中文" else "en-US"
    )
    canonical_story_id = brief.evidence_filters.get("canon_story_id") or None
    fact_contract_sha256 = _sha256(
        {
            "claims": [claim.model_dump(mode="json") for claim in brief.claims],
            "evidence_bundle_id": brief.evidence_bundle_id,
            "evidence_item_ids": brief.evidence_item_ids,
            "evidence_gaps": brief.evidence_gaps,
            "source_label": brief.source_label,
        }
    )
    variant_group_id = (
        f"canonical-story:{canonical_story_id}"
        if canonical_story_id is not None
        else f"fact-contract:{fact_contract_sha256[:24]}"
    )
    return StoryLocaleVariant(
        locale=locale,
        variant_group_id=variant_group_id,
        canonical_story_id=canonical_story_id,
        fact_contract_sha256=fact_contract_sha256,
    )


def _new_run_dir(data_root: Path) -> tuple[str, Path]:
    _reject_symlink_ancestry(data_root)
    _ensure_private_directory(data_root, parents=True)
    runs_root = data_root / "content-runs"
    _ensure_private_directory(runs_root)
    for _ in range(8):
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"content-{timestamp}-{uuid4().hex[:10]}"
        run_dir = runs_root / run_id
        if run_dir.exists() or run_dir.is_symlink():
            continue
        _ensure_private_directory(run_dir)
        return run_id, run_dir
    raise ContentWorkspaceError("Could not allocate a non-overwriting content run")


def run_content_workspace(
    *,
    data_root: Path,
    brief: ContentBrief,
    evidence_hub: EvidenceHub | None = None,
    drafts: ContentDrafts | None = None,
    provider: ProviderIdentity | None = None,
    prompt_version: str | None = None,
    network_used: bool = False,
    reference_source_text: str | None = None,
    model_call_profile: ModelCallProfile | None = None,
    fallback_used: bool = False,
) -> ContentRun:
    """Build one private content candidate without publishing it."""

    normalized_reference = _validate_reference_source(brief, reference_source_text)
    reference_context: dict[str, object] = {
        "status": "NOT_REQUESTED",
        "reference_id": None,
        "pattern_id": None,
        "raw_sha256": None,
        "raw_reference_in_public_outputs": False,
        "facts_source": "operator_claim_ledger_only",
    }
    if brief.reference_asset is not None and brief.content_pattern is not None:
        if (
            brief.reference_asset.source_kind is ReferenceSourceKind.MANUAL_TEXT
            and normalized_reference is None
        ):
            raise ContentWorkspaceError("Reference source text is unavailable")
        reference_context = {
            "status": (
                "VIDEO_PATTERN_ANALYZED_LOCALLY"
                if brief.reference_asset.source_kind is ReferenceSourceKind.LOCAL_VIDEO
                else "PATTERN_DISTILLED_LOCALLY"
            ),
            "reference_id": brief.reference_asset.reference_id,
            "pattern_id": brief.content_pattern.pattern_id,
            "raw_sha256": brief.reference_asset.raw_sha256,
            "raw_reference_in_public_outputs": False,
            "facts_source": brief.content_pattern.facts_source,
            "distinctive_expression_reuse_allowed": False,
        }

    evidence_context: dict[str, object] = {
        "status": "NOT_REQUESTED",
        "bundle_id": None,
        "bundle_sha256": None,
        "coverage": [],
        "gaps": [],
        "items": [],
    }
    if brief.evidence_bundle_id is not None:
        selected_hub = evidence_hub or EvidenceHub(data_root)
        bundle, items = selected_hub.resolve_bundle(brief.evidence_bundle_id)
        if brief.evidence_item_ids and not set(brief.evidence_item_ids).issubset(
            bundle.evidence_ids
        ):
            raise ValueError("content evidence items must belong to the selected bundle")
        selected_ids = brief.evidence_item_ids or bundle.evidence_ids
        selected_by_id = {item.evidence_id: item for item in items}
        selected_items = [selected_by_id[evidence_id] for evidence_id in selected_ids]
        merged_gaps = list(dict.fromkeys([*bundle.gaps, *brief.evidence_gaps]))
        for index, coverage in enumerate(bundle.coverage):
            _reject_private_output(coverage, field=f"evidence coverage {index + 1}")
        for index, item in enumerate(selected_items):
            _reject_private_output(
                item.public_safe_summary, field=f"evidence item {index + 1} summary"
            )
        for index, gap in enumerate(merged_gaps):
            _reject_private_output(gap, field=f"evidence gap {index + 1}")
        brief = brief.model_copy(
            update={"evidence_item_ids": selected_ids, "evidence_gaps": merged_gaps}
        )
        evidence_context = {
            "status": "BUNDLE_RESOLVED_FOR_EDITORIAL_REVIEW",
            "bundle_id": bundle.bundle_id,
            "bundle_sha256": bundle.bundle_sha256,
            "coverage": bundle.coverage,
            "gaps": merged_gaps,
            "items": [
                {
                    "evidence_id": item.evidence_id,
                    "public_safe_summary": item.public_safe_summary,
                    "truth_class": item.truth_class.value,
                    "verification_status": item.verification_status,
                }
                for item in selected_items
            ],
        }
        evidence_hub = selected_hub
    if drafts is None:
        if provider is not None or prompt_version is not None or network_used:
            raise ContentWorkspaceError(
                "Custom provider metadata requires generated content drafts"
            )
        drafts = build_content_drafts(brief)
        selected_provider = ProviderIdentity(
            kind=ProviderKind.TEMPLATE,
            provider="soloscale",
            model="deterministic-content-template-v1",
        )
        selected_prompt_version = "content-template-v1"
        model_used = False
    else:
        if provider is None or provider.kind is ProviderKind.TEMPLATE:
            raise ContentWorkspaceError(
                "Generated content drafts require a non-template provider identity"
            )
        _validate_reviewed_drafts(brief, drafts)
        selected_provider = provider
        selected_prompt_version = prompt_version or _OLLAMA_PROMPT_VERSION
        model_used = True
    execution_state: Literal["AI_EXECUTED", "AI_NOT_EXECUTED"] = (
        "AI_EXECUTED" if model_used else "AI_NOT_EXECUTED"
    )
    model_calls = 1 if model_used else 0
    token_usage: dict[str, int] | None = None
    if model_call_profile is not None:
        usage: dict[str, int] = {}
        if model_call_profile.prompt_eval_tokens is not None:
            usage["prompt_eval_tokens"] = model_call_profile.prompt_eval_tokens
        if model_call_profile.output_tokens is not None:
            usage["output_tokens"] = model_call_profile.output_tokens
        token_usage = usage or None
    latency_ms = model_call_profile.wall_ms if model_call_profile is not None else None
    cost_usd = None
    if normalized_reference is not None:
        generated_values = [
            drafts.canonical_story,
            drafts.linkedin,
            *drafts.x_thread,
            drafts.x_post,
            drafts.blog,
            drafts.youtube_script,
            drafts.video_script,
            *[
                value
                for scene in drafts.storyboard
                for value in (
                    scene.purpose,
                    scene.visual,
                    scene.voiceover,
                    scene.on_screen_text,
                )
            ],
        ]
        try:
            reject_distinctive_reference_reuse(
                normalized_reference, generated_values
            )
        except ValueError as exc:
            raise ContentWorkspaceError(str(exc)) from exc
    try:
        run_id, run_dir = _new_run_dir(data_root.absolute())
    except ResumeWorkspaceStorageError as exc:
        raise ContentWorkspaceError(str(exc)) from exc
    created_at = datetime.now(UTC).isoformat()
    artifact_paths = [
        "00_input.json",
        "01_claim_ledger.json",
        "02_linkedin.md",
        "03_x_thread.md",
        "03_x_post.md",
        "04_video_script.md",
        "05_storyboard.json",
        "06_publish_pack.json",
        "07_provenance.json",
        "08_verification.json",
        "12_editorial_provenance.json",
        "14_evidence_context.json",
        "15_canonical_story.md",
        "16_blog.md",
        "20_youtube_script.md",
        "run.json",
    ]
    if brief.reference_asset is not None:
        artifact_paths[-1:-1] = [
            "17_reference_asset.json",
            "18_content_pattern.json",
        ]
        if normalized_reference is not None:
            artifact_paths[-1:-1] = ["19_reference_source.txt"]
    brief_payload = brief.model_dump(mode="json")
    locale_variant = _story_locale_variant(brief)
    locale_variant_payload = locale_variant.model_dump(mode="json")
    drafts_payload = drafts.model_dump(mode="json")
    output_artifacts = {
        "15_canonical_story.md": drafts.canonical_story,
        "02_linkedin.md": drafts.linkedin,
        "03_x_thread.md": "\n\n".join(drafts.x_thread) + "\n",
        "03_x_post.md": drafts.x_post,
        "16_blog.md": drafts.blog,
        "20_youtube_script.md": drafts.youtube_script,
        "04_video_script.md": drafts.video_script,
        "05_storyboard.json": _canonical_json({"scenes": drafts_payload["storyboard"]}),
    }
    editorial_provenance = make_provenance(
        role=EditorialRole.WRITER,
        provider=selected_provider,
        reasoning="deterministic" if not model_used else "schema_constrained",
        prompt_version=selected_prompt_version,
        input_artifacts={
            "00_input.json": _canonical_json(brief_payload),
            "14_evidence_context.json": _canonical_json(evidence_context),
        },
        output_artifacts=output_artifacts,
        network_used=network_used,
        token_usage=token_usage,
        cost_usd=cost_usd,
    )
    publish_pack = {
        "status": "DRAFT_REQUIRES_HUMAN_APPROVAL",
        "topic": brief.topic,
        "execution_state": execution_state,
        "model_calls": model_calls,
        "token_usage": token_usage,
        "latency_ms": latency_ms,
        "cost_usd": cost_usd,
        "fallback_used": fallback_used,
        "locale_variant": locale_variant_payload,
        "channels": [
            "Canonical story",
            "LinkedIn",
            "X",
            "Blog",
            "YouTube",
            "Short video",
        ],
        "drafts": drafts_payload,
        "publication_performed": False,
        "evidence_context": evidence_context,
        "reference_context": reference_context,
        "next_gate": "Human fact-check and explicit per-channel publish approval",
    }
    provenance = {
        "source_label": brief.source_label,
        "claim_ledger_sha256": _sha256(brief_payload["claims"]),
        "claims": brief_payload["claims"],
        "locale_variant": locale_variant_payload,
        "evidence_context": evidence_context,
        "reference_context": reference_context,
        "boundary": (
            "Citation membership and operator classification are recorded; semantic support "
            "and public suitability still require human review."
        ),
        "editorial_pipeline": [editorial_provenance.model_dump(mode="json")],
    }
    verification = {
        "status": "PASS",
        "claim_count": len(brief.claims),
        "every_claim_has_anchor": all(claim.id for claim in brief.claims),
        "verified_and_observed_have_receipts": all(
            claim.receipt
            for claim in brief.claims
            if claim.status in {ClaimStatus.VERIFIED, ClaimStatus.OBSERVED}
        ),
        "private_path_scan_passed": True,
        "credential_shape_scan_passed": True,
        "network_used": network_used,
        "model_used": model_used,
        "execution_state": execution_state,
        "model_calls": model_calls,
        "token_usage": token_usage,
        "latency_ms": latency_ms,
        "cost_usd": cost_usd,
        "fallback_used": fallback_used,
        "editorial_provenance_recorded": True,
        "evidence_bundle_used": brief.evidence_bundle_id is not None,
        "evidence_item_count": len(brief.evidence_item_ids),
        "evidence_gap_count": len(brief.evidence_gaps),
        "publication_performed": False,
        "locale": locale_variant.locale,
        "variant_group_id": locale_variant.variant_group_id,
        "fact_contract_sha256": locale_variant.fact_contract_sha256,
    }
    if brief.reference_asset is not None:
        verification.update(
            {
                "reference_pattern_used": True,
                "raw_reference_in_public_outputs": False,
                "reference_originality_scan_passed": normalized_reference is not None,
            }
        )
    run = ContentRun(
        run_id=run_id,
        created_at=created_at,
        brief=brief,
        drafts=drafts,
        locale_variant=locale_variant,
        artifact_paths=artifact_paths,
        editorial_provenance=[editorial_provenance],
        network_used=network_used,
        model_used=model_used,
        execution_state=execution_state,
        model_calls=model_calls,
        token_usage=token_usage,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        fallback_used=fallback_used,
        limitations=[
            (
                "Drafts were generated by a local schema-constrained model and still require "
                "human semantic fact-checking."
                if model_used
                else (
                    "Drafts are deterministic editorial candidates, not semantic "
                    "fact-check results."
                )
            ),
            "Receipts may still need public-safe URLs before posting.",
            "No account connection or publishing action was performed.",
            "Evidence gaps remain explicit and are not converted into factual claims.",
            *(
                [
                    (
                        "The reference supplied expression guidance only; its facts, "
                        "examples, and distinctive wording were excluded."
                    )
                ]
                if brief.reference_asset is not None
                else []
            ),
        ],
    )
    artifacts = {
        "00_input.json": _canonical_json(brief_payload),
        "01_claim_ledger.json": _canonical_json({"claims": brief_payload["claims"]}),
        "15_canonical_story.md": drafts.canonical_story,
        "02_linkedin.md": drafts.linkedin,
        "03_x_thread.md": "\n\n".join(drafts.x_thread) + "\n",
        "03_x_post.md": drafts.x_post,
        "16_blog.md": drafts.blog,
        "20_youtube_script.md": drafts.youtube_script,
        "04_video_script.md": drafts.video_script,
        "05_storyboard.json": _canonical_json({"scenes": drafts_payload["storyboard"]}),
        "06_publish_pack.json": _canonical_json(publish_pack),
        "07_provenance.json": _canonical_json(provenance),
        "08_verification.json": _canonical_json(verification),
        "12_editorial_provenance.json": _canonical_json(
            {
                "workflow": ["writer", "fresh_reviewer", "reviser", "human_gate"],
                "completed": [editorial_provenance.model_dump(mode="json")],
                "next_gate": "Fresh independent review before controlled revision",
            }
        ),
        "14_evidence_context.json": _canonical_json(evidence_context),
        "run.json": _canonical_json(run.model_dump(mode="json")),
    }
    if brief.reference_asset is not None:
        if brief.reference_asset is None or brief.content_pattern is None:
            raise ContentWorkspaceError("Reference metadata is unavailable")
        artifacts.update(
            {
                "17_reference_asset.json": _canonical_json(
                    brief.reference_asset.model_dump(mode="json")
                ),
                "18_content_pattern.json": _canonical_json(
                    brief.content_pattern.model_dump(mode="json")
                ),
            }
        )
        if normalized_reference is not None:
            artifacts["19_reference_source.txt"] = normalized_reference + "\n"
    try:
        for name in artifact_paths:
            _atomic_private_write(run_dir / name, artifacts[name])
    except (OSError, ResumeWorkspaceStorageError) as exc:
        raise ContentWorkspaceError("Private content artifacts could not be saved") from exc
    capture_assets(
        data_root=data_root,
        run_dir=run_dir,
        owner="content",
        run_id=run_id,
        artifact_names=artifact_paths,
        evidence_bundle_id=brief.evidence_bundle_id,
        evidence_item_ids=brief.evidence_item_ids,
        evidence_hub=evidence_hub,
    )
    return run


def content_run_directory(data_root: Path, run_id: str) -> Path:
    if _CONTENT_RUN_ID.fullmatch(run_id) is None:
        raise ContentWorkspaceError("Content run id is invalid")
    root = data_root.absolute()
    try:
        _reject_symlink_ancestry(root)
    except ResumeWorkspaceStorageError as exc:
        raise ContentWorkspaceError(str(exc)) from exc
    run_dir = root / "content-runs" / run_id
    try:
        metadata = run_dir.lstat()
    except FileNotFoundError:
        raise ContentWorkspaceError("Content run is unavailable") from None
    if not stat.S_ISDIR(metadata.st_mode) or run_dir.is_symlink():
        raise ContentWorkspaceError("Content run is unsafe")
    return run_dir


def load_content_run(data_root: Path, run_id: str) -> ContentRun:
    run_dir = content_run_directory(data_root, run_id)
    path = run_dir / "run.json"
    if path.is_symlink() or not path.is_file():
        raise ContentWorkspaceError("Content run receipt is unavailable")
    try:
        run = ContentRun.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValidationError) as exc:
        raise ContentWorkspaceError("Content run receipt is invalid") from exc
    if not run.drafts.canonical_story or not run.drafts.x_post or not run.drafts.blog:
        fallback = build_content_drafts(run.brief)
        run = run.model_copy(
            update={
                "drafts": run.drafts.model_copy(
                    update={
                        "canonical_story": run.drafts.canonical_story
                        or fallback.canonical_story,
                        "x_post": run.drafts.x_post or fallback.x_post,
                        "blog": run.drafts.blog or fallback.blog,
                        "youtube_script": run.drafts.youtube_script
                        or fallback.youtube_script,
                    }
                )
            }
        )
    return run


def _draft_review_values(drafts: ContentDrafts) -> dict[str, str]:
    return {
        "canonical_story": drafts.canonical_story,
        "linkedin": drafts.linkedin,
        "x_thread": "\n\n".join(drafts.x_thread).strip() + "\n",
        "x_post": drafts.x_post,
        "blog": drafts.blog,
        "youtube_script": drafts.youtube_script,
        "video_script": drafts.video_script,
    }


def _latest_review_directory(run_dir: Path) -> Path | None:
    reviews_root = run_dir / "reviews"
    if reviews_root.is_symlink() or not reviews_root.is_dir():
        return None
    candidates = [
        path
        for path in reviews_root.iterdir()
        if re.fullmatch(r"review-[0-9]{4}", path.name)
        and path.is_dir()
        and not path.is_symlink()
    ]
    return max(candidates, key=lambda path: path.name) if candidates else None


def load_content_review(
    data_root: Path, run_id: str
) -> tuple[ContentReviewReceipt, dict[str, str]] | None:
    """Load and hash-verify the latest immutable owner review revision."""

    run_dir = content_run_directory(data_root, run_id)
    review_dir = _latest_review_directory(run_dir)
    if review_dir is None:
        return None
    receipt_path = review_dir / "review.json"
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise ContentWorkspaceError("Content review receipt is unavailable")
    try:
        receipt = ContentReviewReceipt.model_validate_json(
            receipt_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, ValidationError) as exc:
        raise ContentWorkspaceError("Content review receipt is invalid") from exc
    if receipt.run_id != run_id:
        raise ContentWorkspaceError("Content review receipt does not match this run")
    values: dict[str, str] = {}
    for key, filename in _REVIEW_ARTIFACTS.items():
        path = review_dir / filename
        if path.is_symlink() or not path.is_file():
            raise ContentWorkspaceError("Content review artifact is unavailable")
        try:
            raw = path.read_bytes()
            value = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ContentWorkspaceError("Content review artifact is invalid") from exc
        expected = receipt.artifact_sha256.get(filename)
        actual = hashlib.sha256(raw).hexdigest()
        if expected != actual:
            raise ContentWorkspaceError("Content review artifact hash does not match")
        values[key] = _normalize_review_text(value)
    return receipt, values


def _review_x_thread(value: str) -> list[str]:
    posts = [
        post.strip()
        for post in re.split(r"\n\s*\n(?=[0-9]+/[0-9]+\s)", value.strip())
        if post.strip()
    ]
    if not posts:
        raise ContentWorkspaceError("X thread review cannot be empty")
    return posts


def _normalize_review_text(value: str) -> str:
    """Canonicalize browser and file newlines before review validation."""

    return value.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"


def save_content_review(
    *,
    data_root: Path,
    run_id: str,
    updates: dict[str, str] | None = None,
    decision: ContentReviewDecision | str = ContentReviewDecision.DRAFT,
    regenerate_target: str | None = None,
) -> ContentReviewReceipt:
    """Persist one non-overwriting owner edit/approval revision without publishing."""

    run = load_content_run(data_root, run_id)
    run_dir = content_run_directory(data_root, run_id)
    if any((run_dir / f"12_buildlog_{channel}.json").exists() for channel in ("linkedin", "x")) or (
        run_dir / "26_distribution_package.json"
    ).exists():
        raise ContentWorkspaceError(
            "This content is already sealed for distribution; create a new content run to edit it"
        )
    unknown = set(updates or {}) - set(_REVIEW_ARTIFACTS)
    if unknown:
        raise ContentWorkspaceError("Content review contains an unknown adaptation")
    previous = load_content_review(data_root, run_id)
    values = (
        dict(previous[1])
        if previous is not None
        else _draft_review_values(run.drafts)
    )
    for key, value in (updates or {}).items():
        values[key] = _normalize_review_text(value)
    if regenerate_target is not None:
        if regenerate_target not in _REVIEW_ARTIFACTS:
            raise ContentWorkspaceError("Select one known adaptation to regenerate")
        regenerated = _draft_review_values(build_content_drafts(run.brief))
        values[regenerate_target] = regenerated[regenerate_target]
    values = {key: _normalize_review_text(value) for key, value in values.items()}
    try:
        reviewed_drafts = ContentDrafts(
            canonical_story=values["canonical_story"],
            linkedin=values["linkedin"],
            x_thread=_review_x_thread(values["x_thread"]),
            x_post=values["x_post"].strip(),
            blog=values["blog"],
            youtube_script=values["youtube_script"],
            video_script=values["video_script"],
            storyboard=run.drafts.storyboard,
        )
    except ValidationError as exc:
        raise ContentWorkspaceError("Content review does not satisfy the output contract") from exc
    _validate_reviewed_drafts(run.brief, reviewed_drafts)
    values = _draft_review_values(_strip_public_claim_markers(reviewed_drafts))
    values = {key: _normalize_review_text(value) for key, value in values.items()}
    selected_decision = ContentReviewDecision(decision)
    previous_revision = previous[0].revision if previous is not None else 0
    revision = previous_revision + 1
    reviews_root = run_dir / "reviews"
    review_dir = reviews_root / f"review-{revision:04d}"
    try:
        _ensure_private_directory(reviews_root)
        if review_dir.exists() or review_dir.is_symlink():
            raise ContentWorkspaceError("Content review revision already exists")
        _ensure_private_directory(review_dir)
        hashes = {
            filename: hashlib.sha256(values[key].encode("utf-8")).hexdigest()
            for key, filename in _REVIEW_ARTIFACTS.items()
        }
        receipt = ContentReviewReceipt(
            run_id=run_id,
            revision=revision,
            decision=selected_decision,
            created_at=datetime.now(UTC).isoformat(),
            artifact_sha256=hashes,
            reset_target=regenerate_target,
        )
        for key, filename in _REVIEW_ARTIFACTS.items():
            _atomic_private_write(review_dir / filename, values[key])
        _atomic_private_write(
            review_dir / "review.json",
            _canonical_json(receipt.model_dump(mode="json")),
        )
    except (OSError, ResumeWorkspaceStorageError) as exc:
        raise ContentWorkspaceError("Private content review could not be saved") from exc
    return receipt


def approved_content_artifact(
    data_root: Path, run_id: str, channel: str
) -> tuple[str, Path, ContentReviewReceipt]:
    """Return the exact owner-approved LinkedIn/X artifact for BuildLog staging."""

    review = load_content_review(data_root, run_id)
    if review is None or review[0].decision is not ContentReviewDecision.APPROVED:
        raise ContentWorkspaceError("Approve the unified content review before BuildLog handoff")
    key = {"linkedin": "linkedin", "x": "x_post"}.get(channel)
    if key is None:
        raise ContentWorkspaceError("Content publishing channel is invalid")
    run_dir = content_run_directory(data_root, run_id)
    review_dir = _latest_review_directory(run_dir)
    if review_dir is None:
        raise ContentWorkspaceError("Approved content review is unavailable")
    path = review_dir / _REVIEW_ARTIFACTS[key]
    relative = path.relative_to(run_dir).as_posix()
    return relative, path, review[0]


def content_download(data_root: Path, run_id: str, download_name: str) -> tuple[str, bytes]:
    artifact_name = _DOWNLOADS.get(download_name)
    if artifact_name is None:
        raise ContentWorkspaceError("Content artifact is not downloadable")
    run_dir = content_run_directory(data_root, run_id)
    path = run_dir / artifact_name
    review_key = _DOWNLOAD_REVIEW_KEYS.get(download_name)
    if review_key is not None:
        review_dir = _latest_review_directory(run_dir)
        if review_dir is not None:
            path = review_dir / _REVIEW_ARTIFACTS[review_key]
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise ContentWorkspaceError("Content artifact is unavailable") from None
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise ContentWorkspaceError("Content artifact is unsafe")
    return artifact_name, path.read_bytes()
