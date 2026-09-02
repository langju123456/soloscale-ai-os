from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import Field, field_validator, model_validator

from soloscale.models import ContractModel, utc_now

NonBlankStr = Annotated[str, Field(min_length=1, pattern=r"\S")]
ReceiptId = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
]
CaseId = Annotated[
    str,
    Field(min_length=3, max_length=80, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"),
]
Sha256Digest = Annotated[
    str,
    Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
]


class EvidenceKind(StrEnum):
    CHAT = "chat"
    CODEX = "codex"
    TERMINAL = "terminal"
    DIFF = "diff"
    TEST = "test"
    CI = "ci"
    CODE = "code"
    DOCUMENT = "document"
    OTHER = "other"


class PracticeStage(StrEnum):
    EXPLAIN = "explain"
    TRACE = "trace"
    REBUILD = "rebuild"
    DEBUG = "debug"
    DEFEND = "defend"


class AttemptOutcome(StrEnum):
    PASS = "pass"
    NEEDS_WORK = "needs-work"


class DerivedCaseStatus(StrEnum):
    CAPTURED = "captured"
    IN_PRACTICE = "in-practice"
    SELF_ASSESSED_INTERVIEW_READY = "self-assessed-interview-ready"


class EngineeringState(StrEnum):
    RESOLVED = "resolved"


class EvidenceReceipt(ContractModel):
    id: ReceiptId
    kind: EvidenceKind
    source_path: NonBlankStr
    archived_path: NonBlankStr
    sha256: Sha256Digest
    byte_size: int = Field(gt=0)
    captured_at: datetime = Field(default_factory=utc_now)


class LearningCase(ContractModel):
    id: CaseId
    created_at: datetime = Field(default_factory=utc_now)
    title: NonBlankStr
    project: NonBlankStr
    problem: NonBlankStr
    expected_behavior: NonBlankStr
    actual_behavior: NonBlankStr
    root_cause: NonBlankStr
    resolution: NonBlankStr
    verification: list[NonBlankStr] = Field(min_length=1)
    concepts: list[NonBlankStr] = Field(min_length=1)
    evidence: list[EvidenceReceipt] = Field(min_length=1)
    engineering_state: EngineeringState = EngineeringState.RESOLVED
    repository: str | None = None
    alternatives_considered: list[str] = Field(default_factory=list)
    trade_offs: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_evidence(self) -> LearningCase:
        evidence_ids = [receipt.id for receipt in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence receipt ids must be unique")

        archived_paths = [receipt.archived_path for receipt in self.evidence]
        if len(archived_paths) != len(set(archived_paths)):
            raise ValueError("evidence archived paths must be unique")
        return self


class PracticeReceipt(ContractModel):
    id: ReceiptId
    source_path: NonBlankStr
    archived_path: NonBlankStr
    sha256: Sha256Digest
    byte_size: int = Field(gt=0)
    captured_at: datetime = Field(default_factory=utc_now)


class PracticeAttempt(ContractModel):
    case_id: CaseId
    stage: PracticeStage
    outcome: AttemptOutcome
    created_at: datetime = Field(default_factory=utc_now)
    note: str | None = None
    receipt: PracticeReceipt | None = None

    @model_validator(mode="after")
    def validate_outcome_evidence(self) -> PracticeAttempt:
        if self.outcome is AttemptOutcome.PASS and self.receipt is None:
            raise ValueError("pass attempts require a receipt")
        if self.outcome is AttemptOutcome.NEEDS_WORK and (
            self.note is None or not self.note.strip()
        ):
            raise ValueError("needs-work attempts require a nonblank note")
        return self


class MasterySnapshot(ContractModel):
    case_id: CaseId
    stage_results: dict[PracticeStage, AttemptOutcome | None]
    passed_stages: list[PracticeStage]
    next_stage: PracticeStage | None = None
    status: DerivedCaseStatus
    interview_ready: bool

    @field_validator("stage_results")
    @classmethod
    def validate_stage_results(
        cls,
        stage_results: dict[PracticeStage, AttemptOutcome | None],
    ) -> dict[PracticeStage, AttemptOutcome | None]:
        expected_stages = list(PracticeStage)
        if set(stage_results) != set(expected_stages):
            raise ValueError("stage_results must contain every practice stage exactly once")
        return {stage: stage_results[stage] for stage in expected_stages}

    @model_validator(mode="after")
    def validate_derived_fields(self) -> MasterySnapshot:
        expected_passed = [
            stage
            for stage in PracticeStage
            if self.stage_results[stage] is AttemptOutcome.PASS
        ]
        if self.passed_stages != expected_passed:
            raise ValueError("passed_stages must match pass results in practice-stage order")

        expected_next = next(
            (
                stage
                for stage in PracticeStage
                if self.stage_results[stage] is not AttemptOutcome.PASS
            ),
            None,
        )
        if self.next_stage is not expected_next:
            raise ValueError("next_stage must be the first practice stage not passed")

        all_passed = len(expected_passed) == len(PracticeStage)
        any_attempted = any(result is not None for result in self.stage_results.values())
        if all_passed:
            expected_status = DerivedCaseStatus.SELF_ASSESSED_INTERVIEW_READY
        elif any_attempted:
            expected_status = DerivedCaseStatus.IN_PRACTICE
        else:
            expected_status = DerivedCaseStatus.CAPTURED

        if self.status is not expected_status:
            raise ValueError("status does not match practice-stage results")
        if self.interview_ready is not all_passed:
            raise ValueError("interview_ready must be true exactly when all stages pass")
        return self
