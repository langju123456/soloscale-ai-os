"""Tracked, reusable engineering-story collections for Content Studio."""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
from functools import cache
from importlib import resources
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StoryReadiness(StrEnum):
    READY_FOR_PRODUCTION = "READY_FOR_PRODUCTION"
    NEEDS_EVIDENCE = "NEEDS_EVIDENCE"
    NEEDS_USER_INPUT = "NEEDS_USER_INPUT"
    DRAFT = "DRAFT"


class CanonicalStory(_StrictModel):
    story_id: str = Field(pattern=r"^M1-[0-9]{2}$")
    month: Literal[1]
    week: int = Field(ge=1, le=4)
    sequence: int = Field(ge=1, le=24)
    chapter: str
    title_cn: str
    working_title_en: str
    one_sentence_thesis: str
    original_user_realization: str
    fact: str
    architecture: str
    decision: str
    implementation: str
    failure: str
    evolution: str
    higher_level_insight: str
    audience_value: str
    evidence_candidates: list[str]
    verified_metrics: list[str]
    overclaim_guardrails: list[str]
    video_hook: str
    blog_thesis: str
    linkedin_angle: str
    x_thread_angle: str
    interview_angle: str
    recommended_visuals: list[str]
    primary_format: Literal["60–90 second video", "technical blog"]
    secondary_formats: list[
        Literal[
            "LinkedIn",
            "X thread",
            "Interview answer",
            "Portfolio case study",
            "Carousel",
        ]
    ]
    status: StoryReadiness


class MonthOneCanon(_StrictModel):
    schema_version: Literal["1.0"]
    collection_id: Literal["soloscale-month-one"]
    title: str
    month: Literal[1]
    narrative: str
    stories: list[CanonicalStory] = Field(min_length=24, max_length=24)

    @model_validator(mode="after")
    def validate_schedule(self) -> MonthOneCanon:
        ids = [story.story_id for story in self.stories]
        sequences = [story.sequence for story in self.stories]
        if len(ids) != len(set(ids)):
            raise ValueError("story IDs must be unique")
        if sequences != list(range(1, 25)):
            raise ValueError("stories must be ordered from sequence 1 through 24")
        if Counter(story.week for story in self.stories) != Counter(
            {1: 6, 2: 6, 3: 6, 4: 6}
        ):
            raise ValueError("each Month 1 week must contain six stories")
        return self


@cache
def load_month_one_canon() -> MonthOneCanon:
    resource = resources.files("soloscale").joinpath("content_data/month_one.json")
    return MonthOneCanon.model_validate_json(resource.read_text(encoding="utf-8"))


def month_one_readiness_counts() -> dict[StoryReadiness, int]:
    counts = Counter(story.status for story in load_month_one_canon().stories)
    return {status: counts[status] for status in StoryReadiness}
