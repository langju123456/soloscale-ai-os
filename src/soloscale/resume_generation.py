"""Candidate Resume generation domain over the evidence-intelligence pipeline.

Real JD → requirement normalization → high-recall retrieval (CP2) → graded claim truth
(CP3) → role strategy → bounded model generation (CP4) → deterministic post-generation
validation → coverage/gap report → traceable Application Resume draft.

This module is not yet connected to the existing ``/resume`` export route. The model may
rephrase, compress, reorder, and combine authorized claims; it may never add technology,
metrics, outcomes, ownership, scale, or deployment.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal, Protocol, cast

from pydantic import BaseModel, Field

from soloscale.model_gateway import ModelCallProfile
from soloscale.resume_claim_truth import build_claim_truth_result
from soloscale.resume_models import (
    ApplicationClaim,
    ApplicationResumeDraft,
    ClaimClass,
    ClaimStrength,
    ClaimTruthResult,
    ContributionMode,
    CoverageReport,
    CoverageSummary,
    EvidenceAuthority,
    EvidenceOwnership,
    GeneratedResumeBullet,
    GenerationPreflight,
    GenerationReceipt,
    GenerationViolation,
    GenerationViolationCode,
    ResumeEvidenceCoverageMap,
    ResumeGenerationContract,
    ResumeGenerationResult,
    ResumeQualityReview,
    ResumeRoleStrategy,
    ValidationReport,
)
from soloscale.resume_retrieval import _ALL_LEXICON_TERMS, capability_expansion

_NUMBER_RE = re.compile(r"(?<!\w)\d+(?:[.,]\d+)?%?\+?[kKmM]?(?!\w)")
_STRONG_VERBS = {
    "implemented",
    "built",
    "designed",
    "developed",
    "engineered",
    "architected",
    "led",
    "shipped",
}
_WEAKENING_VERBS = {
    "familiar",
    "exposed",
    "worked with",
    "studied",
    "used",
    "explored",
    "experimented",
}
_SCALE_TERMS = {
    "production",
    "productionized",
    "enterprise",
    "millions",
    "users",
    "customers",
    "serving",
    "scale",
}
_OUTCOME_TERMS = {"reduced", "improved", "increased", "accelerated", "boosted", "grew"}
_DEPLOYMENT_TERMS = {"deployed", "deployment", "cloud run", "gke", "vertex"}
_FILLER_TERMS = {
    "leveraged",
    "utilized",
    "passionate",
    "dynamic",
    "results-driven",
    "seamless",
    "robust",
    "cutting-edge",
}
_INTERNAL_IDENTITY_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_CONTAINER_PROJECT_NAMES = {
    "ai team",
    "ai team worktrees",
    "resume applications",
    "career",
    "work",
    "docs",
    "src",
    "tests",
    "runs",
    "examples",
}
_PROJECT_TEXT_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("SoloScale AI OS", ("soloscale", "solo-scale")),
    (
        "AI Research Assistant",
        ("ai-research-assistant", "vector_store.py", "ai-agent-demo"),
    ),
    ("BuildLog", ("buildlog",)),
    ("SoloLeverage", ("company-site", "sololeverage")),
)
_MODEL_RULES = (
    ("Rephrase, compress, reorder, and combine only the provided authorized claims."),
    (
        "Never add a technology, metric, outcome, ownership, deployment, or scale "
        "not present in the authorized claims."
    ),
    (
        "Keep bullets specific and short; avoid filler such as leveraged, utilized, "
        "passionate, dynamic, or results-driven."
    ),
    ("Assign every bullet exactly the claim ids and contribution mode provided."),
    ("Do not mention excluded implications."),
)


def canonical_project_name(identity: str) -> str | None:
    """Map an evidence-source identity to a resume project identity, or None."""

    name = Path(identity).name.strip()
    folded = name.casefold().strip(" .")
    if not folded or folded in _CONTAINER_PROJECT_NAMES:
        return None
    if len(folded) > 80 or any(character in folded for character in ("?", "&", "%", "=")):
        return None
    if folded.startswith("unknown"):
        return None
    if _INTERNAL_IDENTITY_RE.fullmatch(folded) is not None:
        return None
    if folded.startswith(("resume-", "evidence-", "run-", "job-", "session-")):
        return None
    if (
        folded in {"solo-scale-ai-os", "soloscale-ai-os", "soloscale"}
        or folded.startswith("solo-scale-ai-os-")
        or folded.startswith("soloscale-")
    ):
        return "SoloScale AI OS"
    if folded.startswith("ai-research-assistant"):
        return "AI Research Assistant"
    if "buildlog" in folded:
        return "BuildLog"
    if folded.startswith("company-site") or "sololeverage" in folded:
        return "SoloLeverage"
    if "evidence-to-narrative" in folded:
        return "Evidence-to-Narrative"
    if folded in {"ai-data-vault"}:
        return "AI Data Vault"
    if folded in {".agency-agents", "agency-agents"}:
        return "Agency Agents"
    return name


def normalize_resume_projects(identities: Sequence[str]) -> list[str]:
    """Roll worktrees/containers up into canonical resume projects."""

    normalized: list[str] = []
    for identity in identities:
        canonical = canonical_project_name(identity)
        if canonical is not None and canonical not in normalized:
            normalized.append(canonical)
    return normalized


class _GeneratedBulletPayload(BaseModel):
    section: Literal["EXPERIENCE", "PROJECTS", "INDEPENDENT_ENGINEERING", "EDUCATION"]
    text: str = Field(min_length=1, max_length=600)
    source_claim_ids: list[str]
    project_identity: str | None = None
    contribution_mode: ContributionMode


class _GeneratedResumePayload(BaseModel):
    headline: str
    summary: str = Field(min_length=1, max_length=800)
    skills: list[str]
    bullets: list[_GeneratedBulletPayload]


class ResumeGenerationGateway(Protocol):
    def complete(
        self,
        schema: type[_GeneratedResumePayload],
        *,
        system: str,
        user: str,
        reasoning_effort: object | None = None,
        thinking_enabled: bool | None = None,
    ) -> _GeneratedResumePayload: ...


class GenerationProviderError(RuntimeError):
    """The selected provider cannot generate without an explicit fallback."""


class _DeepSeekGenerationGateway:
    def __init__(self, gateway: object) -> None:
        self._gateway = gateway

    def complete(
        self,
        schema: type[_GeneratedResumePayload],
        *,
        system: str,
        user: str,
        reasoning_effort: object | None = None,
        thinking_enabled: bool | None = None,
    ) -> _GeneratedResumePayload:
        return cast(
            _GeneratedResumePayload,
            self._gateway.complete(  # type: ignore[attr-defined]
                schema,
                system=system,
                user=user,
                reasoning_effort=(
                    "none" if reasoning_effort == "none" else "low"
                ),
            ),
        )

    @property
    def last_receipt(self) -> ModelCallProfile | None:
        return getattr(self._gateway, "last_call_profile", None)


def _claim_priority(claim: ApplicationClaim) -> int:
    return (
        (2 if claim.claim_class is ClaimClass.VERIFIED else 1)
        + (2 if claim.authority is EvidenceAuthority.HIGH else 1)
        + min(len(claim.evidence_ids), 5)
    )


def select_allowed_claims(
    claim_truth: ClaimTruthResult, *, max_claims: int = 10
) -> list[ApplicationClaim]:
    """Claim budget: strongest, least redundant claims first."""

    ranked = sorted(
        claim_truth.application_claims,
        key=lambda claim: (-_claim_priority(claim), claim.claim_id),
    )
    selected: list[ApplicationClaim] = []
    covered_terms: set[str] = set()
    for claim in ranked:
        vocabulary = set(claim.technology_vocabulary)
        overlap = vocabulary & covered_terms
        if selected and vocabulary and len(overlap) / len(vocabulary) >= 0.75:
            continue
        selected.append(claim)
        covered_terms.update(vocabulary)
        if len(selected) >= max_claims:
            break
    return selected


def build_role_strategy(
    *,
    coverage_map: ResumeEvidenceCoverageMap,
    claim_truth: ClaimTruthResult,
) -> ResumeRoleStrategy:
    """JD-conditioned positioning derived from the graded claim map."""

    claims_by_requirement: dict[str, list[ApplicationClaim]] = {}
    for claim in claim_truth.application_claims:
        claims_by_requirement.setdefault(claim.requirement_id, []).append(claim)
    prioritized = sorted(
        claim_truth.requirement_maps,
        key=lambda item: (
            -sum(
                _claim_priority(claim)
                for claim in claims_by_requirement.get(item.requirement_id, [])
            ),
            item.requirement_id,
        ),
    )
    prioritized_ids = [item.requirement_id for item in prioritized[:8]]
    jd_terms = sorted(
        {
            term
            for item in prioritized[:8]
            for term in capability_expansion(item.requirement_text).technology_terms
        }
    )[:10]
    requirements_by_id = {
        item.requirement_id: item for item in coverage_map.requirements
    }
    evidence_ordered: list[str] = []
    for claim in sorted(
        claim_truth.application_claims,
        key=lambda item: (-_claim_priority(item), item.claim_id),
    ):
        requirement = requirements_by_id[claim.requirement_id]
        excluded: set[str] = set()
        for implication in claim.excluded_implications:
            excluded.add(implication.casefold())
            excluded.update(
                capability_expansion(implication).technology_terms
            )
        for term in claim.technology_vocabulary:
            if (
                term in _ALL_LEXICON_TERMS
                and term not in requirement.technology_terms
                and term not in excluded
                and term not in evidence_ordered
            ):
                evidence_ordered.append(term)
    emphasized = list(dict.fromkeys((*jd_terms, *evidence_ordered)))[:20]
    unsupported = sorted(
        {
            implication
            for claim in claim_truth.application_claims
            for implication in claim.excluded_implications
        }
    )[:20]
    project_ranking: dict[str, int] = {}
    for candidate in coverage_map.candidates:
        if candidate.source_kind.value == "resume_library":
            continue
        project_name = canonical_project_name(candidate.source_identity)
        if project_name is not None:
            project_ranking[project_name] = (
                project_ranking.get(project_name, 0) + 1
            )
        combined = f"{candidate.text} {candidate.source_identity}".casefold()
        for canonical, markers in _PROJECT_TEXT_MARKERS:
            if any(marker in combined for marker in markers):
                project_ranking[canonical] = (
                    project_ranking.get(canonical, 0) + 1
                )
    selected_projects = [
        name
        for name, _count in sorted(
            project_ranking.items(), key=lambda item: (-item[1], item[0])
        )[:6]
    ]
    first_text = (
        coverage_map.requirements[0].text
        if coverage_map.requirements
        else "AI Engineer"
    )
    role_title = first_text.strip(" \t#•-")
    if not role_title:
        role_title = "AI Engineer"
    if "faiss" in evidence_ordered and (
        "chroma" in evidence_ordered or "chromadb" in evidence_ordered
    ):
        headline = f"{role_title} | RAG & vector retrieval — FAISS, Chroma"
    elif evidence_ordered:
        headline = f"{role_title} | {', '.join(evidence_ordered[:3])}"
    else:
        headline = f"{role_title} | {', '.join(jd_terms[:3]) or 'AI engineering'}"
    claim_allocations = {
        requirement_id: [claim.claim_id for claim in claims]
        for requirement_id, claims in claims_by_requirement.items()
    }
    if "faiss" in evidence_ordered and (
        "chroma" in evidence_ordered or "chromadb" in evidence_ordered
    ):
        positioning = (
            f"{role_title} with evidence-backed strengths in "
            f"{', '.join(jd_terms[:4]) or 'software engineering'}, plus "
            "RAG & vector retrieval built on FAISS and Chroma."
        )
    else:
        positioning = (
            f"{role_title} with evidence-backed strengths in "
            f"{', '.join(emphasized[:6]) or 'software engineering'}."
        )
    return ResumeRoleStrategy(
        role_title=role_title,
        positioning=positioning,
        headline=headline,
        prioritized_requirement_ids=prioritized_ids,
        emphasized_terms=emphasized,
        unsupported_terms=unsupported,
        selected_projects=selected_projects,
        claim_allocations=claim_allocations,
        recruiter_scan_priorities=[item.requirement_text[:160] for item in prioritized[:5]],
    )


def build_generation_contract(
    *,
    job_description: str,
    strategy: ResumeRoleStrategy,
    claims: Sequence[ApplicationClaim],
) -> ResumeGenerationContract:
    """Bounded generation context: strategy, allowed claims, and hard rules."""

    vocabulary = sorted(
        {
            term
            for claim in claims
            for term in claim.technology_vocabulary
            if term in _ALL_LEXICON_TERMS
        }
    )
    emphasized = [term for term in strategy.emphasized_terms if term in vocabulary]
    skills = (emphasized + [term for term in vocabulary if term not in emphasized])[:24]
    excluded = sorted(
        {
            implication
            for claim in claims
            for implication in claim.excluded_implications
        }
    )
    return ResumeGenerationContract(
        job_description_sha256=hashlib.sha256(job_description.encode()).hexdigest(),
        role_strategy=strategy,
        allowed_claims=list(claims),
        skills=skills,
        projects=strategy.selected_projects,
        excluded_implications=excluded,
        required_sections=["EXPERIENCE", "PROJECTS", "INDEPENDENT_ENGINEERING", "EDUCATION"],
        model_rules=list(_MODEL_RULES),
    )


def _system_prompt(contract: ResumeGenerationContract) -> str:
    rules = "\n".join(f"- {rule}" for rule in contract.model_rules)
    excluded = ", ".join(contract.excluded_implications) or "none"
    return (
        "You are drafting a truthful submit-ready Application Resume from an authorized "
        "claim set. Use only the provided claims, skills, and projects.\n"
        f"{rules}\n"
        f"Excluded implications (do not state): {excluded}\n"
        "Return the structured schema exactly. Keep bullets under 24 words where possible."
    )


def _contract_user_text(contract: ResumeGenerationContract) -> str:
    return contract.model_dump_json()


def _draft_from_payload(
    *,
    payload: _GeneratedResumePayload,
    job_description: str,
    claims_by_id: dict[str, ApplicationClaim],
) -> ApplicationResumeDraft:
    bullets: list[GeneratedResumeBullet] = []
    for index, item in enumerate(payload.bullets, start=1):
        source_claims = [
            claim
            for claim_id in item.source_claim_ids
            if (claim := claims_by_id.get(claim_id)) is not None
        ]
        boundary = (
            source_claims[0].claim_class.value
            if source_claims
            else "unverified"
        )
        bullets.append(
            GeneratedResumeBullet(
                bullet_id=f"BULLET-{index:02d}",
                section=item.section,
                text=item.text,
                source_claim_ids=item.source_claim_ids,
                project_identity=item.project_identity,
                contribution_mode=item.contribution_mode,
                truth_boundary=boundary,
            )
        )
    return ApplicationResumeDraft(
        job_description_sha256=hashlib.sha256(job_description.encode()).hexdigest(),
        headline=payload.headline,
        summary=payload.summary,
        skills=payload.skills,
        bullets=bullets,
    )


def _text_technology_violation(text: str, allowed: set[str]) -> bool:
    for term in capability_expansion(text).technology_terms:
        if " " not in term:
            if term not in allowed:
                return True
            continue
        for word in term.split():
            if word in _ALL_LEXICON_TERMS and word not in allowed:
                return True
    return False


def _normalized_numbers(text: str) -> set[str]:
    return {
        match.group(0).replace(",", "").casefold()
        for match in _NUMBER_RE.finditer(text)
    }


def _claim_text_violations(
    text: str,
    source: Sequence[ApplicationClaim],
) -> list[GenerationViolationCode]:
    if not source:
        return [GenerationViolationCode.UNKNOWN_SOURCE_CLAIM]
    violations: list[GenerationViolationCode] = []
    excluded: set[str] = set()
    for claim in source:
        for implication in claim.excluded_implications:
            excluded.add(implication.casefold())
            excluded.update(capability_expansion(implication).technology_terms)
    source_text = " ".join(claim.proposed_text for claim in source).casefold()
    excluded = {term for term in excluded if term not in source_text}
    allowed_technology = {
        term for claim in source for term in claim.technology_vocabulary
    } - excluded
    folded = text.casefold()
    if _text_technology_violation(text, allowed_technology) or any(
        term and term in folded for term in excluded
    ):
        violations.append(GenerationViolationCode.UNAUTHORIZED_TECHNOLOGY)
    allowed_numbers = {
        number
        for claim in source
        for number in _normalized_numbers(claim.proposed_text)
    }
    if not _normalized_numbers(text) <= allowed_numbers:
        violations.append(GenerationViolationCode.INVENTED_METRIC)
    if any(term in folded for term in _SCALE_TERMS) and not any(
        term in source_text for term in _SCALE_TERMS
    ):
        violations.append(GenerationViolationCode.SCALE_INFLATION)
    if any(term in folded for term in _OUTCOME_TERMS) and not any(
        term in source_text for term in _OUTCOME_TERMS
    ):
        violations.append(GenerationViolationCode.INVENTED_OUTCOME)
    if any(term in folded for term in _DEPLOYMENT_TERMS) and not any(
        term in source_text for term in _DEPLOYMENT_TERMS
    ):
        violations.append(GenerationViolationCode.DEPLOYMENT_INFLATION)
    if any(verb in folded for verb in _STRONG_VERBS) and any(
        claim.ownership is EvidenceOwnership.UNPROVEN for claim in source
    ):
        violations.append(GenerationViolationCode.OWNERSHIP_INFLATION)
    return violations


def _bullet_violations(
    bullet: GeneratedResumeBullet,
    claims_by_id: dict[str, ApplicationClaim],
) -> list[GenerationViolationCode]:
    source = [
        claim
        for claim_id in bullet.source_claim_ids
        if (claim := claims_by_id.get(claim_id)) is not None
    ]
    violations: list[GenerationViolationCode] = []
    if len(source) != len(bullet.source_claim_ids) or not source:
        violations.append(GenerationViolationCode.UNKNOWN_SOURCE_CLAIM)
        return violations
    violations.extend(_claim_text_violations(bullet.text, source))
    folded = bullet.text.casefold()
    if (
        bullet.contribution_mode is ContributionMode.EXTERNAL_UNKNOWN
        and any(verb in folded for verb in _STRONG_VERBS)
    ):
        violations.append(GenerationViolationCode.CONTRIBUTION_MODE_INFLATION)
    strongest = max(
        source,
        key=lambda claim: (
            1 if claim.strength is ClaimStrength.STRONG else 0,
            claim.claim_id,
        ),
    )
    if (
        any(verb in folded for verb in _WEAKENING_VERBS)
        and strongest.strength in {ClaimStrength.STRONG, ClaimStrength.MODERATE}
    ):
        violations.append(GenerationViolationCode.WEAKENED_CLAIM)
    return violations


def _strongest_claim_text(
    bullet: GeneratedResumeBullet, claims_by_id: dict[str, ApplicationClaim]
) -> str:
    source = [
        claim
        for claim_id in bullet.source_claim_ids
        if (claim := claims_by_id.get(claim_id)) is not None
    ]
    if not source:
        return bullet.text
    return max(
        source,
        key=lambda claim: (
            1 if claim.strength is ClaimStrength.STRONG else 0,
            1 if claim.claim_class is ClaimClass.VERIFIED else 0,
            claim.claim_id,
        ),
    ).proposed_text


def _safe_document_field_text(
    claims: Sequence[ApplicationClaim],
    *,
    maximum: int,
    fallback: str,
) -> str:
    if not claims:
        return fallback
    strongest = max(
        claims,
        key=lambda claim: (
            1 if claim.strength is ClaimStrength.STRONG else 0,
            1 if claim.claim_class is ClaimClass.VERIFIED else 0,
            claim.claim_id,
        ),
    )
    proposed = strongest.proposed_text[:maximum].strip()
    if proposed and not _claim_text_violations(proposed, [strongest]):
        return proposed
    return fallback


def validate_application_draft(
    draft: ApplicationResumeDraft,
    claim_truth: ClaimTruthResult,
) -> tuple[ApplicationResumeDraft, ValidationReport]:
    """Deterministic post-generation truth gate with repair-or-reject semantics."""

    claims_by_id = {claim.claim_id: claim for claim in claim_truth.application_claims}
    violations: list[GenerationViolation] = []
    repaired_count = 0
    rejected_count = 0
    final_bullets: list[GeneratedResumeBullet] = []
    for bullet in draft.bullets:
        codes = _bullet_violations(bullet, claims_by_id)
        if not codes:
            final_bullets.append(bullet)
            continue
        violations.extend(
            GenerationViolation(
                bullet_id=bullet.bullet_id,
                rule_code=code,
                detail=f"{code.value} in generated bullet",
            )
            for code in codes
        )
        if GenerationViolationCode.UNKNOWN_SOURCE_CLAIM in codes:
            rejected_count += 1
            continue
        repaired = bullet.model_copy(
            update={
                "text": _strongest_claim_text(bullet, claims_by_id),
                "generation_status": "DETERMINISTIC_REPAIR",
            }
        )
        if _bullet_violations(repaired, claims_by_id):
            rejected_count += 1
            continue
        repaired_count += 1
        final_bullets.append(repaired)
    document_updates: dict[str, str] = {}
    all_claims = list(claim_truth.application_claims)
    for field_name, maximum, fallback in (
        ("headline", 240, "Evidence-backed engineering experience"),
        (
            "summary",
            800,
            "The statements below are limited to verified or supported evidence.",
        ),
    ):
        text = getattr(draft, field_name)
        codes = _claim_text_violations(text, all_claims)
        if not codes:
            continue
        violations.extend(
            GenerationViolation(
                field_name=cast(Literal["headline", "summary"], field_name),
                rule_code=code,
                detail=f"{code.value} in generated {field_name}",
            )
            for code in codes
        )
        document_updates[field_name] = _safe_document_field_text(
            all_claims,
            maximum=maximum,
            fallback=fallback,
        )
        repaired_count += 1
    valid = bool(final_bullets)
    allowed_skill_terms: set[str] = set()
    for claim in claim_truth.application_claims:
        excluded_terms: set[str] = set()
        for implication in claim.excluded_implications:
            excluded_terms.add(implication.casefold())
            excluded_terms.update(
                capability_expansion(implication).technology_terms
            )
        allowed_skill_terms |= set(claim.technology_vocabulary) - excluded_terms
    kept_skills = [
        skill
        for skill in draft.skills
        if _skill_supported(skill, allowed_skill_terms)
    ]
    rejected_skills = [
        skill for skill in draft.skills if skill not in kept_skills
    ]
    repaired_draft = draft.model_copy(
        update={
            "bullets": final_bullets,
            "skills": kept_skills,
            **document_updates,
        }
    )
    return repaired_draft, ValidationReport(
        checked_count=len(draft.bullets) + 2,
        violations=violations,
        repaired_count=repaired_count,
        rejected_count=rejected_count,
        rejected_skills=rejected_skills,
        valid=valid,
    )


def _skill_supported(skill: str, allowed: set[str]) -> bool:
    technology = set(capability_expansion(skill).technology_terms)
    return not technology or technology <= allowed


def apply_editorial_review(
    draft: ApplicationResumeDraft,
    claim_truth: ClaimTruthResult,
    *,
    editorial: Callable[[str], str] | None = None,
) -> tuple[ApplicationResumeDraft, ValidationReport]:
    """Stylistic review may improve wording but can never bypass the truth gate."""

    if editorial is None:
        polished = draft
    else:
        polished = draft.model_copy(
            update={
                "bullets": [
                    bullet.model_copy(update={"text": editorial(bullet.text)})
                    for bullet in draft.bullets
                ]
            }
        )
    final, report = validate_application_draft(polished, claim_truth)
    report = report.model_copy(update={"post_editorial_validated": True})
    return final, report


def build_coverage_report(
    *,
    claim_truth: ClaimTruthResult,
    draft: ApplicationResumeDraft | None,
) -> CoverageReport:
    claim_requirement_ids = {
        claim.claim_id: claim.requirement_id
        for claim in claim_truth.application_claims
    }
    used = (
        {
            claim_requirement_ids.get(claim_id, "")
            for bullet in draft.bullets
            for claim_id in bullet.source_claim_ids
        }
        if draft is not None
        else set()
    )
    used.discard("")
    gap_requirements = {gap.requirement_id for gap in claim_truth.target_gaps}
    strongly: list[str] = []
    partially: list[str] = []
    available_not_selected: list[str] = []
    unsupported: list[str] = []
    gaps: list[str] = []
    summaries: list[CoverageSummary] = []
    for requirement in claim_truth.requirement_maps:
        verified_claims = [
            claim
            for claim in claim_truth.application_claims
            if claim.requirement_id == requirement.requirement_id
            and claim.claim_class is ClaimClass.VERIFIED
        ]
        supported_claims = [
            claim
            for claim in claim_truth.application_claims
            if claim.requirement_id == requirement.requirement_id
            and claim.claim_class is ClaimClass.SUPPORTED_DERIVATION
        ]
        if requirement.requirement_id in used and verified_claims:
            status: Literal[
                "STRONGLY_REPRESENTED",
                "PARTIALLY_REPRESENTED",
                "AVAILABLE_BUT_NOT_SELECTED",
                "HIGH_VALUE_GAP",
                "UNSUPPORTED",
            ] = "STRONGLY_REPRESENTED"
            strongly.append(requirement.requirement_id)
        elif requirement.requirement_id in used and supported_claims:
            status = "PARTIALLY_REPRESENTED"
            partially.append(requirement.requirement_id)
        elif requirement.requirement_id in gap_requirements:
            status = "HIGH_VALUE_GAP"
            gaps.append(requirement.requirement_id)
        elif verified_claims or supported_claims:
            status = "AVAILABLE_BUT_NOT_SELECTED"
            available_not_selected.append(requirement.requirement_id)
        else:
            status = "UNSUPPORTED"
            unsupported.append(requirement.requirement_id)
        summaries.append(
            CoverageSummary(
                requirement_id=requirement.requirement_id,
                status=status,
                detail=requirement.requirement_text[:200],
            )
        )
    return CoverageReport(
        requirements_total=len(claim_truth.requirement_maps),
        strongly_represented=strongly,
        partially_represented=partially,
        available_not_selected=available_not_selected,
        high_value_gaps=gaps,
        unsupported=unsupported,
        summaries=summaries,
    )


def generate_application_resume(
    *,
    job_description: str,
    coverage_map: ResumeEvidenceCoverageMap,
    gateway: ResumeGenerationGateway,
    owned_project_markers: Sequence[str] = (),
    max_claims: int = 10,
    reasoning_effort: str = "high",
    thinking_enabled: bool = True,
    real_call: bool = False,
) -> ResumeGenerationResult:
    """Run the full canonical generation path through the model boundary."""

    claim_truth = build_claim_truth_result(
        job_description=job_description,
        coverage_map=coverage_map,
        owned_project_markers=owned_project_markers,
    )
    strategy = build_role_strategy(
        coverage_map=coverage_map, claim_truth=claim_truth
    )
    claims = select_allowed_claims(claim_truth, max_claims=max_claims)
    if not claims:
        raise GenerationProviderError(
            "no allowed claims are available for generation"
        )
    contract = build_generation_contract(
        job_description=job_description, strategy=strategy, claims=claims
    )
    claims_by_id = {claim.claim_id: claim for claim in claims}
    try:
        payload = gateway.complete(
            _GeneratedResumePayload,
            system=_system_prompt(contract),
            user=_contract_user_text(contract),
            reasoning_effort=reasoning_effort,
            thinking_enabled=thinking_enabled,
        )
    except GenerationProviderError:
        raise
    except Exception as exc:
        raise GenerationProviderError(
            "the selected provider could not complete generation"
        ) from exc
    draft = _draft_from_payload(
        payload=payload,
        job_description=job_description,
        claims_by_id=claims_by_id,
    )
    validated_draft, validation_report = validate_application_draft(
        draft, claim_truth
    )
    receipt = _receipt_from_gateway(
        gateway,
        reasoning_effort=reasoning_effort,
        thinking_enabled=thinking_enabled,
        real_call=real_call,
    )
    coverage = build_coverage_report(
        claim_truth=claim_truth, draft=validated_draft
    )
    return ResumeGenerationResult(
        job_description_sha256=hashlib.sha256(job_description.encode()).hexdigest(),
        role_strategy=strategy,
        application_draft=validated_draft,
        coverage_report=coverage,
        validation_report=validation_report,
        generation_receipt=receipt,
        target_gaps=claim_truth.target_gaps,
    )


def _receipt_from_gateway(
    gateway: ResumeGenerationGateway,
    *,
    reasoning_effort: str,
    thinking_enabled: bool,
    real_call: bool,
) -> GenerationReceipt:
    call_profile = getattr(gateway, "last_receipt", None)
    if isinstance(call_profile, ModelCallProfile):
        return GenerationReceipt(
            provider=call_profile.provider.value,
            model=call_profile.model,
            reasoning_effort=call_profile.reasoning_effort,
            thinking_enabled=call_profile.thinking_enabled,
            model_calls=1,
            latency_ms=call_profile.wall_ms,
            input_tokens=call_profile.prompt_eval_tokens,
            output_tokens=call_profile.output_tokens,
            cache_tokens=None,
            status="SUCCEEDED",
            real_call=real_call,
        )
    return GenerationReceipt(
        provider="unknown",
        model="unknown",
        reasoning_effort=reasoning_effort,
        thinking_enabled=thinking_enabled,
        model_calls=1,
        latency_ms=0,
        status="SUCCEEDED",
        real_call=real_call,
    )


def deepseek_generation_gateway(gateway: object) -> ResumeGenerationGateway:
    """Wrap the canonical DeepSeek gateway for structured Resume generation."""

    return _DeepSeekGenerationGateway(gateway)


def build_generation_preflight(
    *,
    job_description: str,
    claim_truth: ClaimTruthResult,
    strategy: ResumeRoleStrategy,
    claims: Sequence[ApplicationClaim],
    provider: str = "deepseek",
    model: str = "deepseek-v4-pro",
    reasoning_effort: str = "high",
    thinking_enabled: bool = True,
    credential_configured: bool = False,
) -> GenerationPreflight:
    """Exact intended call plan. This function never performs the call."""

    excluded = sorted(
        {
            implication
            for claim in claims
            for implication in claim.excluded_implications
        }
    )
    return GenerationPreflight(
        provider=provider,
        model=model,
        reasoning_effort=reasoning_effort,
        thinking_enabled=thinking_enabled,
        credential_status=(
            "CONFIGURED" if credential_configured else "NOT_CONFIGURED"
        ),
        job_description_sha256=hashlib.sha256(job_description.encode()).hexdigest(),
        requirements_total=len(claim_truth.requirement_maps),
        allowed_claim_ids=[claim.claim_id for claim in claims],
        selected_projects=strategy.selected_projects,
        excluded_terms=excluded,
        estimated_stage="one structured Application Resume generation",
        intended_calls=1,
        automatic_retries=0,
    )


def render_application_resume(draft: ApplicationResumeDraft) -> str:
    """Public recruiter-facing text with no internal provenance labels."""

    lines = [f"# {draft.headline}", "", draft.summary, "", "## Skills"]
    lines.append(", ".join(draft.skills))
    for section in ("EXPERIENCE", "PROJECTS", "INDEPENDENT_ENGINEERING", "EDUCATION"):
        bullets = [item.text for item in draft.bullets if item.section == section]
        if not bullets:
            continue
        lines.append("")
        lines.append(f"## {section.title().replace('_', ' ')}")
        lines.extend(f"- {text}" for text in bullets)
    return "\n".join(lines) + "\n"


def review_application_resume(
    draft: ApplicationResumeDraft,
    claim_truth: ClaimTruthResult,
) -> ResumeQualityReview:
    """Structured deterministic quality review; never expands truth."""

    bullets = draft.bullets
    texts = [bullet.text for bullet in bullets]
    combined = " ".join(texts).casefold()
    tech_terms = {
        term
        for text in texts
        for term in capability_expansion(text).technology_terms
    }
    used_claim_ids = {
        claim_id for bullet in bullets for claim_id in bullet.source_claim_ids
    }
    available_claim_ids = {
        claim.claim_id for claim in claim_truth.application_claims
    }
    strengths: list[str] = []
    weaknesses: list[str] = []

    specificity = min(5, 2 + sum(
        1 for text in texts
        if len(capability_expansion(text).technology_terms) >= 2
    ))
    average_words = (
        sum(len(text.split()) for text in texts) / max(len(texts), 1)
    )
    scanability = 5 if average_words <= 24 else 4 if average_words <= 34 else 2
    filler_count = sum(1 for term in _FILLER_TERMS if term in combined)
    natural_language = max(2, 5 - filler_count)
    term_counts: dict[str, int] = {}
    for text in texts:
        for term in capability_expansion(text).technology_terms:
            term_counts[term] = term_counts.get(term, 0) + 1
    repeated = [term for term, count in term_counts.items() if count >= 3]
    redundancy = max(2, 5 - len(repeated))
    differentiation = min(5, 2 + len(tech_terms) // 3)
    evidence_utilization = min(
        5,
        1 + round(4 * len(used_claim_ids) / max(len(available_claim_ids), 1)),
    )
    role_fit = min(5, 2 + len(tech_terms) // 4)
    truth = 5 if bullets and all(
        bullet.generation_status in {"MODEL_GENERATED", "DETERMINISTIC_REPAIR"}
        for bullet in bullets
    ) else 3
    if len(tech_terms) >= 5:
        strengths.append("technically specific evidence vocabulary")
    if not repeated:
        strengths.append("no repeated claim vocabulary")
    if average_words <= 24:
        strengths.append("concise recruiter-scannable bullets")
    if filler_count:
        weaknesses.append(f"{filler_count} filler word(s) present")
    if repeated:
        weaknesses.append(f"repeated technology terms: {', '.join(sorted(repeated)[:5])}")
    if average_words > 34:
        weaknesses.append("bullets run long")
    if not weaknesses:
        weaknesses.append("no deterministic weaknesses detected")
    return ResumeQualityReview(
        truth=truth,
        role_fit=role_fit,
        specificity=specificity,
        differentiation=differentiation,
        scanability=scanability,
        natural_language=natural_language,
        redundancy=redundancy,
        evidence_utilization=evidence_utilization,
        strengths=strengths,
        weaknesses=weaknesses,
    )
