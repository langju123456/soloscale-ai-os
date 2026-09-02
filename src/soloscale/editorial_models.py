"""Strict contracts for model-traceable editorial work."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from soloscale.models import ContractModel


class ProviderKind(StrEnum):
    TEMPLATE = "template"
    CODEX_SESSION = "codex_session"
    SOLOSCALE_HOSTED = "soloscale_hosted"
    OLLAMA = "ollama"
    OPENAI_COMPATIBLE = "openai_compatible"
    HUGGINGFACE = "huggingface"
    FUTURE_EXTERNAL = "future_external"


class EditorialRole(StrEnum):
    WRITER = "writer"
    REVIEWER = "reviewer"
    REVISER = "reviser"


class RunStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class ProviderIdentity(ContractModel):
    kind: ProviderKind
    provider: str = Field(min_length=1, max_length=120)
    model: str | None = Field(default=None, min_length=1, max_length=240)
    base_url: str | None = Field(default=None, min_length=1, max_length=500)


class EditorialProvenance(ContractModel):
    """Receipt for one writer, reviewer, or reviser execution."""

    role: EditorialRole
    provider: ProviderIdentity
    exact_model: str = Field(min_length=1, max_length=240)
    reasoning: str | None = Field(default=None, min_length=1, max_length=120)
    prompt_version: str = Field(min_length=1, max_length=120)
    input_artifact_hashes: dict[str, str] = Field(min_length=1, max_length=40)
    output_artifact_hashes: dict[str, str] = Field(default_factory=dict, max_length=40)
    started_at: datetime
    completed_at: datetime | None = None
    network_used: bool = False
    token_usage: dict[str, int] | None = None
    cost_usd: float | None = Field(default=None, ge=0)
    status: RunStatus
    errors: list[str] = Field(default_factory=list, max_length=20)
    fresh_context: bool = True

    @field_validator("input_artifact_hashes", "output_artifact_hashes")
    @classmethod
    def validate_artifact_hashes(cls, values: dict[str, str]) -> dict[str, str]:
        if any(not name.strip() for name in values):
            raise ValueError("artifact hash names must not be blank")
        if any(not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in values.values()):
            raise ValueError("artifact hashes must be lowercase SHA-256 values")
        return values

    @field_validator("token_usage")
    @classmethod
    def validate_token_usage(cls, value: dict[str, int] | None) -> dict[str, int] | None:
        if value is not None and any(count < 0 for count in value.values()):
            raise ValueError("token usage must not be negative")
        return value

    @model_validator(mode="after")
    def validate_completion(self) -> EditorialProvenance:
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        if self.status is RunStatus.SUCCEEDED and not self.output_artifact_hashes:
            raise ValueError("succeeded provenance requires output artifact hashes")
        if self.status is RunStatus.FAILED and not self.errors:
            raise ValueError("failed provenance requires errors")
        return self


class ReviewCategory(StrEnum):
    FACTUAL_RISK = "factual_risk"
    PRIVACY = "privacy"
    CROSS_ARTIFACT_INCONSISTENCY = "cross_artifact_inconsistency"
    UNSUPPORTED_IMPLICATION = "unsupported_implication"
    EDITORIAL_PREFERENCE = "editorial_preference"
    PLATFORM_FIT = "platform_fit"
    VOICE_DRIFT = "voice_drift"
    REPETITION = "repetition"
    HOOK_PAYOFF = "hook_payoff"
    PROVENANCE_GAP = "provenance_gap"


class ReviewSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class ReviewFinding(ContractModel):
    finding_id: str = Field(pattern=r"^FINDING-[0-9]{2}$")
    category: ReviewCategory
    severity: ReviewSeverity
    artifact: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=1200)
    before: str | None = Field(default=None, max_length=1600)
    after: str | None = Field(default=None, max_length=1600)


class ReviewResult(ContractModel):
    overall_verdict: str = Field(min_length=1, max_length=240)
    scorecard: dict[str, int] = Field(default_factory=dict, max_length=20)
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=60)
    artifact_verdicts: dict[str, str] = Field(default_factory=dict, max_length=20)
    top_changes: list[str] = Field(default_factory=list, max_length=12)
    publication_recommendation: str = Field(min_length=1, max_length=500)
    provenance: EditorialProvenance

    @model_validator(mode="after")
    def blockers_require_rejection(self) -> ReviewResult:
        if self.provenance.role is not EditorialRole.REVIEWER:
            raise ValueError("review provenance must have reviewer role")
        if any(score < 0 or score > 10 for score in self.scorecard.values()):
            raise ValueError("review scorecard values must be between 0 and 10")
        return self


class RevisionDecision(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    PARTIALLY_ACCEPTED = "PARTIALLY_ACCEPTED"


class RevisionRecord(ContractModel):
    finding_id: str = Field(pattern=r"^FINDING-[0-9]{2}$")
    decision: RevisionDecision
    reason: str = Field(min_length=1, max_length=1200)


class RevisionResult(ContractModel):
    decisions: list[RevisionRecord] = Field(default_factory=list, max_length=60)
    provenance: EditorialProvenance

    @model_validator(mode="after")
    def reviser_provenance_required(self) -> RevisionResult:
        if self.provenance.role is not EditorialRole.REVISER:
            raise ValueError("revision provenance must have reviser role")
        return self


class AuthorVoiceProfile(ContractModel):
    """Versioned, operator-owned constraints; never inferred as a public fact."""

    profile_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    version: str = Field(pattern=r"^v[0-9]+(?:\.[0-9]+){0,2}$")
    voice_traits: list[str] = Field(min_length=1, max_length=16)
    preferred_phrases: list[str] = Field(default_factory=list, max_length=30)
    prohibited_phrases: list[str] = Field(default_factory=list, max_length=30)
    audience_notes: str | None = Field(default=None, max_length=1200)
    updated_at: datetime

    @field_validator("voice_traits", "preferred_phrases", "prohibited_phrases")
    @classmethod
    def no_blank_values(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("voice profile values must not be blank")
        return values
