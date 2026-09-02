"""Strict contracts for repo-scoped Skills, routes, and private Run Receipts."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SkillContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SkillStatus(StrEnum):
    DRAFT = "DRAFT"
    CANDIDATE = "CANDIDATE"
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"
    BLOCKED = "BLOCKED"


class SkillRiskClass(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class SkillRunStatus(StrEnum):
    ROUTED = "ROUTED"
    AWAITING_HUMAN_GATE = "AWAITING_HUMAN_GATE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class SkillChangeDecision(StrEnum):
    NO_SKILL_CHANGE = "NO_SKILL_CHANGE"
    CREATE_CANDIDATE_SKILL = "CREATE_CANDIDATE_SKILL"
    PROPOSE_SKILL_UPDATE = "PROPOSE_SKILL_UPDATE"
    DEPRECATE_SKILL = "DEPRECATE_SKILL"


class SkillRegistryEntry(SkillContractModel):
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    current_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    status: SkillStatus
    description: str = Field(min_length=1, max_length=500)
    trigger_keywords: list[str] = Field(min_length=1, max_length=50)
    semantic_intent: list[str] = Field(min_length=1, max_length=20)
    primary_workstream: str = Field(min_length=1, max_length=80)
    system_layer_tags: list[str] = Field(min_length=1, max_length=20)
    risk_class: SkillRiskClass
    input_contract: str = Field(min_length=1, max_length=240)
    output_contract: str = Field(min_length=1, max_length=240)
    human_gates: list[str] = Field(default_factory=list, max_length=12)
    recommended_model_route: dict[str, str]
    dependencies: list[str] = Field(default_factory=list, max_length=12)
    incompatible_skills: list[str] = Field(default_factory=list, max_length=12)
    last_validated_date: date | None = None
    last_validated_run_receipt: str | None = Field(default=None, max_length=200)
    success_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    owner: str = Field(min_length=1, max_length=100)
    deprecation_or_replacement: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def validate_phase_routes(self) -> SkillRegistryEntry:
        expected = {"discovery", "decision", "implementation", "verification", "review"}
        if set(self.recommended_model_route) != expected:
            raise ValueError("recommended_model_route must define the five canonical phases")
        if any(not value.strip() for value in self.recommended_model_route.values()):
            raise ValueError("recommended model routes must not be blank")
        if self.name in self.dependencies or self.name in self.incompatible_skills:
            raise ValueError("a Skill cannot depend on or be incompatible with itself")
        return self


class SkillRegistry(SkillContractModel):
    schema_version: Literal["1.0"] = "1.0"
    skills: list[SkillRegistryEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_graph(self) -> SkillRegistry:
        names = [skill.name for skill in self.skills]
        if len(names) != len(set(names)):
            raise ValueError("Skill names must be unique")
        known = set(names)
        for skill in self.skills:
            unknown = (set(skill.dependencies) | set(skill.incompatible_skills)) - known
            if unknown:
                raise ValueError(f"{skill.name} references unknown Skills: {sorted(unknown)}")
            if len(skill.dependencies) != len(set(skill.dependencies)):
                raise ValueError(f"{skill.name} has duplicate dependencies")
            if len(skill.incompatible_skills) != len(set(skill.incompatible_skills)):
                raise ValueError(f"{skill.name} has duplicate incompatible Skills")

        entries = {skill.name: skill for skill in self.skills}
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(name: str) -> None:
            if name in visited:
                return
            if name in visiting:
                raise ValueError("Skill dependency graph contains a cycle")
            visiting.add(name)
            for dependency in entries[name].dependencies:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)

        for name in names:
            visit(name)
        return self

    def get(self, name: str) -> SkillRegistryEntry:
        for skill in self.skills:
            if skill.name == name:
                return skill
        raise KeyError(name)


class SkillTaskEnvelope(SkillContractModel):
    task_id: str = Field(default_factory=lambda: f"skill-task-{uuid4().hex[:12]}")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    objective: str = Field(min_length=1, max_length=4000)
    desired_user_or_external_outcome: str = Field(min_length=1, max_length=1200)
    requested_artifacts: list[str] = Field(default_factory=list, max_length=30)
    project_or_workstream: str = Field(min_length=1, max_length=120)
    available_inputs: list[str] = Field(default_factory=list, max_length=30)
    evidence_requirements: list[str] = Field(default_factory=list, max_length=30)
    constraints: list[str] = Field(default_factory=list, max_length=40)
    publication_intent: bool = False
    deployment_intent: bool = False
    application_submission_intent: bool = False
    paid_api_intent: bool = False
    cost_boundary: str | None = Field(default=None, max_length=300)
    privacy_boundary: list[str] = Field(default_factory=list, max_length=20)
    completion_condition: str = Field(min_length=1, max_length=1200)
    known_non_goals: list[str] = Field(default_factory=list, max_length=40)


class SelectedSkill(SkillContractModel):
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    status: SkillStatus
    reason: str = Field(min_length=1, max_length=500)


class PhaseModelRoute(SkillContractModel):
    phase: Literal["discovery", "decision", "implementation", "verification", "review"]
    recommended: str = Field(min_length=1, max_length=120)
    actual_provider_used: str | None = Field(default=None, max_length=120)
    actual_model_used: str | None = Field(default=None, max_length=120)
    actual_reasoning_effort: str | None = Field(default=None, max_length=80)
    actual_agent_used: str | None = Field(default=None, max_length=120)


class SkillTaskRoute(SkillContractModel):
    task: SkillTaskEnvelope
    primary_skill: SelectedSkill
    supporting_skills: list[SelectedSkill] = Field(default_factory=list)
    dependency_order: list[str] = Field(min_length=1)
    model_route: list[PhaseModelRoute] = Field(min_length=5, max_length=5)
    human_gates: list[str] = Field(default_factory=list)
    unmet_preconditions: list[str] = Field(default_factory=list)
    expected_receipts: list[str] = Field(min_length=1)
    routing_reason: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_selected_route(self) -> SkillTaskRoute:
        selected = [self.primary_skill, *self.supporting_skills]
        selected_names = [skill.name for skill in selected]
        if len(selected_names) != len(set(selected_names)):
            raise ValueError("selected Skills must be unique")
        if set(selected_names) != set(self.dependency_order):
            raise ValueError("selected Skills must match dependency_order exactly")
        if len(self.dependency_order) != len(set(self.dependency_order)):
            raise ValueError("dependency_order must contain unique Skills")
        phases = [route.phase for route in self.model_route]
        if phases != ["discovery", "decision", "implementation", "verification", "review"]:
            raise ValueError("model routes must preserve canonical phase order")
        return self


class RepositoryRunState(SkillContractModel):
    repository: str = Field(min_length=1, max_length=500)
    branch: str = Field(min_length=1, max_length=300)
    sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")


class ArtifactHash(SkillContractModel):
    artifact_id: str = Field(min_length=1, max_length=200)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DeterministicCheck(SkillContractModel):
    name: str = Field(min_length=1, max_length=200)
    status: Literal["PASSED", "FAILED", "NOT_RUN"]
    detail: str | None = Field(default=None, max_length=1000)


class SkillFailure(SkillContractModel):
    phase: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=1000)
    hypothesis: str | None = Field(default=None, max_length=1000)


class RunOutcomeState(SkillContractModel):
    workflow_completed: bool = False
    artifact_generated: bool = False
    human_approved: bool = False
    externally_published_or_submitted: bool = False
    external_outcome_observed: bool = False

    @model_validator(mode="after")
    def preserve_state_boundaries(self) -> RunOutcomeState:
        if self.externally_published_or_submitted and not self.human_approved:
            raise ValueError("external publication or submission requires human approval")
        if self.externally_published_or_submitted and not self.artifact_generated:
            raise ValueError("external publication or submission requires a generated artifact")
        if self.external_outcome_observed and not self.externally_published_or_submitted:
            raise ValueError("an external outcome requires prior publication or submission")
        return self


class SkillRunReceipt(SkillContractModel):
    receipt_id: str = Field(pattern=r"^skill-run-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
    task_id: str
    normalized_task_envelope: SkillTaskEnvelope
    selected_skills: list[SelectedSkill] = Field(min_length=1)
    routing_reason: list[str] = Field(min_length=1)
    phase_routes: list[PhaseModelRoute] = Field(min_length=5, max_length=5)
    tools_and_commands: list[str] = Field(default_factory=list)
    repositories: list[RepositoryRunState] = Field(default_factory=list)
    evidence_bundle_ids: list[str] = Field(default_factory=list)
    input_hashes: dict[str, str]
    output_artifacts: list[ArtifactHash] = Field(default_factory=list)
    deterministic_checks: list[DeterministicCheck] = Field(default_factory=list)
    human_gates: list[str] = Field(default_factory=list)
    approvals: list[str] = Field(default_factory=list)
    failures: list[SkillFailure] = Field(default_factory=list)
    retries: int = Field(default=0, ge=0, le=2)
    final_status: SkillRunStatus
    started_at: datetime = Field(default_factory=_utc_now)
    completed_at: datetime = Field(default_factory=_utc_now)
    external_outcome_ids: list[str] = Field(default_factory=list)
    proposed_skill_update: SkillChangeDecision = SkillChangeDecision.NO_SKILL_CHANGE
    outcome_state: RunOutcomeState

    @model_validator(mode="after")
    def validate_receipt(self) -> SkillRunReceipt:
        if self.task_id != self.normalized_task_envelope.task_id:
            raise ValueError("receipt task_id must match its normalized Task Envelope")
        if (
            self.input_hashes.get("operator_request")
            != self.normalized_task_envelope.request_sha256
        ):
            raise ValueError("operator request hash must match the normalized Task Envelope")
        if self.final_status is SkillRunStatus.AWAITING_HUMAN_GATE and not self.human_gates:
            raise ValueError("awaiting-human-gate receipts must name at least one gate")
        if (
            self.final_status is SkillRunStatus.COMPLETED
            and not self.outcome_state.workflow_completed
        ):
            raise ValueError("completed receipts require workflow_completed=true")
        if self.final_status is SkillRunStatus.ROUTED and self.outcome_state != RunOutcomeState():
            raise ValueError("routed receipts cannot claim workflow or outcome progress")
        if (
            self.final_status is SkillRunStatus.AWAITING_HUMAN_GATE
            and self.outcome_state.workflow_completed
        ):
            raise ValueError("awaiting-human-gate receipts cannot claim workflow completion")
        if self.final_status is SkillRunStatus.AWAITING_HUMAN_GATE and (
            self.outcome_state.externally_published_or_submitted
            or self.outcome_state.external_outcome_observed
        ):
            raise ValueError("awaiting-human-gate receipts cannot claim external outcomes")
        if self.final_status is SkillRunStatus.FAILED and self.outcome_state.workflow_completed:
            raise ValueError("failed receipts cannot claim workflow completion")
        if self.external_outcome_ids and not (
            self.outcome_state.externally_published_or_submitted
            or self.outcome_state.external_outcome_observed
        ):
            raise ValueError(
                "external outcome IDs require an external publication or outcome state"
            )
        if self.outcome_state.human_approved and not self.approvals:
            raise ValueError("human-approved outcomes require an approval record")
        if self.outcome_state.externally_published_or_submitted and not self.external_outcome_ids:
            raise ValueError("confirmed external publication requires an external outcome ID")
        if not self.input_hashes or any(
            not key.strip() or not re.fullmatch(r"[0-9a-f]{64}", digest)
            for key, digest in self.input_hashes.items()
        ):
            raise ValueError("input hashes must contain named SHA-256 digests")
        if self.completed_at < self.started_at:
            raise ValueError("receipt completion time cannot precede start time")
        return self
