from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from soloscale.models import ContractModel

NonBlankStr = Annotated[str, Field(min_length=1, pattern=r"\S")]
StableId = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
]
Sha256Digest = Annotated[
    str,
    Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
]


class TruthStage(StrEnum):
    RAW_STATEMENT = "RAW_STATEMENT"
    DISTILLED_INSIGHT = "DISTILLED_INSIGHT"
    DECISION = "DECISION"
    IMPLEMENTED_CAPABILITY = "IMPLEMENTED_CAPABILITY"
    VERIFIED_EVIDENCE = "VERIFIED_EVIDENCE"
    MASTERY_RECEIPT = "MASTERY_RECEIPT"
    APPROVED_CLAIM = "APPROVED_CLAIM"


class MasteryLevel(StrEnum):
    L0_SEEN = "L0 Seen"
    L1_EXPLAIN = "L1 Explain"
    L2_TRACE = "L2 Trace"
    L3_REBUILD = "L3 Rebuild"
    L4_DEBUG = "L4 Debug"
    L5_DEFEND = "L5 Defend"


class MasteryAction(StrEnum):
    EXPLAIN = "Explain"
    TRACE = "Trace"
    REBUILD = "Rebuild"
    DEBUG = "Debug"
    DEFEND = "Defend"


class OwnershipConfidence(StrEnum):
    CONFIRMED = "confirmed"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class SourceRecord(ContractModel):
    id: StableId
    source_kind: NonBlankStr
    title: NonBlankStr
    source_path: NonBlankStr
    content_sha256: Sha256Digest
    truth_stage: Literal[TruthStage.RAW_STATEMENT] = TruthStage.RAW_STATEMENT


class ReasoningArtifact(ContractModel):
    id: StableId
    summary: NonBlankStr
    source_record_ids: list[StableId] = Field(min_length=1)
    limitations: list[NonBlankStr] = Field(default_factory=list)
    truth_stage: Literal[TruthStage.RAW_STATEMENT] = TruthStage.RAW_STATEMENT


class DistilledInsight(ContractModel):
    id: StableId
    statement: NonBlankStr
    reasoning_artifact_ids: list[StableId] = Field(min_length=1)
    truth_stage: Literal[TruthStage.DISTILLED_INSIGHT] = TruthStage.DISTILLED_INSIGHT


class EngineeringDecision(ContractModel):
    id: StableId
    decision: NonBlankStr
    rationale: NonBlankStr
    insight_ids: list[StableId] = Field(min_length=1)
    alternatives_considered: list[NonBlankStr] = Field(default_factory=list)
    trade_offs: list[NonBlankStr] = Field(default_factory=list)
    truth_stage: Literal[TruthStage.DECISION] = TruthStage.DECISION


class ImplementedCapability(ContractModel):
    id: StableId
    name: NonBlankStr
    description: NonBlankStr
    decision_ids: list[StableId] = Field(min_length=1)
    code_anchor_ids: list[StableId] = Field(min_length=1)
    truth_stage: Literal[TruthStage.IMPLEMENTED_CAPABILITY] = (
        TruthStage.IMPLEMENTED_CAPABILITY
    )


class TechnicalConcept(ContractModel):
    id: StableId
    name: NonBlankStr
    explanation: NonBlankStr
    capability_ids: list[StableId] = Field(min_length=1)
    glossary: dict[str, NonBlankStr] = Field(default_factory=dict)


class CodeAnchor(ContractModel):
    id: StableId
    repository: NonBlankStr
    branch: NonBlankStr
    commit: NonBlankStr
    file: NonBlankStr
    symbol: NonBlankStr
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    file_sha256: Sha256Digest
    capability_ids: list[StableId] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_line_range(self) -> CodeAnchor:
        if self.line_end < self.line_start:
            raise ValueError("line_end must be greater than or equal to line_start")
        return self


class VerificationAnchor(ContractModel):
    id: StableId
    repository: NonBlankStr
    branch: NonBlankStr
    commit: NonBlankStr
    file: NonBlankStr
    symbol: NonBlankStr
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    file_sha256: Sha256Digest
    verification_command: NonBlankStr
    receipt_state: Literal["committed_test_definition"] = "committed_test_definition"
    capability_ids: list[StableId] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_line_range(self) -> VerificationAnchor:
        if self.line_end < self.line_start:
            raise ValueError("line_end must be greater than or equal to line_start")
        return self


class ContributionAttribution(ContractModel):
    case_id: StableId
    problem_framed_by: str | None = None
    requirements_defined_by: str | None = None
    decisions_made_or_approved_by: str | None = None
    implementation_performed_by: str | None = None
    reviewed_by: str | None = None
    verified_by: str | None = None
    ai_assistance: list[NonBlankStr] = Field(default_factory=list)
    independent_modification_receipt: str | None = None
    ownership_confidence: OwnershipConfidence = OwnershipConfidence.UNKNOWN
    unknowns: list[NonBlankStr] = Field(default_factory=list)


class MasteryState(ContractModel):
    case_id: StableId
    level: MasteryLevel
    completed_actions: list[MasteryAction] = Field(default_factory=list)
    next_action: MasteryAction | None
    interview_ready: bool
    receipt_ids: list[StableId] = Field(default_factory=list)
    truth_stage: Literal[TruthStage.MASTERY_RECEIPT] = TruthStage.MASTERY_RECEIPT

    @model_validator(mode="after")
    def validate_level(self) -> MasteryState:
        order = list(MasteryAction)
        level_index = list(MasteryLevel).index(self.level)
        if self.completed_actions != order[:level_index]:
            raise ValueError("completed_actions must match the mastery level in order")
        expected_next = order[level_index] if level_index < len(order) else None
        if self.next_action is not expected_next:
            raise ValueError("next_action must be the first incomplete mastery action")
        if self.interview_ready is not (self.level is MasteryLevel.L5_DEFEND):
            raise ValueError("interview_ready requires L5 Defend")
        return self


class LearningTask(ContractModel):
    id: StableId
    case_id: StableId
    title: NonBlankStr
    action: MasteryAction
    objective: NonBlankStr
    instructions: list[NonBlankStr] = Field(min_length=1)
    anchor_ids: list[StableId] = Field(min_length=1)
    completion_evidence: NonBlankStr
    status: Literal["pending", "complete"] = "pending"


class LearningResponseReceipt(ContractModel):
    id: StableId
    run_id: StableId
    case_id: StableId
    stage: Literal[MasteryAction.EXPLAIN, MasteryAction.TRACE]
    response: Annotated[str, Field(min_length=1, max_length=20_000, pattern=r"\S")]
    submitted_at: datetime
    status: Literal["SUBMITTED_REQUIRES_REVIEW"] = "SUBMITTED_REQUIRES_REVIEW"
    mastery_advanced: Literal[False] = False
    truth_stage: Literal[TruthStage.RAW_STATEMENT] = TruthStage.RAW_STATEMENT


class LearningProjectBinding(ContractModel):
    """Explicit Evidence source identity for the project behind one Learning case."""

    project_source_id: StableId
    project: NonBlankStr
    repository: NonBlankStr
    branch: NonBlankStr
    commit: NonBlankStr


class InterviewQuestion(ContractModel):
    id: StableId
    case_id: StableId
    prompt: NonBlankStr
    target_level: MasteryLevel
    anchor_ids: list[StableId] = Field(min_length=1)
    strong_answer_signals: list[NonBlankStr] = Field(min_length=1)


class ClaimEligibility(ContractModel):
    case_id: StableId
    target_requirement: NonBlankStr
    engineering_truth_stage: TruthStage
    ownership_confidence: OwnershipConfidence
    mastery_level: MasteryLevel
    interview_ready: bool
    resume_eligible: bool
    approved_claim: str | None = None
    safe_verbs: list[NonBlankStr] = Field(default_factory=list)
    prohibited_phrasing: list[NonBlankStr] = Field(default_factory=list)
    rationale: NonBlankStr

    @model_validator(mode="after")
    def validate_claim_gate(self) -> ClaimEligibility:
        claim_ready = (
            self.engineering_truth_stage is TruthStage.APPROVED_CLAIM
            and self.ownership_confidence is OwnershipConfidence.CONFIRMED
        )
        if self.resume_eligible is not claim_ready:
            raise ValueError("resume_eligible must match truth and ownership gates")
        if self.resume_eligible != (self.approved_claim is not None):
            raise ValueError("approved_claim must exist exactly when resume_eligible is true")
        if self.interview_ready is not (self.mastery_level is MasteryLevel.L5_DEFEND):
            raise ValueError("interview_ready requires L5 Defend")
        return self


class KnowledgeGraphNode(ContractModel):
    id: StableId
    kind: NonBlankStr
    label: NonBlankStr
    truth_stage: TruthStage | None = None
    detail: dict[str, object] = Field(default_factory=dict)


class KnowledgeGraphEdge(ContractModel):
    source: StableId
    target: StableId
    relation: NonBlankStr


class LearningTraceabilityRun(ContractModel):
    run_id: StableId
    case_id: StableId
    evidence_bundle_id: str | None = None
    project_source_id: StableId
    case_kind: Literal["SEED_CASE", "DOGFOOD_CASE"] = "SEED_CASE"
    repository: NonBlankStr
    branch: NonBlankStr
    commit: NonBlankStr
    cache_key: Sha256Digest
    cache_hit: bool
    private_run_path: NonBlankStr
    artifact_files: list[NonBlankStr]
    model_calls: int = Field(ge=0, le=1)
    network_used: bool = False
    engineering_state: Literal["ENGINEERING_VERIFIED"] = "ENGINEERING_VERIFIED"
    mastery_level: MasteryLevel
    next_action: MasteryAction | None
    limitations: list[NonBlankStr] = Field(default_factory=list)
