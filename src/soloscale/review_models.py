"""Contracts for the human Review Workbench over Evidence Agent drafts.

A review item is one model claim from a saved ``AgentRunResult`` plus the exact cited
evidence the model saw.  The models here are response/request shapes only; state rules
live in ``review_workflow`` and persistence in ``review_store``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

MAX_CORRECTION_CHARS = 4_000
MAX_NOTE_CHARS = 2_000
MAX_REVIEWER_CHARS = 80
MAX_BATCH_ITEMS = 100
DEFAULT_REVIEWER = "local-operator"

ReviewerLabel = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_REVIEWER_CHARS),
]
ReviewNote = Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_NOTE_CHARS)]
CorrectionText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_CORRECTION_CHARS),
]


class ReviewState(StrEnum):
    PENDING_REVIEW = "PENDING_REVIEW"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    CONFIRMED = "CONFIRMED"
    CORRECTED = "CORRECTED"
    REJECTED = "REJECTED"


class EvidenceStatus(StrEnum):
    """Rule-based citation verification result; deliberately not a confidence score."""

    FULLY_VERIFIED = "FULLY_VERIFIED"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    HASH_MISMATCH = "HASH_MISMATCH"


class CitationCheck(StrEnum):
    VERIFIED = "VERIFIED"
    DOCUMENT_REVISED = "DOCUMENT_REVISED"
    HASH_MISMATCH = "HASH_MISMATCH"
    INTEGRITY_FAILED = "INTEGRITY_FAILED"
    SOURCE_MISSING = "SOURCE_MISSING"
    NOT_IN_RECEIPT = "NOT_IN_RECEIPT"
    UNCHECKED = "UNCHECKED"


class ReviewAction(StrEnum):
    CONFIRM = "confirm"
    CORRECT = "correct"
    REJECT = "reject"
    REOPEN = "reopen"


class EventAction(StrEnum):
    IMPORTED = "IMPORTED"
    CONFIRMED = "CONFIRMED"
    CORRECTED = "CORRECTED"
    REJECTED = "REJECTED"
    REOPENED = "REOPENED"


class AuthorKind(StrEnum):
    MODEL = "MODEL"
    REVIEWER = "REVIEWER"


class ConfirmRequirement(StrEnum):
    NONE = "NONE"
    NOTE_REQUIRED = "NOTE_REQUIRED"
    BLOCKED = "BLOCKED"


class _ReviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReviewCitation(_ReviewModel):
    ordinal: int = Field(ge=0)
    chunk_id: str
    document_id: str | None
    source_kind: str | None
    external_id: str | None
    title: str | None
    role: str | None
    source_timestamp: str | None
    recorded_chunk_sha256: str | None
    recorded_document_sha256: str | None
    model_visible_excerpt: str | None
    check: CitationCheck
    current_chunk_sha256: str | None
    current_document_sha256: str | None
    document_changed: bool
    checked_at: datetime


class TextVersion(_ReviewModel):
    version: int = Field(ge=1)
    text: str
    text_sha256: str
    author_kind: AuthorKind
    reviewer: str | None
    note: str | None
    created_at: datetime


class ReviewEvent(_ReviewModel):
    event_id: int
    action: EventAction
    from_state: ReviewState | None
    to_state: ReviewState
    from_revision: int | None
    to_revision: int
    text_version: int
    reviewer: str | None
    note: str | None
    created_at: datetime


class ReviewItemSummary(_ReviewModel):
    item_id: str
    run_id: str
    claim_index: int = Field(ge=0)
    state: ReviewState
    evidence_status: EvidenceStatus
    revision: int = Field(ge=1)
    current_version: int = Field(ge=1)
    current_text: str
    citation_count: int = Field(ge=0)
    verified_citation_count: int = Field(ge=0)
    confirm_requirement: ConfirmRequirement
    allowed_actions: list[ReviewAction]
    updated_at: datetime


class ReviewRunGaps(_ReviewModel):
    unsupported: list[str]
    open_questions: list[str]
    limitations: list[str]


class ReviewRunSummary(_ReviewModel):
    run_id: str
    question: str
    model: str
    prompt_version: str
    draft_created_at: datetime
    draft_sha256: str
    input_manifest_sha256: str
    retrieval_manifest_sha256: str
    knowledge_index_checked: bool
    imported_at: datetime
    item_count: int = Field(ge=0)
    state_counts: dict[str, int]
    gaps: ReviewRunGaps


class ReviewItemDetail(_ReviewModel):
    item: ReviewItemSummary
    run: ReviewRunSummary
    original_text: str
    citations: list[ReviewCitation]
    versions: list[TextVersion]
    events: list[ReviewEvent]


class DraftCandidate(_ReviewModel):
    run_id: str
    question: str
    model: str
    created_at: datetime
    claim_count: int = Field(ge=0)
    imported: bool


class DraftListing(_ReviewModel):
    drafts: list[DraftCandidate]
    skipped_invalid: int = Field(ge=0)


class ImportResponse(_ReviewModel):
    created: bool
    run: ReviewRunSummary
    items: list[ReviewItemSummary]


class DiffOp(_ReviewModel):
    op: Literal["equal", "insert", "delete"]
    text: str


class VersionDiff(_ReviewModel):
    item_id: str
    from_version: int = Field(ge=1)
    to_version: int = Field(ge=1)
    from_text: str
    to_text: str
    ops: list[DiffOp]


class ItemList(_ReviewModel):
    items: list[ReviewItemSummary]


class RunList(_ReviewModel):
    runs: list[ReviewRunSummary]


class _ActionRequest(_ReviewModel):
    expected_revision: int = Field(ge=1)
    reviewer: ReviewerLabel = DEFAULT_REVIEWER


class ConfirmRequest(_ActionRequest):
    note: ReviewNote = ""


class CorrectRequest(_ActionRequest):
    text: CorrectionText
    note: ReviewNote = ""


class RejectRequest(_ActionRequest):
    note: ReviewNote = ""


class ReopenRequest(_ActionRequest):
    note: ReviewNote = ""


class BatchConfirmEntry(_ReviewModel):
    item_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1)


class BatchConfirmRequest(_ReviewModel):
    items: list[BatchConfirmEntry] = Field(min_length=1, max_length=MAX_BATCH_ITEMS)
    reviewer: ReviewerLabel = DEFAULT_REVIEWER
    note: ReviewNote = ""
