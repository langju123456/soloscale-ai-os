"""Turn one tracked Month 1 story into a grounded Content Studio brief."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from soloscale.content_canon import CanonicalStory, StoryReadiness, load_month_one_canon
from soloscale.content_models import ClaimStatus, ContentBrief, ContentClaim
from soloscale.story_mining import StoryMiningError, load_story_candidate


class ContentCanonError(ValueError):
    """Raised when a tracked story is not ready for automatic production."""


def month_one_story(story_id: str) -> CanonicalStory:
    """Resolve one stable Month 1 story identity."""

    story = next(
        (item for item in load_month_one_canon().stories if item.story_id == story_id),
        None,
    )
    if story is None:
        raise ContentCanonError("This Month 1 story is unavailable")
    return story


def content_brief_from_month_one_story(
    story_id: str,
    *,
    language: Literal["English", "中文"] = "中文",
) -> ContentBrief:
    """Create a body-bounded brief from a production-ready Six-Layer story."""

    story = month_one_story(story_id)
    if story.status is not StoryReadiness.READY_FOR_PRODUCTION:
        raise ContentCanonError(
            "This story still needs evidence or owner input before AI production"
        )
    layers = (
        ("fact", story.fact),
        ("architecture", story.architecture),
        ("decision", story.decision),
        ("implementation", story.implementation),
        ("failure", story.failure),
        ("evolution", story.evolution),
    )
    claims = [
        ContentClaim(
            id=f"CLAIM-{index:02d}",
            text=value,
            status=ClaimStatus.OBSERVED,
            receipt=f"month-one-canon:{story.story_id}#{field}",
            limits=(
                "Stay within the tracked Six-Layer story and its overclaim guardrails."
            ),
        )
        for index, (field, value) in enumerate(layers, start=1)
    ]
    for metric in story.verified_metrics[:2]:
        claims.append(
            ContentClaim(
                id=f"CLAIM-{len(claims) + 1:02d}",
                text=metric,
                status=ClaimStatus.VERIFIED,
                receipt=f"month-one-canon:{story.story_id}#verified-metric",
                limits="Keep the measurement tied to the recorded local run scope.",
            )
        )
    topic = story.title_cn if language == "中文" else story.working_title_en
    call_to_action = (
        "如果你也遇到过类似的工程取舍，欢迎分享你的做法。"
        if language == "中文"
        else "Share a similar engineering trade-off you have faced."
    )
    return ContentBrief(
        topic=topic,
        audience=(
            "正在用 AI 构建真实产品的工程师和独立开发者"
            if language == "中文"
            else "AI engineers and solo builders shipping real products"
        ),
        language=language,
        call_to_action=call_to_action,
        source_label=f"SoloScale Month 1 Content Canon · {story.story_id}",
        claims=claims,
        evidence_gaps=list(story.overclaim_guardrails),
        evidence_filters={
            "canon_story_id": story.story_id,
            "readiness": story.status.value,
            "primary_format": story.primary_format,
        },
    )


def content_brief_from_story_candidate(
    data_root: Path,
    candidate_id: str,
    *,
    language: Literal["English", "中文"] = "中文",
) -> ContentBrief:
    """Turn one mined Story Bank candidate into the same grounded brief contract."""

    try:
        candidate = load_story_candidate(data_root, candidate_id)
    except StoryMiningError as exc:
        raise ContentCanonError(str(exc)) from exc
    claims = [
        ContentClaim(
            id=f"CLAIM-{index:02d}",
            text=summary,
            status=ClaimStatus.OBSERVED,
            receipt=f"story-candidate:{candidate.candidate_id}#evidence:{evidence_id}",
            limits=(
                "Keep the claim inside the mined evidence summary; do not add new facts."
            ),
        )
        for index, (evidence_id, summary) in enumerate(
            zip(
                candidate.source_evidence_ids,
                candidate.evidence_summaries,
                strict=True,
            ),
            start=1,
        )
    ]
    topic = (
        candidate.working_title_cn
        if language == "中文"
        else candidate.working_title_en
    )
    return ContentBrief(
        topic=topic,
        audience=(
            "正在用 AI 构建真实产品的工程师和独立开发者"
            if language == "中文"
            else "AI engineers and solo builders shipping real products"
        ),
        language=language,
        call_to_action=(
            "如果你也遇到过类似的工程取舍，欢迎分享你的做法。"
            if language == "中文"
            else "Share a similar engineering trade-off you have faced."
        ),
        source_label=f"Story Bank candidate · {candidate.candidate_id}",
        claims=claims,
        evidence_gaps=[],
        evidence_filters={"story_candidate_id": candidate.candidate_id},
    )
