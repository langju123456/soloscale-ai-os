"""Graded claim truth and Application vs Target Resume separation.

This stage consumes the checkpoint-2 Evidence Coverage Map and decides what each retrieved
piece of evidence actually authorizes us to say. It never fabricates claims: it classifies
evidence by authority, ownership, and contamination, then derives the strongest truthful
wording for the submit-ready Application Resume while surfacing labeled gaps for the
Target Resume. Final human review remains mandatory.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence

from soloscale.resume_models import (
    ApplicationClaim,
    ClaimClass,
    ClaimComponentVerdict,
    ClaimStrength,
    ClaimTruthResult,
    ClaimValidationCode,
    ContaminationKind,
    EvidenceAuthority,
    EvidenceOwnership,
    GapAction,
    GradedEvidence,
    RequirementClaimMap,
    ResumeEvidenceCoverageMap,
    ResumeRequirementExpansion,
    ResumeRetrievalCandidate,
    ResumeRetrievalSourceKind,
    TargetGap,
)
from soloscale.resume_retrieval import _ALL_LEXICON_TERMS, capability_expansion

_IMPLEMENTATION_RE = re.compile(
    r"(?:\bdef\s+\w+|\bclass\s+\w+|\bself\.\w+|::|\.py\b|\btest_\w+"
    r"|\bpytest\b|\bexit_code\b|\bFAILURES\b|\bHTTP 201\b|\bgit\s+commit\b"
    r"|\.github/workflows)",
    re.I,
)
_NUMBER_RE = re.compile(r"(?<!\w)\d+(?:[.,]\d+)?%?\+?(?!\w)")
_COURSE_MARKERS = (
    "course",
    "certificate",
    "transcript",
    "assignment",
    "submission",
    "hugging face",
    "instructure",
    "canvas",
    "tutorial",
    "lesson",
    "practice",
    "unit ",
)
_SUGGESTION_MARKERS = (
    "建议",
    "应该",
    "可以写",
    "不要",
    "简历",
    "recommend",
    "suggest",
    "consider",
    "should mention",
    "bullet",
    "tailor",
    "jd",
)
_CAPABILITY_NOUNS = {
    "rag_embedding_retrieval": "retrieval layer",
    "agentic_workflows": "agent workflow",
    "evaluation": "evaluation harness",
    "backend_api": "backend service",
    "cloud_infra": "deployment pipeline",
    "data_storage": "data layer",
    "frontend": "frontend",
    "testing_reliability": "test suite",
    "general": "engineering work",
}
_CAPABILITY_CANONICAL_TERMS = {
    "rag_embedding_retrieval": frozenset({"rag", "retrieval"}),
    "agentic_workflows": frozenset({"agent", "workflow"}),
    "evaluation": frozenset({"evaluation"}),
    "backend_api": frozenset({"backend", "api", "service"}),
    "cloud_infra": frozenset({"deployment", "pipeline"}),
    "data_storage": frozenset({"data", "layer"}),
    "frontend": frozenset({"frontend"}),
    "testing_reliability": frozenset({"test", "suite"}),
    "general": frozenset({"engineering", "work"}),
}
_RAG_COMPONENTS: tuple[tuple[str, frozenset[str], frozenset[str]], ...] = (
    (
        "FAISS/Chroma vector-store implementation",
        frozenset({"faiss", "chroma", "chromadb", "vector store", "vector_store"}),
        frozenset(),
    ),
    (
        "vector / similarity retrieval",
        frozenset(
            {
                "vector",
                "similarity",
                "similarity search",
                "cosine similarity",
                "retrieval",
                "vector index",
                "hybrid search",
            }
        ),
        frozenset(),
    ),
    (
        "embedding generation",
        frozenset(
            {
                "embedding",
                "embeddings",
                "embedding-based",
                "sbert",
                "sentence-transformers",
            }
        ),
        frozenset({"embedding", "embeddings", "embedding-based"}),
    ),
    (
        "Vertex AI / Google Cloud RAG",
        frozenset(
            {"vertex", "vertex ai", "google cloud", "gcp", "cloud run", "cloud"}
        ),
        frozenset({"rag", "retrieval", "chunking"}),
    ),
    ("reranking", frozenset({"rerank", "reranking"}), frozenset({"rag", "retrieval"})),
    (
        "grounding / grounding evaluation",
        frozenset({"grounding", "hallucination", "grounding checks", "answer quality"}),
        frozenset({"evaluation", "hallucination", "rag"}),
    ),
)
_GENERIC_APPLICATION_ACTIONS = (
    GapAction.SEARCH_MORE,
    GapAction.USER_SUPPLEMENT,
    GapAction.LEARNING_CASE,
    GapAction.IGNORE,
)
_ABSTRACTION_COMPONENTS = frozenset({"vector / similarity retrieval"})
_GENERIC_TECH_TERMS = frozenset(
    {
        "agent",
        "agents",
        "agentic",
        "api",
        "apis",
        "service",
        "workflow",
        "backend",
        "test",
        "suite",
        "data",
        "layer",
        "frontend",
        "deployment",
        "pipeline",
        "engineering",
        "work",
        "web",
        "documentation",
        "planning",
        "memory",
        "schema",
        "prompt",
        "prompts",
        "quality",
        "reliability",
        "evaluation",
    }
)


def _evidence_terms(candidate: ResumeRetrievalCandidate) -> set[str]:
    text = f"{candidate.text} {candidate.source_identity}"
    expansion = capability_expansion(text)
    return set(expansion.technology_terms) | set(candidate.signals)


def _requirement_token_set(text: str) -> set[str]:
    expansion = capability_expansion(text)
    return set(expansion.requirement_tokens)


def _contamination(
    candidate: ResumeRetrievalCandidate,
    requirement_texts: Sequence[str],
) -> ContaminationKind:
    text = candidate.text.casefold()
    tokens = _requirement_token_set(text)
    for requirement_text in requirement_texts:
        requirement_tokens = _requirement_token_set(requirement_text)
        if not requirement_tokens:
            continue
        overlap = len(tokens & requirement_tokens)
        if overlap / len(requirement_tokens) >= 0.6:
            return ContaminationKind.JD_QUERY_ECHO
    combined = f"{candidate.text} {candidate.source_identity}".casefold()
    if any(marker in combined for marker in _COURSE_MARKERS):
        return ContaminationKind.COURSE_EXPOSURE
    if any(marker in combined for marker in _SUGGESTION_MARKERS):
        return ContaminationKind.MODEL_SUGGESTION
    return ContaminationKind.NONE


def _is_implementation(candidate: ResumeRetrievalCandidate) -> bool:
    if candidate.evidence_type in {"repository", "file:test", "file:build_ci", "file:code"}:
        return True
    return _IMPLEMENTATION_RE.search(
        f"{candidate.text} {candidate.source_identity}"
    ) is not None


def _authority(
    candidate: ResumeRetrievalCandidate,
    contamination: ContaminationKind,
    is_implementation: bool,
) -> EvidenceAuthority:
    if contamination is ContaminationKind.JD_QUERY_ECHO:
        return EvidenceAuthority.NON_SUPPORTING
    if contamination in {
        ContaminationKind.COURSE_EXPOSURE,
        ContaminationKind.MODEL_SUGGESTION,
    }:
        return EvidenceAuthority.LOW_CONTEXTUAL
    if (
        candidate.source_kind is ResumeRetrievalSourceKind.EVIDENCE_HUB
        and candidate.provenance.get("verification_status") == "verified"
    ):
        return EvidenceAuthority.HIGH
    if (
        candidate.source_kind is ResumeRetrievalSourceKind.LOCAL_GIT
        and candidate.provenance.get("commit")
    ):
        return EvidenceAuthority.HIGH
    if candidate.source_kind is ResumeRetrievalSourceKind.MAC_CATALOG:
        kind = candidate.evidence_type or ""
        if kind.startswith("file:") and kind.removeprefix("file:") in {
            "code",
            "test",
            "build_ci",
        }:
            return EvidenceAuthority.HIGH
        return EvidenceAuthority.MEDIUM
    if is_implementation and candidate.source_kind in {
        ResumeRetrievalSourceKind.CODEX,
        ResumeRetrievalSourceKind.BUILDLOG,
        ResumeRetrievalSourceKind.CHATGPT,
    }:
        return EvidenceAuthority.HIGH
    if candidate.source_kind in {
        ResumeRetrievalSourceKind.CODEX,
        ResumeRetrievalSourceKind.BUILDLOG,
        ResumeRetrievalSourceKind.RESUME_LIBRARY,
    }:
        return EvidenceAuthority.MEDIUM
    return EvidenceAuthority.LOW_CONTEXTUAL


def _document_key(candidate: ResumeRetrievalCandidate) -> str:
    return (
        candidate.provenance.get("document_sha256")
        or candidate.provenance.get("external_id")
        or candidate.source_identity
    )


def classify_evidence(
    candidates: Sequence[ResumeRetrievalCandidate],
    *,
    requirement_texts: Sequence[str],
    owned_project_markers: Sequence[str] = (),
) -> tuple[list[GradedEvidence], dict[ContaminationKind, int]]:
    """Classify authority, ownership, and contamination for every candidate."""

    markers = tuple(marker.casefold() for marker in owned_project_markers if marker.strip())
    proven_documents: set[str] = set()
    for candidate in candidates:
        combined = f"{candidate.text} {candidate.source_identity}".casefold()
        if any(marker in combined for marker in markers):
            proven_documents.add(_document_key(candidate))

    graded: list[GradedEvidence] = []
    counts: dict[ContaminationKind, int] = {}
    for candidate in candidates:
        contamination = _contamination(candidate, requirement_texts)
        counts[contamination] = counts.get(contamination, 0) + 1
        is_implementation = _is_implementation(candidate)
        authority = _authority(candidate, contamination, is_implementation)
        if (
            candidate.source_kind is ResumeRetrievalSourceKind.EVIDENCE_HUB
            and candidate.provenance.get("verification_status") == "verified"
        ):
            ownership = EvidenceOwnership.PROVEN
        elif _document_key(candidate) in proven_documents:
            ownership = EvidenceOwnership.PROVEN
        else:
            ownership = EvidenceOwnership.UNPROVEN
        graded.append(
            GradedEvidence(
                evidence_id=candidate.candidate_id,
                source_kind=candidate.source_kind,
                authority=authority,
                ownership=ownership,
                contamination=contamination,
                capability_terms=sorted(_evidence_terms(candidate))[:40],
                is_implementation=is_implementation,
            )
        )
    return graded, counts


def _component_verdict(
    component: str,
    component_terms: frozenset[str],
    context_terms: frozenset[str],
    candidates: Sequence[ResumeRetrievalCandidate],
    graded_by_id: dict[str, GradedEvidence],
) -> ClaimComponentVerdict:
    matching: list[ResumeRetrievalCandidate] = []
    for candidate in candidates:
        graded = graded_by_id[candidate.candidate_id]
        overlap = component_terms & set(graded.capability_terms)
        if overlap:
            matching.append(candidate)
    strong = [
        candidate
        for candidate in matching
        if graded_by_id[candidate.candidate_id].authority is EvidenceAuthority.HIGH
        and graded_by_id[candidate.candidate_id].ownership is EvidenceOwnership.PROVEN
        and context_terms
        <= set(graded_by_id[candidate.candidate_id].capability_terms)
        and _implements_terms(candidate.text, component_terms)
    ]
    derivable = [
        candidate
        for candidate in matching
        if graded_by_id[candidate.candidate_id].authority
        in {EvidenceAuthority.HIGH, EvidenceAuthority.MEDIUM}
        and graded_by_id[candidate.candidate_id].ownership is EvidenceOwnership.PROVEN
        and len(component_terms & set(graded_by_id[candidate.candidate_id].capability_terms))
        >= 2
        and context_terms
        <= set(graded_by_id[candidate.candidate_id].capability_terms)
    ]
    if strong and component not in _ABSTRACTION_COMPONENTS:
        evidence_ids = [item.candidate_id for item in strong]
        return ClaimComponentVerdict(
            component=component,
            claim_class=ClaimClass.VERIFIED,
            evidence_ids=evidence_ids,
            rationale=(
                f"direct high-authority implementation evidence ({len(strong)} item(s))"
            ),
        )
    if derivable or (strong and component in _ABSTRACTION_COMPONENTS):
        evidence_ids = [
            item.candidate_id for item in (derivable or strong)
        ]
        return ClaimComponentVerdict(
            component=component,
            claim_class=ClaimClass.SUPPORTED_DERIVATION,
            evidence_ids=evidence_ids,
            rationale=(
                "strongly supported professional abstraction; no material new technology"
            ),
        )
    evidence_ids = [item.candidate_id for item in matching]
    if matching:
        return ClaimComponentVerdict(
            component=component,
            claim_class=ClaimClass.HIGH_VALUE_GAP,
            evidence_ids=evidence_ids,
            missing_proof=[
                f"no direct implementation evidence for: {', '.join(sorted(component_terms))}"
            ],
            rationale="related evidence exists but does not yet authorize the specific claim",
        )
    return ClaimComponentVerdict(
        component=component,
        claim_class=ClaimClass.UNSUPPORTED,
        rationale="no adequate evidence found for this component",
    )


def _implements_terms(text: str, terms: frozenset[str]) -> bool:
    folded = text.casefold()
    for line in folded.splitlines():
        if _IMPLEMENTATION_RE.search(line) and any(term in line for term in terms):
            return True
    return False


def _join_tech(terms: Sequence[str]) -> str:
    ordered = [term.upper() if term in {"faiss", "sbert"} else term.title() for term in terms]
    if len(ordered) == 1:
        return ordered[0]
    return f"{', '.join(ordered[:-1])} and {ordered[-1]}"


def propose_claim_text(
    capability: str,
    component: str,
    terms: set[str],
    claim_class: ClaimClass,
) -> str:
    """Derive deterministic, evidence-bounded wording for one claim."""

    if capability == "rag_embedding_retrieval":
        if component.startswith("FAISS/Chroma") and claim_class is ClaimClass.VERIFIED:
            return "Implemented a FAISS/Chroma vector store for document and query vectors."
        if component == "Vertex AI / Google Cloud RAG" and claim_class is ClaimClass.VERIFIED:
            return "Deployed RAG pipelines on Google Cloud / Vertex AI."
        if component == "embedding generation" and claim_class is ClaimClass.VERIFIED:
            return "Implemented an embedding-generation stage for document and query vectors."
        if component == "reranking" and claim_class is ClaimClass.VERIFIED:
            return "Implemented reranking for retrieval results."
        if (
            component == "grounding / grounding evaluation"
            and claim_class is ClaimClass.VERIFIED
        ):
            return "Defined and ran grounding evaluation for retrieval answers."
        if (
            {"faiss"} <= terms
            and ({"chroma"} <= terms or {"chromadb"} <= terms)
            and {"vector"} <= terms
            and {"similarity"} <= terms
        ):
            return (
                "Built a RAG retrieval layer using FAISS and Chroma for vector "
                "similarity search."
            )
        if {"reranking"} <= terms and {"retrieval"} <= terms:
            return "Built retrieval with reranking for ranked results."
        if {"vector"} <= terms and {"retrieval"} <= terms:
            return "Built vector retrieval over indexed documents."
        tech = sorted(
            term
            for term in terms
            if term
            in {
                "faiss",
                "chroma",
                "chromadb",
                "sbert",
                "retrieval",
                "similarity",
                "hybrid search",
            }
        )
        if tech:
            return f"Built retrieval components using {_join_tech(tech[:2])}."
        return "Worked with retrieval and vector-search components."
    noun = _CAPABILITY_NOUNS.get(capability, "engineering work")
    distinctive = sorted(
        term
        for term in terms
        if len(term) <= 24 and term not in _GENERIC_TECH_TERMS
    )
    fallback = sorted(term for term in terms if len(term) <= 24)
    tech = (distinctive or fallback)[:2]
    if claim_class is ClaimClass.VERIFIED:
        if tech:
            return f"Implemented {noun} using {_join_tech(tech)}."
        return f"Implemented {noun}."
    if tech:
        return f"Built {noun} using {_join_tech(tech)}."
    return f"Worked with {noun} components."


def _gap_suggestion(capability: str, component: str) -> str:
    if component == "Vertex AI / Google Cloud RAG":
        return (
            "Deployed RAG pipelines on Google Cloud / Vertex AI with reranking "
            "and grounding evaluation."
        )
    if component == "reranking":
        return "Implemented reranking for retrieval results and measured retrieval quality."
    if component == "grounding / grounding evaluation":
        return "Defined and ran grounding / hallucination evaluation with quality gates."
    if component == "embedding generation":
        return "Implemented an embedding-generation stage for document and query vectors."
    if capability == "rag_embedding_retrieval":
        return f"Demonstrated {component} in a real project."
    return f"Demonstrated {component} in a real project."


def _strength(
    claim_class: ClaimClass,
    evidence: Sequence[GradedEvidence],
) -> ClaimStrength:
    any_implementation = any(item.is_implementation for item in evidence)
    all_proven = all(item.ownership is EvidenceOwnership.PROVEN for item in evidence)
    any_high = any(item.authority is EvidenceAuthority.HIGH for item in evidence)
    any_low = any(
        item.authority is EvidenceAuthority.LOW_CONTEXTUAL for item in evidence
    )
    if claim_class is ClaimClass.VERIFIED and any_implementation and all_proven:
        return ClaimStrength.STRONG
    if claim_class is ClaimClass.VERIFIED and all_proven:
        return ClaimStrength.MODERATE
    if claim_class is ClaimClass.SUPPORTED_DERIVATION and all_proven and any_high:
        return ClaimStrength.MODERATE
    if claim_class is ClaimClass.SUPPORTED_DERIVATION and all_proven:
        return ClaimStrength.BOUNDED
    if any_high or any_implementation:
        return ClaimStrength.CONTRIBUTION_SAFE
    if any_low:
        return ClaimStrength.EXPOSURE_SAFE
    return ClaimStrength.LEARNING_ONLY


def build_claim_truth_result(
    *,
    job_description: str,
    coverage_map: ResumeEvidenceCoverageMap,
    owned_project_markers: Sequence[str] = (),
) -> ClaimTruthResult:
    """Turn a coverage map into graded claims plus Application/Target separation."""

    requirement_texts = [item.text for item in coverage_map.requirements]
    graded, contamination_counts = classify_evidence(
        coverage_map.candidates,
        requirement_texts=requirement_texts,
        owned_project_markers=owned_project_markers,
    )
    graded_by_id = {item.evidence_id: item for item in graded}

    requirement_maps: list[RequirementClaimMap] = []
    application_claims: list[ApplicationClaim] = []
    target_gaps: list[TargetGap] = []
    verified_count = 0
    supported_count = 0
    gap_count = 0
    unsupported_count = 0
    claim_index = 0
    gap_index = 0

    for requirement in coverage_map.requirements:
        if requirement.capability == "rag_embedding_retrieval":
            component_specs = _RAG_COMPONENTS
        else:
            component_specs = (
                (
                    f"{requirement.capability} capability evidence",
                    frozenset(requirement.technology_terms),
                    frozenset(),
                ),
            )
        verdicts: list[ClaimComponentVerdict] = []
        for component, component_terms, context_terms in component_specs:
            verdict = _component_verdict(
                component,
                component_terms,
                context_terms,
                coverage_map.candidates,
                graded_by_id,
            )
            verdicts.append(verdict)
            if verdict.claim_class is ClaimClass.VERIFIED:
                verified_count += 1
            elif verdict.claim_class is ClaimClass.SUPPORTED_DERIVATION:
                supported_count += 1
            elif verdict.claim_class is ClaimClass.HIGH_VALUE_GAP:
                gap_count += 1
            else:
                unsupported_count += 1

            if verdict.claim_class in {
                ClaimClass.VERIFIED,
                ClaimClass.SUPPORTED_DERIVATION,
            }:
                evidence = [graded_by_id[item] for item in verdict.evidence_ids]
                evidence_vocabulary = sorted(
                    {
                        term
                        for item in evidence
                        for term in item.capability_terms
                    }
                )
                terms = set(evidence_vocabulary)
                claim_vocabulary = sorted(
                    set(evidence_vocabulary)
                    | set(requirement.technology_terms)
                    | set(_CAPABILITY_CANONICAL_TERMS.get(requirement.capability, frozenset()))
                )
                claim_index += 1
                claim = ApplicationClaim(
                    claim_id=f"CLAIM-{claim_index:02d}",
                    requirement_id=requirement.requirement_id,
                    claim_class=verdict.claim_class,
                    strength=_strength(verdict.claim_class, evidence),
                    proposed_text=propose_claim_text(
                        requirement.capability, component, terms, verdict.claim_class
                    ),
                    evidence_ids=verdict.evidence_ids,
                    authority=max(
                        (item.authority for item in evidence),
                        key=lambda value: _AUTHORITY_PRIORITY[value],
                    ),
                    ownership=(
                        EvidenceOwnership.PROVEN
                        if all(
                            item.ownership is EvidenceOwnership.PROVEN for item in evidence
                        )
                        else EvidenceOwnership.UNPROVEN
                    ),
                    derivation_rationale=verdict.rationale,
                    truth_boundary=(
                        f"{verdict.claim_class.value} from {len(evidence)} evidence "
                        f"item(s); strongest authority "
                        f"{max(item.authority.value for item in evidence)}"
                    ),
                        excluded_implications=_excluded_implications(
                            requirement, evidence_vocabulary
                        ),
                        technology_vocabulary=claim_vocabulary,
                    )
                application_claims.append(claim)
            else:
                gap_index += 1
                evidence_ids = verdict.evidence_ids
                target_gaps.append(
                    TargetGap(
                        gap_id=f"GAP-{gap_index:02d}",
                        requirement_id=requirement.requirement_id,
                        capability=requirement.capability,
                        claim_class=verdict.claim_class,
                        suggested_wording=_gap_suggestion(
                            requirement.capability, component
                        ),
                        why_it_matters=(
                            f"the job requirement explicitly values '{component}'"
                        ),
                        evidence_found=evidence_ids[:24],
                        missing_proof=verdict.missing_proof
                        or [
                            (
                                f"no evidence authorizes '{component}'; "
                                "do not state it as a submit-ready fact"
                            )
                        ],
                        actions=list(_GENERIC_APPLICATION_ACTIONS),
                    )
                )
        requirement_maps.append(
            RequirementClaimMap(
                requirement_id=requirement.requirement_id,
                requirement_text=requirement.text,
                capability=requirement.capability,
                components=verdicts,
            )
        )

    return ClaimTruthResult(
        job_description_sha256=hashlib.sha256(job_description.encode("utf-8")).hexdigest(),
        requirement_maps=requirement_maps,
        evidence=graded,
        application_claims=application_claims,
        target_gaps=target_gaps,
        verified_count=verified_count,
        supported_derivation_count=supported_count,
        high_value_gap_count=gap_count,
        unsupported_count=unsupported_count,
        contamination_counts=contamination_counts,
    )


_AUTHORITY_PRIORITY: dict[EvidenceAuthority, int] = {
    EvidenceAuthority.HIGH: 4,
    EvidenceAuthority.MEDIUM: 3,
    EvidenceAuthority.LOW_CONTEXTUAL: 2,
    EvidenceAuthority.NON_SUPPORTING: 1,
}


def _excluded_implications(
    requirement: ResumeRequirementExpansion, vocabulary: Sequence[str]
) -> list[str]:
    vocabulary_set = set(vocabulary)
    excluded = [
        term
        for term in requirement.technology_terms
        if term not in vocabulary_set
    ]
    if requirement.capability == "rag_embedding_retrieval":
        if not vocabulary_set & {"embedding", "embeddings", "embedding-based", "sbert"}:
            excluded.append("embedding generation ownership")
        if not vocabulary_set & {"vertex", "vertex ai", "google cloud", "gcp"}:
            excluded.append("Vertex AI / Google Cloud deployment")
        if not vocabulary_set & {"rerank", "reranking"}:
            excluded.append("reranking implementation")
        if not vocabulary_set & {"grounding", "hallucination", "grounding checks"}:
            excluded.append("grounding evaluation")
    return sorted(set(excluded))


def validate_application_claim(
    claim: ApplicationClaim,
    graded_by_id: dict[str, GradedEvidence],
) -> list[ClaimValidationCode]:
    """Deterministic validator for one submit-ready Application claim."""

    violations: list[ClaimValidationCode] = []
    if claim.claim_class not in {
        ClaimClass.VERIFIED,
        ClaimClass.SUPPORTED_DERIVATION,
    }:
        violations.append(ClaimValidationCode.UNSUPPORTED_CLASS)
    evidence = [graded_by_id[item] for item in claim.evidence_ids]
    if any(item.authority is EvidenceAuthority.NON_SUPPORTING for item in evidence):
        violations.append(ClaimValidationCode.NON_SUPPORTING_EVIDENCE)
    if any(item.contamination is not ContaminationKind.NONE for item in evidence):
        violations.append(ClaimValidationCode.ECHO_CONTAMINATION)
    if claim.strength in {
        ClaimStrength.STRONG,
        ClaimStrength.MODERATE,
        ClaimStrength.BOUNDED,
        ClaimStrength.CONTRIBUTION_SAFE,
    } and all(item.ownership is EvidenceOwnership.UNPROVEN for item in evidence):
        violations.append(ClaimValidationCode.OWNERSHIP_UNPROVEN)
    allowed = set(claim.technology_vocabulary)
    if _technology_inflated(claim.proposed_text, allowed):
        violations.append(ClaimValidationCode.TECHNOLOGY_INFLATION)
    if _NUMBER_RE.search(claim.proposed_text) is not None:
        violations.append(ClaimValidationCode.NEW_NUMBER)
    return violations


def _technology_inflated(text: str, allowed: set[str]) -> bool:
    for term in capability_expansion(text).technology_terms:
        if " " not in term:
            if term not in allowed:
                return True
            continue
        for word in term.split():
            if word in _ALL_LEXICON_TERMS and word not in allowed:
                return True
    return False
