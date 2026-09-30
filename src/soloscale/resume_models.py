"""Typed, local-only contracts for the Resume Intelligence Workspace."""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from typing import Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from soloscale.models import ContractModel, utc_now

MAX_RESUME_PROFILE_ENTRIES = 120
MAX_RESUME_EXPERT_REVIEW_PATCHES = MAX_RESUME_PROFILE_ENTRIES
MAX_RESUME_ATOMIC_FACTS = MAX_RESUME_PROFILE_ENTRIES * 12
PROFILE_ENTRY_ID_PATTERN = r"^PROFILE-\d{2,3}$"
RESUME_FACT_ID_PATTERN = (
    r"^FACT-(?:PROFILE-\d{2,3}|EVIDENCE-[A-Z0-9-]+)-\d{2}$"
)


class ResumeMode(StrEnum):
    LOCAL_ONLY = "local-only"
    HYBRID = "hybrid"


class InterviewDefenseStatus(StrEnum):
    NEEDS_MAPPING = "NEEDS_MAPPING"
    MAPPED = "MAPPED"


class InterviewDefenseMapping(ContractModel):
    case_id: str
    learning_run_id: str
    mapping_basis: Literal["OPERATOR_CONFIRMED"] = "OPERATOR_CONFIRMED"
    repository: str
    branch: str
    commit: str
    anchor_pack: dict[str, object]


class InterviewDefenseRecord(ContractModel):
    bullet_id: str
    bullet_text: str
    bullet_sha256: str
    status: InterviewDefenseStatus = InterviewDefenseStatus.NEEDS_MAPPING
    mapping: InterviewDefenseMapping | None = None

    @model_validator(mode="after")
    def validate_mapping(self) -> InterviewDefenseRecord:
        if (self.status == InterviewDefenseStatus.MAPPED) is not (
            self.mapping is not None
        ):
            raise ValueError("interview defense mapping status must match its mapping")
        return self


class GraphNodeKind(StrEnum):
    JOB = "JOB"
    REQUIREMENT = "REQUIREMENT"
    SKILL = "SKILL"
    EVIDENCE = "EVIDENCE"
    PROJECT = "PROJECT"
    CODE = "CODE"
    VERIFICATION = "VERIFICATION"
    GAP = "GAP"
    LEARNING_TASK = "LEARNING_TASK"


class CandidateProfile(ContractModel):
    full_name: str | None = None
    headline: str | None = None
    summary: str | None = None
    skills: list[str] = Field(default_factory=list)
    experience_bullets: list[str] = Field(default_factory=list)
    project_bullets: list[str] = Field(default_factory=list)
    education: list[str] = Field(default_factory=list)

    @field_validator("skills", "experience_bullets", "project_bullets", "education")
    @classmethod
    def nonblank_items(cls, values: list[str]) -> list[str]:
        return [value.strip() for value in values if value.strip()]


class ResumeProfileLimitError(ValueError):
    """The uploaded profile is too large for one bounded generation request."""


def validate_resume_profile_entry_count(profile: CandidateProfile) -> None:
    entry_count = len(profile.experience_bullets) + len(profile.project_bullets)
    if entry_count > MAX_RESUME_PROFILE_ENTRIES:
        raise ResumeProfileLimitError(
            "Resume generation supports at most "
            f"{MAX_RESUME_PROFILE_ENTRIES} experience and project bullets; "
            f"the uploaded resume contains {entry_count}. Reduce the bullet count "
            "and try again."
        )


_ATOMIC_FACT_SPLIT_RE = re.compile(r"(?:[.;；]\s+|\s+[—–]\s+)")


class ResumeAtomicFactQuarantineReason(StrEnum):
    LOW_INFORMATION = "LOW_INFORMATION"
    STRUCTURAL_FRAGMENT = "STRUCTURAL_FRAGMENT"
    INVALID_TEXT = "INVALID_TEXT"
    INVALID_PROVENANCE = "INVALID_PROVENANCE"
    OTHER = "OTHER"


class ResumeAtomicFactAdmissionTrace(ContractModel):
    """Body-free counts for request-local candidate fact admission."""

    candidate_facts_total: int = Field(ge=0)
    candidate_facts_admitted: int = Field(ge=0)
    candidate_facts_quarantined: int = Field(ge=0)
    quarantine_reason_counts: dict[ResumeAtomicFactQuarantineReason, int] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def validate_counts(self) -> ResumeAtomicFactAdmissionTrace:
        if any(count < 0 for count in self.quarantine_reason_counts.values()):
            raise ValueError("atomic fact quarantine counts must be non-negative")
        if self.candidate_facts_total != (
            self.candidate_facts_admitted + self.candidate_facts_quarantined
        ):
            raise ValueError("atomic fact admission counts must balance")
        if self.candidate_facts_quarantined != sum(
            self.quarantine_reason_counts.values()
        ):
            raise ValueError("atomic fact quarantine reasons must match the total")
        return self


class ResumeAtomicFactAdmissionError(ValueError):
    """No truth-safe candidate facts survived individual admission."""

    def __init__(self, trace: ResumeAtomicFactAdmissionTrace) -> None:
        super().__init__("Candidate Evidence Pack contains no usable approved atomic facts")
        self.trace = trace


class ResumeAtomicFact(ContractModel):
    """One immutable fact identity derived from an approved profile entry."""

    fact_id: str = Field(pattern=RESUME_FACT_ID_PATTERN)
    profile_entry_id: str = Field(pattern=PROFILE_ENTRY_ID_PATTERN)
    evidence_id: str = Field(
        pattern=r"^(?:PROFILE-\d{2,3}|EVIDENCE-[A-Z0-9-]+)$"
    )
    source_kind: Literal["PROFILE_ENTRY", "CANDIDATE_EVIDENCE"]
    project: str | None = Field(default=None, max_length=120)
    capability_tags: list[str] = Field(default_factory=list, max_length=12)
    metric: str | None = Field(default=None, max_length=300)
    allowed_numbers: list[str] = Field(default_factory=list, max_length=16)
    source_refs: list[str] = Field(default_factory=list, max_length=12)
    text: str = Field(min_length=3, max_length=1_500)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    fact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_hashes(self) -> ResumeAtomicFact:
        if self.fact_sha256 != hashlib.sha256(
            f"{self.fact_id}\0{self.profile_entry_id}\0{self.text}".encode()
        ).hexdigest():
            raise ValueError("fact_sha256 must bind the fact ID, source, and text")
        return self

    @field_validator("allowed_numbers", "source_refs")
    @classmethod
    def validate_unique_metadata(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("atomic fact metadata identities must be unique")
        return values


def _candidate_fact_prevalidation_reason(
    text: str,
) -> ResumeAtomicFactQuarantineReason | None:
    if "\ufffd" in text or any(
        ord(character) < 0x20 and character not in {"\t", "\n", "\r"}
        for character in text
    ):
        return ResumeAtomicFactQuarantineReason.INVALID_TEXT
    if not any(character.isalnum() for character in text):
        return ResumeAtomicFactQuarantineReason.STRUCTURAL_FRAGMENT
    if len(text) < 3:
        return ResumeAtomicFactQuarantineReason.LOW_INFORMATION
    return None


def _candidate_fact_validation_reason(
    error: ValidationError,
) -> ResumeAtomicFactQuarantineReason:
    provenance_fields = {
        "fact_id",
        "profile_entry_id",
        "evidence_id",
        "source_sha256",
        "fact_sha256",
    }
    if any(
        validation_error.get("loc")
        and validation_error["loc"][0] in provenance_fields
        for validation_error in error.errors()
    ):
        return ResumeAtomicFactQuarantineReason.INVALID_PROVENANCE
    return ResumeAtomicFactQuarantineReason.OTHER


def admit_resume_atomic_facts(
    profile: CandidateProfile,
) -> tuple[list[ResumeAtomicFact], ResumeAtomicFactAdmissionTrace]:
    """Admit approved facts independently and quarantine only invalid fragments."""

    validate_resume_profile_entry_count(profile)
    entries = profile.experience_bullets + profile.project_bullets
    facts: list[ResumeAtomicFact] = []
    total = 0
    quarantine_reason_counts: dict[ResumeAtomicFactQuarantineReason, int] = {}
    for entry_index, source_text in enumerate(entries, start=1):
        profile_entry_id = f"PROFILE-{entry_index:02d}"
        source_sha256 = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        fragments = [
            fragment.strip(" \t\n,.;；")
            for fragment in _ATOMIC_FACT_SPLIT_RE.split(source_text)
            if fragment.strip(" \t\n,.;；")
        ] or [source_text]
        for fact_index, fragment in enumerate(fragments[:12], start=1):
            total += 1
            prevalidation_reason = _candidate_fact_prevalidation_reason(fragment)
            if prevalidation_reason is not None:
                quarantine_reason_counts[prevalidation_reason] = (
                    quarantine_reason_counts.get(prevalidation_reason, 0) + 1
                )
                continue
            fact_id = f"FACT-{profile_entry_id}-{fact_index:02d}"
            try:
                fact = ResumeAtomicFact(
                    fact_id=fact_id,
                    profile_entry_id=profile_entry_id,
                    evidence_id=profile_entry_id,
                    source_kind="PROFILE_ENTRY",
                    text=fragment,
                    source_sha256=source_sha256,
                    fact_sha256=hashlib.sha256(
                        f"{fact_id}\0{profile_entry_id}\0{fragment}".encode()
                    ).hexdigest(),
                )
            except ValidationError as exc:
                reason = _candidate_fact_validation_reason(exc)
                quarantine_reason_counts[reason] = (
                    quarantine_reason_counts.get(reason, 0) + 1
                )
                continue
            facts.append(fact)
    trace = ResumeAtomicFactAdmissionTrace(
        candidate_facts_total=total,
        candidate_facts_admitted=len(facts),
        candidate_facts_quarantined=total - len(facts),
        quarantine_reason_counts=quarantine_reason_counts,
    )
    if not facts:
        raise ResumeAtomicFactAdmissionError(trace)
    return facts, trace


def build_resume_atomic_facts(profile: CandidateProfile) -> list[ResumeAtomicFact]:
    """Derive deterministic request-local fact IDs from approved resume bullets."""

    facts, _trace = admit_resume_atomic_facts(profile)
    return facts


class GroundedResumeBulletRewrite(ContractModel):
    """One model-authored bullet anchored to explicit approved profile entries."""

    profile_entry_id: str = Field(pattern=PROFILE_ENTRY_ID_PATTERN)
    kind: Literal["REWRITE", "SYNTHESIS"] = "REWRITE"
    text: str = Field(min_length=1, max_length=600)
    source_profile_entry_ids: list[str] = Field(default_factory=list, max_length=8)
    source_fact_ids: list[str] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_sources(self) -> GroundedResumeBulletRewrite:
        if not self.source_profile_entry_ids:
            self.source_profile_entry_ids = [self.profile_entry_id]
        if len(self.source_profile_entry_ids) != len(
            set(self.source_profile_entry_ids)
        ):
            raise ValueError("rewrite source profile identities must be unique")
        if len(self.source_fact_ids) != len(set(self.source_fact_ids)):
            raise ValueError("rewrite atomic fact identities must be unique")
        if self.kind == "REWRITE" and self.source_profile_entry_ids != [
            self.profile_entry_id
        ]:
            raise ValueError("REWRITE must use only its target profile entry")
        if self.kind == "SYNTHESIS" and (
            len(self.source_fact_ids) < 2
            or self.profile_entry_id not in self.source_profile_entry_ids
        ):
            raise ValueError("SYNTHESIS requires its target and multiple atomic facts")
        return self


class GroundedResumeSummaryRewrite(ContractModel):
    """One professional summary synthesized from approved profile entries."""

    text: str = Field(min_length=1, max_length=800)
    source_profile_entry_ids: list[str] = Field(min_length=1, max_length=8)
    source_fact_ids: list[str] = Field(min_length=2, max_length=32)

    @model_validator(mode="after")
    def validate_sources(self) -> GroundedResumeSummaryRewrite:
        if len(self.source_profile_entry_ids) != len(
            set(self.source_profile_entry_ids)
        ):
            raise ValueError("summary source profile identities must be unique")
        if len(self.source_fact_ids) != len(set(self.source_fact_ids)):
            raise ValueError("summary atomic fact identities must be unique")
        return self


class RoleStrategy(ContractModel):
    """JD-conditioned plan for a truth-bounded whole-resume optimization."""

    role_summary: str = Field(min_length=1, max_length=500)
    top_hiring_signals: list[str] = Field(min_length=1, max_length=8)
    evidence_priority: list[str] = Field(
        min_length=1, max_length=MAX_RESUME_PROFILE_ENTRIES
    )
    skill_priority: list[str] = Field(default_factory=list, max_length=40)
    bullet_rewrites: list[GroundedResumeBulletRewrite] = Field(
        min_length=1, max_length=MAX_RESUME_PROFILE_ENTRIES
    )
    summary_rewrite: GroundedResumeSummaryRewrite | None = None
    unsupported_requirements: list[str] = Field(default_factory=list, max_length=16)
    rewrite_guidance: str = Field(min_length=1, max_length=800)


class CandidateEvidenceSource(ContractModel):
    """One compact, verified source admitted to Resume composition."""

    evidence_id: str = Field(pattern=r"^EVIDENCE-[A-Z0-9-]+$")
    project: str = Field(min_length=1, max_length=120)
    source_kind: Literal["TRACKED_CANON", "VERIFIED_RUN", "REPOSITORY"]
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_refs: list[str] = Field(default_factory=list, max_length=12)


class ResumeEvidenceRetrievalHit(ContractModel):
    """Body-free trace for one local-knowledge retrieval result."""

    retrieval_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_kind: Literal["codex_session", "chatgpt_export", "buildlog_run"]
    chunk_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    document_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    score: float = Field(ge=0)
    disposition: Literal["DISCOVERY_ONLY"] = "DISCOVERY_ONLY"
    requirement_ids: list[str] = Field(default_factory=list, max_length=8)
    matched_fact_ids: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("requirement_ids", "matched_fact_ids")
    @classmethod
    def validate_unique_ids(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("retrieval identities must be unique")
        return values


class ResumeRequirementCoverage(ContractModel):
    """Body-safe requirement coverage derived from exact JD spans."""

    requirement_id: str = Field(pattern=r"^REQ-\d{2}$")
    requirement_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["STRONG", "MEDIUM", "GAP"]
    matched_fact_ids: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("matched_fact_ids")
    @classmethod
    def validate_unique_fact_ids(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("requirement fact identities must be unique")
        return values


class ResumeEvidenceSourceSummary(ContractModel):
    """Count-only source participation summary for the Resume UI."""

    source_type: Literal[
        "EXISTING_RESUME",
        "LOCAL_GIT",
        "GITHUB",
        "BUILDLOG",
        "CODEX",
        "CHATGPT",
        "CONTENT_CANON",
        "LEARNING",
        "RESUME_HISTORY",
    ]
    state: Literal["MATCHED", "NO_MATCH", "UNAVAILABLE"]
    retrieved_count: int = Field(ge=0)
    admitted_count: int = Field(ge=0)
    context_only_count: int = Field(ge=0)
    sent_count: int = Field(ge=0)


class CompositionRequirementPlan(ContractModel):
    """Deterministic fact allocation for one exact JD requirement."""

    requirement_id: str = Field(pattern=r"^REQ-\d{2}$")
    requirement_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    primary_fact_ids: list[str] = Field(default_factory=list, max_length=4)
    secondary_fact_ids: list[str] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def validate_fact_allocation(self) -> CompositionRequirementPlan:
        combined = [*self.primary_fact_ids, *self.secondary_fact_ids]
        if len(combined) != len(set(combined)):
            raise ValueError("composition facts must be unique per requirement")
        return self


class CompositionEvidencePlan(ContractModel):
    """Read-only evidence plan that keeps model composition JD-relevant."""

    job_description_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    requirements: list[CompositionRequirementPlan] = Field(
        default_factory=list, max_length=8
    )
    prioritized_fact_ids: list[str] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def validate_plan(self) -> CompositionEvidencePlan:
        requirement_ids = [item.requirement_id for item in self.requirements]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("composition requirement identities must be unique")
        allocated = [
            fact_id
            for item in self.requirements
            for fact_id in (*item.primary_fact_ids, *item.secondary_fact_ids)
        ]
        if len(self.prioritized_fact_ids) != len(set(self.prioritized_fact_ids)):
            raise ValueError("prioritized composition facts must be unique")
        if not set(allocated) <= set(self.prioritized_fact_ids):
            raise ValueError("allocated facts must be present in composition priority")
        return self


class ResumeEvidenceAdoptionTrace(ContractModel):
    """Body-free lifecycle for one fact considered by Resume composition."""

    fact_id: str = Field(pattern=RESUME_FACT_ID_PATTERN)
    retrieved: bool = True
    admitted: bool
    sent_to_model: bool
    proposed: bool = False
    accepted: bool = False
    rendered: bool = False
    rejection_rule_codes: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> ResumeEvidenceAdoptionTrace:
        if self.sent_to_model and not self.admitted:
            raise ValueError("sent facts must be admitted")
        if self.proposed and not self.sent_to_model:
            raise ValueError("proposed facts must have been sent")
        if self.accepted and not self.proposed:
            raise ValueError("accepted facts must have been proposed")
        if self.rendered and not self.accepted:
            raise ValueError("rendered facts must have been accepted")
        if len(self.rejection_rule_codes) != len(set(self.rejection_rule_codes)):
            raise ValueError("adoption rejection rules must be unique")
        return self


class ResumeEvidenceRetrievalTrace(ContractModel):
    """Explain how local context selected verified facts without authorizing claims."""

    job_description_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_counts: dict[str, int] = Field(default_factory=dict)
    hits: list[ResumeEvidenceRetrievalHit] = Field(default_factory=list, max_length=32)
    requirements: list[ResumeRequirementCoverage] = Field(
        default_factory=list, max_length=8
    )
    sources: list[ResumeEvidenceSourceSummary] = Field(
        default_factory=list, max_length=9
    )
    retrieved_count: int = Field(ge=0)
    admitted_count: int = Field(ge=0)
    sent_count: int = Field(ge=0)
    admitted_fact_ids: list[str] = Field(
        default_factory=list, max_length=MAX_RESUME_ATOMIC_FACTS
    )
    sent_fact_ids: list[str] = Field(
        default_factory=list, max_length=MAX_RESUME_ATOMIC_FACTS
    )
    adoption: list[ResumeEvidenceAdoptionTrace] = Field(
        default_factory=list, max_length=MAX_RESUME_ATOMIC_FACTS
    )

    @model_validator(mode="after")
    def validate_trace(self) -> ResumeEvidenceRetrievalTrace:
        if any(count < 0 for count in self.source_counts.values()):
            raise ValueError("retrieval source counts must be non-negative")
        retrieval_ids = [hit.retrieval_id for hit in self.hits]
        if len(retrieval_ids) != len(set(retrieval_ids)):
            raise ValueError("retrieval identities must be unique")
        if len(self.admitted_fact_ids) != len(set(self.admitted_fact_ids)):
            raise ValueError("admitted fact identities must be unique")
        if len(self.sent_fact_ids) != len(set(self.sent_fact_ids)):
            raise ValueError("sent fact identities must be unique")
        if not set(self.sent_fact_ids) <= set(self.admitted_fact_ids):
            raise ValueError("sent facts must be admitted first")
        if self.admitted_count != len(self.admitted_fact_ids):
            raise ValueError("admitted count must match admitted fact identities")
        if self.sent_count != len(self.sent_fact_ids):
            raise ValueError("sent count must match sent fact identities")
        requirement_ids = [item.requirement_id for item in self.requirements]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("requirement identities must be unique")
        source_types = [item.source_type for item in self.sources]
        if len(source_types) != len(set(source_types)):
            raise ValueError("evidence source summaries must be unique")
        adoption_ids = [item.fact_id for item in self.adoption]
        if len(adoption_ids) != len(set(adoption_ids)):
            raise ValueError("evidence adoption facts must be unique")
        return self


class CandidateEvidencePack(ContractModel):
    """High-density truth context used in addition to the uploaded Resume."""

    schema_version: Literal["1.0"] = "1.0"  # type: ignore[assignment]
    sources: list[CandidateEvidenceSource] = Field(default_factory=list, max_length=32)
    atomic_facts: list[ResumeAtomicFact] = Field(
        min_length=1, max_length=MAX_RESUME_ATOMIC_FACTS
    )
    fact_admission: ResumeAtomicFactAdmissionTrace | None = None
    composition_plan: CompositionEvidencePlan | None = None
    pack_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_pack(self) -> CandidateEvidencePack:
        source_ids = [source.evidence_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("candidate evidence source identities must be unique")
        fact_ids = [fact.fact_id for fact in self.atomic_facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("candidate evidence fact identities must be unique")
        allowed_evidence_ids = set(source_ids) | {
            fact.evidence_id
            for fact in self.atomic_facts
            if fact.source_kind == "PROFILE_ENTRY"
        }
        if any(fact.evidence_id not in allowed_evidence_ids for fact in self.atomic_facts):
            raise ValueError("candidate evidence fact references an unknown source")
        if self.fact_admission is not None:
            profile_fact_count = sum(
                fact.source_kind == "PROFILE_ENTRY" for fact in self.atomic_facts
            )
            if self.fact_admission.candidate_facts_admitted != profile_fact_count:
                raise ValueError("atomic fact admission trace must match profile facts")
        if self.composition_plan is not None and not set(
            self.composition_plan.prioritized_fact_ids
        ) <= set(fact_ids):
            raise ValueError("composition plan references an unknown candidate fact")
        return self


class JDPositioningBrief(ContractModel):
    """Deterministic JD framing supplied to the model as read-only context."""

    job_description_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    role_title: str = Field(min_length=1, max_length=200)
    top_hiring_signals: list[str] = Field(min_length=1, max_length=8)
    technical_themes: list[str] = Field(default_factory=list, max_length=12)
    priority_fact_ids: list[str] = Field(default_factory=list, max_length=48)
    first_resume_focus: list[str] = Field(default_factory=list, max_length=8)


class ResumeClaimVerificationStatus(StrEnum):
    """Deterministic export status for one final resume claim."""

    VERIFIED = "VERIFIED"
    SUPPORTED = "SUPPORTED"
    UNVERIFIED = "UNVERIFIED"
    CONTRADICTED = "CONTRADICTED"


class ResumeHiringSignalReceipt(ContractModel):
    """Body-free identity for one exact JD hiring signal."""

    signal_id: str = Field(pattern=r"^SIGNAL-\d{2}$")
    signal_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    exact_jd_quote: Literal[True] = True


class ResumeClaimProvenance(ContractModel):
    """One rendered resume slot and all approved sources that support it."""

    claim_id: str = Field(pattern=r"^CLAIM-\d{2,3}$")
    render_location: Literal["SUMMARY", "BULLET"]
    final_text: str = Field(min_length=1, max_length=800)
    final_text_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_entry_id: str = Field(pattern=r"^(?:PROFILE-\d{2,3}|SUMMARY)$")
    approved_source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_ids: list[str] = Field(min_length=1, max_length=8)
    approved_evidence_sha256s: list[str] = Field(min_length=1, max_length=8)
    fact_ids: list[str] = Field(default_factory=list, max_length=32)
    source_fact_sha256s: list[str] = Field(default_factory=list, max_length=32)
    hiring_signal_ids: list[str] = Field(default_factory=list, max_length=8)
    status: ResumeClaimVerificationStatus
    verification_basis: Literal[
        "EXACT_OPERATOR_APPROVED_PROFILE_ENTRY",
        "DETERMINISTIC_EVIDENCE_PRESERVING_REWRITE",
        "DETERMINISTIC_MULTI_SOURCE_SYNTHESIS",
        "EDITORIAL_PARAPHRASE_PENDING_HUMAN_REVIEW",
    ]

    @model_validator(mode="after")
    def validate_exported_claim(self) -> ResumeClaimProvenance:
        if self.final_text_sha256 != hashlib.sha256(
            self.final_text.encode("utf-8")
        ).hexdigest():
            raise ValueError("final_text_sha256 must match the exported claim")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("resume claim evidence identities must be unique")
        if any(
            value != "SUMMARY"
            and not value.startswith("PROFILE-")
            and not value.startswith("EVIDENCE-")
            for value in self.evidence_ids
        ):
            raise ValueError("resume claim evidence must use approved stable sources")
        if len(self.approved_evidence_sha256s) != len(self.evidence_ids):
            raise ValueError("every evidence ID requires an ordered approved-source hash")
        if len(self.source_fact_sha256s) != len(set(self.source_fact_sha256s)):
            raise ValueError("source fact hashes must be unique")
        if len(self.fact_ids) != len(set(self.fact_ids)):
            raise ValueError("source fact identities must be unique")
        if any(
            re.fullmatch(
                r"FACT-(?:PROFILE-\d{2,3}|EVIDENCE-[A-Z0-9-]+)-\d{2}",
                fact_id,
            )
            is None
            for fact_id in self.fact_ids
        ):
            raise ValueError("source fact identities must use stable FACT IDs")
        if len(self.fact_ids) != len(self.source_fact_sha256s):
            raise ValueError("every source fact ID requires an ordered fact hash")
        if len(self.hiring_signal_ids) != len(set(self.hiring_signal_ids)):
            raise ValueError("hiring signal identities must be unique")
        if self.status == ResumeClaimVerificationStatus.CONTRADICTED:
            raise ValueError("contradicted claims cannot be exported")
        if self.render_location == "SUMMARY" and self.profile_entry_id != "SUMMARY":
            raise ValueError("summary provenance must target the Summary slot")
        if self.render_location == "BULLET" and not self.profile_entry_id.startswith(
            "PROFILE-"
        ):
            raise ValueError("bullet provenance must target a profile entry")
        if self.status == ResumeClaimVerificationStatus.VERIFIED:
            if self.verification_basis != "EXACT_OPERATOR_APPROVED_PROFILE_ENTRY":
                raise ValueError("VERIFIED claims require the exact-source basis")
            if self.final_text_sha256 != self.approved_source_sha256:
                raise ValueError("VERIFIED claims must preserve approved text exactly")
            if self.evidence_ids != [self.profile_entry_id]:
                raise ValueError("VERIFIED claims must retain their exact profile source")
            if self.approved_evidence_sha256s != [self.approved_source_sha256]:
                raise ValueError("VERIFIED claims must retain their approved source hash")
            if self.fact_ids or self.source_fact_sha256s:
                raise ValueError("VERIFIED claims do not need rewrite source facts")
        else:
            if self.verification_basis == "EXACT_OPERATOR_APPROVED_PROFILE_ENTRY":
                raise ValueError("non-exact claims require a rewrite, synthesis, or review basis")
            if len(self.source_fact_sha256s) < len(self.evidence_ids):
                raise ValueError("every supported evidence source needs a hashed fact")
            fact_source_ids = {
                fact_id.removeprefix("FACT-").rsplit("-", maxsplit=1)[0]
                for fact_id in self.fact_ids
            }
            if fact_source_ids != set(self.evidence_ids):
                raise ValueError("supported facts must cover every evidence source")
            if (
                self.render_location == "BULLET"
                and self.profile_entry_id in self.evidence_ids
            ):
                target_index = self.evidence_ids.index(self.profile_entry_id)
                if (
                    self.approved_evidence_sha256s[target_index]
                    != self.approved_source_sha256
                ):
                    raise ValueError("bullet provenance must retain its target source hash")
        if (
            self.verification_basis
            == "DETERMINISTIC_EVIDENCE_PRESERVING_REWRITE"
            and self.evidence_ids != [self.profile_entry_id]
        ):
            raise ValueError("single-source rewrites must retain their profile source")
        if self.verification_basis == "DETERMINISTIC_MULTI_SOURCE_SYNTHESIS":
            if len(self.fact_ids) < 2:
                raise ValueError("synthesis requires multiple approved atomic facts")
        if self.status == ResumeClaimVerificationStatus.UNVERIFIED:
            if self.verification_basis != "EDITORIAL_PARAPHRASE_PENDING_HUMAN_REVIEW":
                raise ValueError("UNVERIFIED claims require the editorial review basis")
        elif self.verification_basis == "EDITORIAL_PARAPHRASE_PENDING_HUMAN_REVIEW":
            raise ValueError("editorial review basis requires an UNVERIFIED claim")
        return self


class ResumeProvenanceReceipt(ContractModel):
    """Private claim-level receipt for one generated resume artifact."""

    run_id: str
    resume_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    job_description_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    generation_mode: str
    source_inputs_retained: Literal[False] = False
    contains_source_bodies: Literal[False] = False
    hiring_signals: list[ResumeHiringSignalReceipt] = Field(
        default_factory=list, max_length=8
    )
    claims: list[ResumeClaimProvenance] = Field(
        min_length=1, max_length=MAX_RESUME_PROFILE_ENTRIES + 1
    )
    unsupported_requirement_sha256s: list[str] = Field(default_factory=list, max_length=16)
    all_exported_claims_supported: bool
    final_human_review_required: Literal[True] = True

    @model_validator(mode="after")
    def validate_receipt_links(self) -> ResumeProvenanceReceipt:
        signal_ids = {item.signal_id for item in self.hiring_signals}
        claim_ids = [item.claim_id for item in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("resume claim identities must be unique")
        render_targets = [
            (item.render_location, item.profile_entry_id) for item in self.claims
        ]
        if len(render_targets) != len(set(render_targets)):
            raise ValueError("resume render locations must have one provenance claim")
        if sum(item.render_location == "SUMMARY" for item in self.claims) > 1:
            raise ValueError("resume provenance may contain only one Summary claim")
        if any(not set(item.hiring_signal_ids) <= signal_ids for item in self.claims):
            raise ValueError("resume claim references an unknown hiring signal")
        if len(self.unsupported_requirement_sha256s) != len(
            set(self.unsupported_requirement_sha256s)
        ):
            raise ValueError("unsupported requirement hashes must be unique")
        derived_all_supported = all(
            item.status
            in {
                ResumeClaimVerificationStatus.VERIFIED,
                ResumeClaimVerificationStatus.SUPPORTED,
            }
            for item in self.claims
        )
        if self.all_exported_claims_supported != derived_all_supported:
            raise ValueError("all_exported_claims_supported must match claim statuses")
        return self


class ResumeExpertReviewPatch(ContractModel):
    """One evidence-preserving wording patch from the optional expert reviewer."""

    profile_entry_id: str = Field(pattern=PROFILE_ENTRY_ID_PATTERN)
    before_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    after: str = Field(min_length=1, max_length=600)
    new_factual_claims: list[str] = Field(default_factory=list, max_length=8)
    rationale: str = Field(min_length=1, max_length=500)


class ResumeExpertReviewResult(ContractModel):
    """Patch-only expert review; deterministic code owns acceptance."""

    summary: str = Field(min_length=1, max_length=800)
    patches: list[ResumeExpertReviewPatch] = Field(
        default_factory=list, max_length=MAX_RESUME_EXPERT_REVIEW_PATCHES
    )
    omitted_high_value_profile_entry_ids: list[str] = Field(
        default_factory=list, max_length=MAX_RESUME_PROFILE_ENTRIES
    )

    @model_validator(mode="after")
    def validate_patch_identity(self) -> ResumeExpertReviewResult:
        patch_ids = [patch.profile_entry_id for patch in self.patches]
        if len(patch_ids) != len(set(patch_ids)):
            raise ValueError("expert review patches must have unique profile identities")
        if len(self.omitted_high_value_profile_entry_ids) != len(
            set(self.omitted_high_value_profile_entry_ids)
        ):
            raise ValueError("omitted profile identities must be unique")
        return self


class JobResearchSource(ContractModel):
    title: str
    url: str | None = None
    summary: str | None = None


class JobRequirement(ContractModel):
    id: str
    text: str
    skills: list[str] = Field(default_factory=list)
    priority: Literal["critical", "preferred"] = "preferred"


class EvidenceLocator(ContractModel):
    document_id: str
    source_kind: str
    external_id: str
    source_locator: str
    title: str | None = None
    role: str
    timestamp: str | None = None
    chunk_sha256: str
    document_sha256: str
    searchable_metadata_sha256: str | None = None
    channels: list[str] = Field(min_length=1)
    repository: str | None = None
    branch: str | None = None
    commit: str | None = None
    file_path: str | None = None
    symbol: str | None = None
    line_range: str | None = None
    related_test: str | None = None
    verification_receipt: str | None = None


class EvidenceMatch(ContractModel):
    id: str
    requirement_id: str
    evidence_id: str
    excerpt: str
    match_quality: Literal["lexical_candidate_strong", "lexical_candidate_partial"]
    locator: EvidenceLocator


class ResumeBullet(ContractModel):
    text: str
    requirement_ids: list[str] = Field(default_factory=list)
    profile_entry_ids: list[str] = Field(min_length=1)
    support: Literal["candidate_profile"] = "candidate_profile"

    @field_validator("profile_entry_ids")
    @classmethod
    def profile_entry_ids_are_explicit(cls, values: list[str]) -> list[str]:
        if any(not value.startswith("PROFILE-") for value in values):
            raise ValueError("resume bullets must reference Candidate Profile entries")
        return values


class ResumeDraft(ContractModel):
    summary: str | None = None
    skills: list[str] = Field(default_factory=list)
    bullets: list[ResumeBullet] = Field(default_factory=list)
    education: list[str] = Field(default_factory=list)


class SkillGap(ContractModel):
    requirement_id: str
    skill: str
    reason: str


class LearningTask(ContractModel):
    id: str
    requirement_id: str
    title: str
    acceptance_criterion: str


class EvidenceGraphNode(ContractModel):
    id: str
    kind: GraphNodeKind
    label: str
    detail: dict[str, str | list[str] | None] = Field(default_factory=dict)


class EvidenceGraphEdge(ContractModel):
    source: str
    target: str
    relation: str


class ResumeRun(ContractModel):
    run_id: str
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    mode: ResumeMode
    evidence_bundle_id: str | None = None
    status: Literal["CANDIDATE_REQUIRES_HUMAN_CONFIRMATION"] = (
        "CANDIDATE_REQUIRES_HUMAN_CONFIRMATION"
    )
    route: dict[str, str | int | bool] = Field(default_factory=dict)
    artifact_paths: list[str] = Field(default_factory=list)


class ResumeDeliveryReceipt(ContractModel):
    run_id: str
    state: Literal[
        "INTERNAL_READY",
        "APPLICATION_LIBRARY_PENDING",
        "APPLICATION_LIBRARY_SAVED",
        "APPLICATION_LIBRARY_PUBLISHED_DURABILITY_UNCERTAIN",
        "APPLICATION_LIBRARY_FAILED",
    ]
    application_library_path: str | None = None
    error_type: str | None = None
    retry_safe: bool = False


class EvidenceCandidateClass(StrEnum):
    """Retrieval-time classification. Final claim eligibility is decided downstream."""

    DIRECT = "direct"
    SEMANTIC = "semantic"
    ADJACENT_CAPABILITY = "adjacent_capability"
    POTENTIAL_DERIVATION = "potential_derivation"
    WEAK = "weak"
    IRRELEVANT = "irrelevant"


class ResumeRetrievalSourceKind(StrEnum):
    MAC_CATALOG = "mac_catalog"
    LOCAL_GIT = "local_git"
    KNOWLEDGE_STORE = "knowledge_store"
    EVIDENCE_HUB = "evidence_hub"
    RESUME_LIBRARY = "resume_library"
    BUILDLOG = "buildlog"
    CODEX = "codex"
    CHATGPT = "chatgpt"
    GITHUB = "github"


class ResumeRetrievalCandidate(ContractModel):
    """One high-recall candidate with intact provenance; no final claim decision."""

    candidate_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_kind: ResumeRetrievalSourceKind
    source_identity: str = Field(min_length=1, max_length=300)
    evidence_id: str | None = None
    evidence_type: str = Field(min_length=1, max_length=80)
    authority: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=4000)
    signals: list[str] = Field(default_factory=list, max_length=40)
    candidate_class: EvidenceCandidateClass = EvidenceCandidateClass.IRRELEVANT
    rationale: str = Field(min_length=1, max_length=400)
    provenance: dict[str, str] = Field(default_factory=dict)
    score: float = Field(ge=0)
    requirement_classes: dict[str, EvidenceCandidateClass] = Field(default_factory=dict)
    requirement_signals: dict[str, list[str]] = Field(default_factory=dict)


class ResumeRequirementExpansion(ContractModel):
    """Bounded normalized requirement with capability-aware search vocabulary."""

    requirement_id: str = Field(pattern=r"^REQ-\d{2}$")
    text: str = Field(min_length=1, max_length=2000)
    capability: str = Field(min_length=1, max_length=120)
    technology_terms: list[str] = Field(default_factory=list, max_length=24)
    synonyms: list[str] = Field(default_factory=list, max_length=80)
    expanded_terms: list[str] = Field(default_factory=list, max_length=104)
    likely_evidence_forms: list[str] = Field(default_factory=list, max_length=12)


class ResumeRetrievalCoverage(ContractModel):
    """One requirement's evidence coverage buckets; classifies, never destroys."""

    requirement_id: str = Field(pattern=r"^REQ-\d{2}$")
    normalized_capability: str = Field(min_length=1, max_length=120)
    direct_evidence: list[str] = Field(default_factory=list, max_length=600)
    semantic_evidence: list[str] = Field(default_factory=list, max_length=600)
    related_projects: list[str] = Field(default_factory=list, max_length=600)
    potential_derivations: list[str] = Field(default_factory=list, max_length=600)
    weak_evidence: list[str] = Field(default_factory=list, max_length=600)
    missing_proof: list[str] = Field(default_factory=list, max_length=12)
    source_diversity: list[ResumeRetrievalSourceKind] = Field(
        default_factory=list, max_length=10
    )


class ResumeRetrievalSourceStatus(ContractModel):
    source_kind: ResumeRetrievalSourceKind
    state: Literal["SEARCHED", "EMPTY", "UNAVAILABLE", "NOT_SELECTED"]
    detail: str | None = None
    candidate_count: int = Field(default=0, ge=0)


class ResumeEvidenceCoverageMap(ContractModel):
    """Structured evidence coverage over one job description."""

    job_description_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    requirements: list[ResumeRequirementExpansion] = Field(max_length=24)
    coverage: list[ResumeRetrievalCoverage] = Field(max_length=24)
    candidates: list[ResumeRetrievalCandidate] = Field(max_length=1200)
    sources: list[ResumeRetrievalSourceStatus] = Field(max_length=10)
    retrieved_count: int = Field(ge=0)
    kept_count: int = Field(ge=0)
    irrelevant_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_coverage_identity(self) -> ResumeEvidenceCoverageMap:
        requirement_ids = [item.requirement_id for item in self.requirements]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("requirement identities must be unique")
        coverage_ids = [item.requirement_id for item in self.coverage]
        if coverage_ids != requirement_ids:
            raise ValueError("coverage must match normalized requirements in order")
        candidate_ids = {item.candidate_id for item in self.candidates}
        referenced = {
            candidate_id
            for item in self.coverage
            for candidate_id in (
                *item.direct_evidence,
                *item.semantic_evidence,
                *item.related_projects,
                *item.potential_derivations,
                *item.weak_evidence,
            )
        }
        if not referenced <= candidate_ids:
            raise ValueError("coverage references an unknown candidate")
        if len(candidate_ids) != len(self.candidates):
            raise ValueError("candidate identities must be unique")
        return self


class ClaimClass(StrEnum):
    """Graded truth for one resume claim. Final export gate is downstream."""

    VERIFIED = "VERIFIED"
    SUPPORTED_DERIVATION = "SUPPORTED_DERIVATION"
    HIGH_VALUE_GAP = "HIGH_VALUE_GAP"
    UNSUPPORTED = "UNSUPPORTED"


class EvidenceAuthority(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW_CONTEXTUAL = "LOW_CONTEXTUAL"
    NON_SUPPORTING = "NON_SUPPORTING"


class EvidenceOwnership(StrEnum):
    PROVEN = "PROVEN"
    UNPROVEN = "UNPROVEN"


class ContaminationKind(StrEnum):
    NONE = "NONE"
    JD_QUERY_ECHO = "JD_QUERY_ECHO"
    COURSE_EXPOSURE = "COURSE_EXPOSURE"
    MODEL_SUGGESTION = "MODEL_SUGGESTION"


class ClaimStrength(StrEnum):
    STRONG = "STRONG"
    MODERATE = "MODERATE"
    BOUNDED = "BOUNDED"
    CONTRIBUTION_SAFE = "CONTRIBUTION_SAFE"
    EXPOSURE_SAFE = "EXPOSURE_SAFE"
    LEARNING_ONLY = "LEARNING_ONLY"


class GapAction(StrEnum):
    SEARCH_MORE = "SEARCH_MORE"
    USER_SUPPLEMENT = "USER_SUPPLEMENT"
    LEARNING_CASE = "LEARNING_CASE"
    IGNORE = "IGNORE"


class ClaimValidationCode(StrEnum):
    UNSUPPORTED_CLASS = "UNSUPPORTED_CLASS"
    NON_SUPPORTING_EVIDENCE = "NON_SUPPORTING_EVIDENCE"
    OWNERSHIP_UNPROVEN = "OWNERSHIP_UNPROVEN"
    TECHNOLOGY_INFLATION = "TECHNOLOGY_INFLATION"
    NEW_NUMBER = "NEW_NUMBER"
    ECHO_CONTAMINATION = "ECHO_CONTAMINATION"


class GradedEvidence(ContractModel):
    """Authority/ownership/contamination classification for one retrieved candidate."""

    evidence_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_kind: ResumeRetrievalSourceKind
    authority: EvidenceAuthority
    ownership: EvidenceOwnership
    contamination: ContaminationKind
    capability_terms: list[str] = Field(default_factory=list, max_length=40)
    is_implementation: bool = False


class ClaimComponentVerdict(ContractModel):
    """Graded verdict for one capability component of one requirement."""

    component: str = Field(min_length=1, max_length=120)
    claim_class: ClaimClass
    evidence_ids: list[str] = Field(default_factory=list, max_length=80)
    missing_proof: list[str] = Field(default_factory=list, max_length=12)
    rationale: str = Field(min_length=1, max_length=400)


class RequirementClaimMap(ContractModel):
    requirement_id: str = Field(pattern=r"^REQ-\d{2}$")
    requirement_text: str = Field(min_length=1, max_length=2000)
    capability: str = Field(min_length=1, max_length=120)
    components: list[ClaimComponentVerdict] = Field(min_length=1, max_length=12)


class ApplicationClaim(ContractModel):
    """One claim allowed into the submit-ready Application Resume."""

    claim_id: str = Field(pattern=r"^CLAIM-\d{2}$")
    requirement_id: str = Field(pattern=r"^REQ-\d{2}$")
    claim_class: ClaimClass
    strength: ClaimStrength
    proposed_text: str = Field(min_length=1, max_length=600)
    evidence_ids: list[str] = Field(min_length=1, max_length=24)
    authority: EvidenceAuthority
    ownership: EvidenceOwnership
    derivation_rationale: str = Field(min_length=1, max_length=400)
    truth_boundary: str = Field(min_length=1, max_length=400)
    excluded_implications: list[str] = Field(default_factory=list, max_length=24)
    technology_vocabulary: list[str] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def validate_application_class(self) -> ApplicationClaim:
        if self.claim_class not in {
            ClaimClass.VERIFIED,
            ClaimClass.SUPPORTED_DERIVATION,
        }:
            raise ValueError("Application Resume may contain only VERIFIED or SUPPORTED_DERIVATION")
        return self


class TargetGap(ContractModel):
    """One clearly labeled non-submit-safe item in the Target Resume."""

    gap_id: str = Field(pattern=r"^GAP-\d{2}$")
    requirement_id: str = Field(pattern=r"^REQ-\d{2}$")
    capability: str = Field(min_length=1, max_length=120)
    claim_class: ClaimClass
    suggested_wording: str = Field(min_length=1, max_length=600)
    why_it_matters: str = Field(min_length=1, max_length=400)
    evidence_found: list[str] = Field(default_factory=list, max_length=24)
    missing_proof: list[str] = Field(min_length=1, max_length=12)
    actions: list[GapAction] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def validate_target_class(self) -> TargetGap:
        if self.claim_class not in {
            ClaimClass.HIGH_VALUE_GAP,
            ClaimClass.UNSUPPORTED,
        }:
            raise ValueError("target gaps must be HIGH_VALUE_GAP or UNSUPPORTED")
        return self


class ClaimTruthResult(ContractModel):
    """Graded claim map plus Application/Target separation for one JD."""

    job_description_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    requirement_maps: list[RequirementClaimMap] = Field(max_length=24)
    evidence: list[GradedEvidence] = Field(default_factory=list, max_length=1200)
    application_claims: list[ApplicationClaim] = Field(max_length=80)
    target_gaps: list[TargetGap] = Field(max_length=80)
    verified_count: int = Field(ge=0)
    supported_derivation_count: int = Field(ge=0)
    high_value_gap_count: int = Field(ge=0)
    unsupported_count: int = Field(ge=0)
    contamination_counts: dict[ContaminationKind, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_claim_identity(self) -> ClaimTruthResult:
        requirement_ids = [item.requirement_id for item in self.requirement_maps]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("requirement claim maps must be unique")
        evidence_ids = {item.evidence_id for item in self.evidence}
        application_ids = [item.claim_id for item in self.application_claims]
        gap_ids = [item.gap_id for item in self.target_gaps]
        if len(application_ids) != len(set(application_ids)):
            raise ValueError("application claim ids must be unique")
        if len(gap_ids) != len(set(gap_ids)):
            raise ValueError("target gap ids must be unique")
        for claim in self.application_claims:
            if not set(claim.evidence_ids) <= evidence_ids:
                raise ValueError("application claim references unknown evidence")
        if any(count < 0 for count in self.contamination_counts.values()):
            raise ValueError("contamination counts must be non-negative")
        return self


class ContributionMode(StrEnum):
    """How engineering contribution was made; independent of claim truth."""

    USER_DIRECT = "user_direct"
    AI_ASSISTED_USER_DIRECTED = "ai_assisted_user_directed"
    AGENT_GENERATED_USER_REVIEWED = "agent_generated_user_reviewed"
    EXTERNAL_UNKNOWN = "external_unknown"


class ResumeRoleStrategy(ContractModel):
    """JD-conditioned positioning derived from the graded claim map."""

    role_title: str = Field(min_length=1, max_length=200)
    positioning: str = Field(min_length=1, max_length=500)
    headline: str = Field(min_length=1, max_length=240)
    prioritized_requirement_ids: list[str] = Field(min_length=1, max_length=24)
    emphasized_terms: list[str] = Field(min_length=1, max_length=40)
    unsupported_terms: list[str] = Field(default_factory=list, max_length=40)
    selected_projects: list[str] = Field(default_factory=list, max_length=12)
    claim_allocations: dict[str, list[str]] = Field(default_factory=dict)
    recruiter_scan_priorities: list[str] = Field(default_factory=list, max_length=12)


class ResumeGenerationContract(ContractModel):
    """Bounded generation context. The model may rephrase, never expand truth."""

    job_description_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    role_strategy: ResumeRoleStrategy
    allowed_claims: list[ApplicationClaim] = Field(default_factory=list, max_length=24)
    skills: list[str] = Field(default_factory=list, max_length=48)
    projects: list[str] = Field(default_factory=list, max_length=12)
    excluded_implications: list[str] = Field(default_factory=list, max_length=40)
    required_sections: list[str] = Field(default_factory=list, max_length=6)
    model_rules: list[str] = Field(default_factory=list, max_length=12)


class GeneratedResumeBullet(ContractModel):
    """One generated bullet that retains its authorized claim provenance."""

    bullet_id: str = Field(pattern=r"^BULLET-\d{2}$")
    section: Literal["EXPERIENCE", "PROJECTS", "INDEPENDENT_ENGINEERING", "EDUCATION"]
    text: str = Field(min_length=1, max_length=600)
    source_claim_ids: list[str] = Field(min_length=1, max_length=8)
    project_identity: str | None = Field(default=None, max_length=240)
    contribution_mode: ContributionMode
    generation_status: Literal["MODEL_GENERATED", "DETERMINISTIC_REPAIR"] = (
        "MODEL_GENERATED"
    )
    truth_boundary: str = Field(min_length=1, max_length=400)


class ApplicationResumeDraft(ContractModel):
    """Structured Application Resume draft; provenance never reaches public output."""

    job_description_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    headline: str = Field(min_length=1, max_length=240)
    summary: str = Field(min_length=1, max_length=800)
    skills: list[str] = Field(min_length=1, max_length=48)
    bullets: list[GeneratedResumeBullet] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def validate_bullet_identity(self) -> ApplicationResumeDraft:
        bullet_ids = [item.bullet_id for item in self.bullets]
        if len(bullet_ids) != len(set(bullet_ids)):
            raise ValueError("generated bullet ids must be unique")
        return self


class GenerationViolationCode(StrEnum):
    UNAUTHORIZED_TECHNOLOGY = "unauthorized_technology"
    INVENTED_METRIC = "invented_metric"
    INVENTED_OUTCOME = "invented_outcome"
    OWNERSHIP_INFLATION = "ownership_inflation"
    SCALE_INFLATION = "scale_inflation"
    DEPLOYMENT_INFLATION = "deployment_inflation"
    WEAKENED_CLAIM = "weakened_claim"
    UNKNOWN_SOURCE_CLAIM = "unknown_source_claim"
    CONTRIBUTION_MODE_INFLATION = "contribution_mode_inflation"


class GenerationViolation(ContractModel):
    bullet_id: str | None = Field(default=None, pattern=r"^BULLET-\d{2}$")
    field_name: Literal["headline", "summary"] | None = None
    rule_code: GenerationViolationCode
    detail: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_target(self) -> GenerationViolation:
        if (self.bullet_id is None) == (self.field_name is None):
            raise ValueError("generation violation must identify one bullet or document field")
        return self


class ValidationReport(ContractModel):
    checked_count: int = Field(ge=0)
    violations: list[GenerationViolation] = Field(default_factory=list, max_length=80)
    repaired_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    rejected_skills: list[str] = Field(default_factory=list, max_length=48)
    valid: bool
    post_editorial_validated: bool = False


class CoverageSummary(ContractModel):
    requirement_id: str = Field(pattern=r"^REQ-\d{2}$")
    status: Literal[
        "STRONGLY_REPRESENTED",
        "PARTIALLY_REPRESENTED",
        "AVAILABLE_BUT_NOT_SELECTED",
        "HIGH_VALUE_GAP",
        "UNSUPPORTED",
    ]
    detail: str = Field(min_length=1, max_length=400)


class CoverageReport(ContractModel):
    requirements_total: int = Field(ge=0)
    strongly_represented: list[str] = Field(default_factory=list, max_length=24)
    partially_represented: list[str] = Field(default_factory=list, max_length=24)
    available_not_selected: list[str] = Field(default_factory=list, max_length=24)
    high_value_gaps: list[str] = Field(default_factory=list, max_length=24)
    unsupported: list[str] = Field(default_factory=list, max_length=24)
    summaries: list[CoverageSummary] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def validate_full_accounting(self) -> CoverageReport:
        accounted = (
            len(self.strongly_represented)
            + len(self.partially_represented)
            + len(self.available_not_selected)
            + len(self.high_value_gaps)
            + len(self.unsupported)
        )
        if accounted != self.requirements_total:
            raise ValueError("coverage must account for every requirement exactly once")
        return self


class ResumeQualityReview(ContractModel):
    """Structured human-readable quality review; never expands truth."""

    truth: int = Field(ge=1, le=5)
    role_fit: int = Field(ge=1, le=5)
    specificity: int = Field(ge=1, le=5)
    differentiation: int = Field(ge=1, le=5)
    scanability: int = Field(ge=1, le=5)
    natural_language: int = Field(ge=1, le=5)
    redundancy: int = Field(ge=1, le=5)
    evidence_utilization: int = Field(ge=1, le=5)
    strengths: list[str] = Field(default_factory=list, max_length=12)
    weaknesses: list[str] = Field(default_factory=list, max_length=12)


class GenerationPreflight(ContractModel):
    """Exact intended paid-call preflight; never performs the call itself."""

    provider: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=120)
    reasoning_effort: str = Field(min_length=1, max_length=40)
    thinking_enabled: bool
    credential_status: Literal["CONFIGURED", "NOT_CONFIGURED"]
    job_description_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    requirements_total: int = Field(ge=0)
    allowed_claim_ids: list[str] = Field(default_factory=list, max_length=24)
    selected_projects: list[str] = Field(default_factory=list, max_length=12)
    excluded_terms: list[str] = Field(default_factory=list, max_length=40)
    estimated_stage: str = Field(min_length=1, max_length=120)
    intended_calls: int = Field(default=1, ge=0, le=1)
    automatic_retries: int = Field(default=0, ge=0, le=0)


class GenerationReceipt(ContractModel):
    provider: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=120)
    reasoning_effort: str = Field(min_length=1, max_length=40)
    thinking_enabled: bool
    model_calls: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cache_tokens: int | None = Field(default=None, ge=0)
    status: Literal["SUCCEEDED", "FAILED", "NOT_EXECUTED"]
    error_category: str | None = None
    real_call: bool = False


class ResumeGenerationResult(ContractModel):
    job_description_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    role_strategy: ResumeRoleStrategy
    application_draft: ApplicationResumeDraft
    coverage_report: CoverageReport
    validation_report: ValidationReport
    generation_receipt: GenerationReceipt
    target_gaps: list[TargetGap] = Field(default_factory=list, max_length=80)
