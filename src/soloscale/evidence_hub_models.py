"""Private, metadata-only contracts for the local EvidenceHub catalog.

These records intentionally model provenance and review state, never source bodies or
retrieval excerpts.  Locators are private catalog metadata and must be hidden by any UI.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from soloscale.knowledge_models import NonBlankStr, NonNegativeInt, Sha256Digest
from soloscale.models import ContractModel


class TruthClass(StrEnum):
    PERSONAL_ARTIFACT = "personal_artifact"
    PERSONAL_CONTEXT = "personal_context"
    EXTERNAL_KNOWLEDGE = "external_knowledge"
    OUTCOME_RECEIPT = "outcome_receipt"


class ReceiptStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class CasePromotion(StrEnum):
    DRAFT = "draft"
    PROMOTED = "promoted"


class SourceRecord(ContractModel):
    """One canonical source, retaining its native identity and private provenance."""

    source_id: NonBlankStr
    native_id: NonBlankStr
    source_system: NonBlankStr
    source_type: NonBlankStr
    project: str | None = None
    original_locator: str | None = None
    captured_at: datetime
    source_at: datetime | None = None
    content_sha256: Sha256Digest
    sensitivity: NonBlankStr = "private"
    truth_class: TruthClass
    raw_available: bool = False
    adapter: NonBlankStr
    metadata: dict[str, str] = Field(default_factory=dict)

    @property
    def project_id(self) -> str | None:
        """Compatibility name used by application-neutral callers."""

        return self.project


class EvidenceItem(ContractModel):
    evidence_id: NonBlankStr
    source_id: NonBlankStr
    native_id: NonBlankStr
    evidence_type: NonBlankStr = "metadata"
    project: str | None = None
    captured_at: datetime
    source_at: datetime | None = None
    time_start: datetime | None = None
    time_end: datetime | None = None
    provenance_locator: str | None = None
    truth_class: TruthClass
    trust_state: NonBlankStr = "unverified"
    public_safe_summary: NonBlankStr
    relationships: list[NonBlankStr] = Field(default_factory=list)
    verification: dict[str, str] = Field(default_factory=dict)
    verification_status: NonBlankStr = "unverified"
    content_sha256: Sha256Digest

    @field_validator("relationships")
    @classmethod
    def validate_relationships(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("relationships must be unique")
        return value

    @model_validator(mode="after")
    def validate_time_range(self) -> EvidenceItem:
        if self.time_start and self.time_end and self.time_end < self.time_start:
            raise ValueError("evidence time range must not run backwards")
        return self

    @property
    def project_id(self) -> str | None:
        return self.project


class EvidenceBundle(ContractModel):
    bundle_id: NonBlankStr
    intent: NonBlankStr
    query: str | None = None
    evidence_ids: list[NonBlankStr] = Field(default_factory=list)
    coverage: list[NonBlankStr] = Field(default_factory=list)
    gaps: list[NonBlankStr] = Field(default_factory=list)
    filters: dict[str, str] = Field(default_factory=dict)
    created_at: datetime
    version: NonBlankStr = "1"
    bundle_sha256: Sha256Digest

    @model_validator(mode="after")
    def validate_membership(self) -> EvidenceBundle:
        if not self.evidence_ids:
            raise ValueError("bundles require evidence")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("bundle evidence ids must be unique")
        return self


class CaseRecord(ContractModel):
    case_id: NonBlankStr
    bundle_id: NonBlankStr
    title: NonBlankStr
    problem: NonBlankStr
    evidence_ids: list[NonBlankStr] = Field(default_factory=list)
    decisions: list[NonBlankStr] = Field(default_factory=list)
    implementation: list[NonBlankStr] = Field(default_factory=list)
    failures: list[NonBlankStr] = Field(default_factory=list)
    recovery: list[NonBlankStr] = Field(default_factory=list)
    results: list[NonBlankStr] = Field(default_factory=list)
    unknowns: list[NonBlankStr] = Field(default_factory=list)
    promotion: CasePromotion = CasePromotion.DRAFT
    created_at: datetime

    @property
    def promotion_state(self) -> str:
        return self.promotion.value


class AssetRecord(ContractModel):
    asset_id: NonBlankStr
    owner: NonBlankStr
    bundle_id: NonBlankStr | None = None
    case_id: NonBlankStr | None = None
    asset_type: NonBlankStr
    private_locator: str | None = None
    external_locator: str | None = None
    content_sha256: Sha256Digest
    provenance: dict[str, str] = Field(default_factory=dict)
    approval: NonBlankStr = "pending"
    evidence_ids: list[NonBlankStr] = Field(default_factory=list)
    created_at: datetime

    @model_validator(mode="after")
    def validate_locator(self) -> AssetRecord:
        if bool(self.private_locator) == bool(self.external_locator):
            raise ValueError("assets require exactly one private or external locator")
        if self.bundle_id is None and self.case_id is None:
            raise ValueError("assets require a bundle or case relationship")
        return self

    @property
    def application_owner(self) -> str:
        return self.owner

    @property
    def approval_state(self) -> str:
        return self.approval


class OutcomeReceipt(ContractModel):
    outcome_id: NonBlankStr
    outcome_type: NonBlankStr
    platform: NonBlankStr
    observed_at: datetime
    external_id: str | None = None
    url: str | None = None
    final_sha256: Sha256Digest
    status: NonBlankStr
    metadata: dict[str, str] = Field(default_factory=dict)
    evidence_ids: list[NonBlankStr] = Field(default_factory=list)
    case_id: NonBlankStr | None = None
    asset_id: NonBlankStr | None = None

    @model_validator(mode="after")
    def validate_asset_relationship(self) -> OutcomeReceipt:
        if self.asset_id is None:
            raise ValueError("outcomes require an asset relationship")
        return self

    @property
    def receipt_id(self) -> str:
        return self.outcome_id

    @property
    def occurred_at(self) -> datetime:
        return self.observed_at


class SyncReceipt(ContractModel):
    receipt_id: NonBlankStr
    adapter: NonBlankStr = "evidence_hub_refresh"
    status: ReceiptStatus
    started_at: datetime
    completed_at: datetime
    snapshot_sha256: Sha256Digest | None = None
    discovered_sources: NonNegativeInt = 0
    source_count: NonNegativeInt = 0
    evidence_count: NonNegativeInt = 0
    bundle_count: NonNegativeInt = 0
    case_count: NonNegativeInt = 0
    asset_count: NonNegativeInt = 0
    outcome_count: NonNegativeInt = 0
    source_counts: dict[str, NonNegativeInt] = Field(default_factory=dict)
    created_count: NonNegativeInt = 0
    updated_count: NonNegativeInt = 0
    unchanged_count: NonNegativeInt = 0
    error_count: NonNegativeInt = 0
    cursor: str | None = None
    errors: list[NonBlankStr] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_result(self) -> SyncReceipt:
        if self.status is ReceiptStatus.SUCCEEDED and self.snapshot_sha256 is None:
            raise ValueError("successful receipts require a snapshot hash")
        if self.status is ReceiptStatus.FAILED and not self.errors:
            raise ValueError("failed receipts require sanitized errors")
        if self.error_count != len(self.errors):
            raise ValueError("error_count must equal the number of sanitized errors")
        return self

    @property
    def error_code(self) -> str | None:
        """Compatibility view of the first sanitized error."""
        return self.errors[0] if self.errors else None

    @property
    def sync_id(self) -> str:
        return self.receipt_id


class EvidenceHubStatus(ContractModel):
    source_count: NonNegativeInt
    evidence_count: NonNegativeInt
    bundle_count: NonNegativeInt
    case_count: NonNegativeInt
    asset_count: NonNegativeInt
    outcome_count: NonNegativeInt
    source_counts: dict[str, NonNegativeInt] = Field(default_factory=dict)
    truth_class_counts: dict[str, NonNegativeInt] = Field(default_factory=dict)
    snapshot_sha256: Sha256Digest | None = None
    last_receipt: SyncReceipt | None = None
