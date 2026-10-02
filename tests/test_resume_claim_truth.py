"""Focused tests for graded resume claim truth and Application/Target separation."""

from __future__ import annotations

import hashlib

from soloscale.resume_claim_truth import (
    build_claim_truth_result,
    classify_evidence,
    propose_claim_text,
    validate_application_claim,
)
from soloscale.resume_models import (
    ApplicationClaim,
    ClaimClass,
    ClaimStrength,
    ClaimValidationCode,
    ContaminationKind,
    EvidenceAuthority,
    EvidenceOwnership,
    GapAction,
    GradedEvidence,
    ResumeEvidenceCoverageMap,
    ResumeRetrievalCandidate,
    ResumeRetrievalCoverage,
    ResumeRetrievalSourceKind,
)
from soloscale.resume_retrieval import normalize_requirements


def _cid(value: int) -> str:
    return hashlib.sha256(f"candidate-{value}".encode()).hexdigest()


def _candidate(
    value: int,
    text: str,
    *,
    source_identity: str = "owned-project",
    source_kind: ResumeRetrievalSourceKind = ResumeRetrievalSourceKind.CODEX,
    signals: list[str] | None = None,
    evidence_type: str = "chunk:assistant",
    provenance: dict[str, str] | None = None,
) -> ResumeRetrievalCandidate:
    return ResumeRetrievalCandidate(
        candidate_id=_cid(value),
        source_kind=source_kind,
        source_identity=source_identity,
        evidence_type=evidence_type,
        authority=f"chunk_sha256:{'a' * 16}",
        text=text,
        signals=signals or [],
        rationale="test candidate",
        score=1.0,
        provenance=provenance or {},
    )


def _coverage_map(
    job_description: str, candidates: list[ResumeRetrievalCandidate]
) -> ResumeEvidenceCoverageMap:
    requirements = normalize_requirements(job_description)
    coverage = [
        ResumeRetrievalCoverage(
            requirement_id=item.requirement_id,
            normalized_capability=item.capability,
        )
        for item in requirements
    ]
    return ResumeEvidenceCoverageMap(
        job_description_sha256=hashlib.sha256(job_description.encode()).hexdigest(),
        requirements=requirements,
        coverage=coverage,
        candidates=candidates,
        sources=[],
        retrieved_count=len(candidates),
        kept_count=len(candidates),
        irrelevant_count=0,
    )


_FAISS_CHROMA_TEXT = (
    "app/memory/vector_store.py: def _faiss_add(self, chunks): self._faiss_add(...); "
    "def _chroma_add(self, chunks): self._chroma_add(...); similarity search over vectors"
)


def test_direct_verified_claim() -> None:
    candidates = [
        _candidate(1, _FAISS_CHROMA_TEXT, signals=["faiss", "chroma", "similarity"])
    ]
    result = build_claim_truth_result(
        job_description="Implement FAISS vector search.",
        coverage_map=_coverage_map("Implement FAISS vector search.", candidates),
        owned_project_markers=("owned-project",),
    )
    verified = [
        claim for claim in result.application_claims if claim.claim_class is ClaimClass.VERIFIED
    ]
    assert verified
    assert verified[0].strength is ClaimStrength.STRONG
    assert verified[0].proposed_text == (
        "Implemented a FAISS/Chroma vector store for document and query vectors."
    )


def test_valid_supported_derivation_faiss_chroma() -> None:
    candidates = [
        _candidate(
            1,
            _FAISS_CHROMA_TEXT,
            signals=["faiss", "chroma", "vector", "similarity"],
        )
    ]
    result = build_claim_truth_result(
        job_description="We need engineers who can build embedding-based semantic search systems.",
        coverage_map=_coverage_map(
            "We need engineers who can build embedding-based semantic search systems.",
            candidates,
        ),
        owned_project_markers=("owned-project",),
    )
    supported = [
        claim
        for claim in result.application_claims
        if claim.claim_class is ClaimClass.SUPPORTED_DERIVATION
    ]
    assert supported
    wording = {
        "Built a RAG retrieval layer using FAISS and Chroma for vector similarity search."
    }
    assert any(claim.proposed_text in wording for claim in supported)
    graded = {item.evidence_id: item for item in result.evidence}
    assert any(validate_application_claim(claim, graded) == [] for claim in supported)


def test_invalid_derivation_adds_new_technology() -> None:
    claim = ApplicationClaim(
        claim_id="CLAIM-01",
        requirement_id="REQ-01",
        claim_class=ClaimClass.SUPPORTED_DERIVATION,
        strength=ClaimStrength.MODERATE,
        proposed_text="Built semantic retrieval using Pinecone.",
        evidence_ids=[_cid(1)],
        authority=EvidenceAuthority.HIGH,
        ownership=EvidenceOwnership.PROVEN,
        derivation_rationale="abstraction over vector evidence",
        truth_boundary="supported",
        technology_vocabulary=["faiss", "vector", "similarity"],
    )
    graded = {
        _cid(1): GradedEvidence(
            evidence_id=_cid(1),
            source_kind=ResumeRetrievalSourceKind.CODEX,
            authority=EvidenceAuthority.HIGH,
            ownership=EvidenceOwnership.PROVEN,
            contamination=ContaminationKind.NONE,
            capability_terms=["faiss", "vector", "similarity"],
            is_implementation=True,
        )
    }
    assert ClaimValidationCode.TECHNOLOGY_INFLATION in validate_application_claim(
        claim, graded
    )


def test_ownership_unproven_downgrades_strength() -> None:
    candidates = [
        _candidate(1, _FAISS_CHROMA_TEXT, signals=["faiss", "chroma", "vector", "similarity"])
    ]
    graded, _ = classify_evidence(
        candidates,
        requirement_texts=["Implement FAISS vector search."],
        owned_project_markers=("somebody-else",),
    )
    assert graded[0].ownership is EvidenceOwnership.UNPROVEN
    claim = ApplicationClaim(
        claim_id="CLAIM-01",
        requirement_id="REQ-01",
        claim_class=ClaimClass.SUPPORTED_DERIVATION,
        strength=ClaimStrength.MODERATE,
        proposed_text=(
            "Built a RAG retrieval layer using FAISS and Chroma for vector "
            "similarity search."
        ),
        evidence_ids=[_cid(1)],
        authority=EvidenceAuthority.HIGH,
        ownership=EvidenceOwnership.UNPROVEN,
        derivation_rationale="abstraction over vector evidence",
        truth_boundary="supported",
        technology_vocabulary=["faiss", "chroma", "vector", "similarity", "retrieval"],
    )
    assert ClaimValidationCode.OWNERSHIP_UNPROVEN in validate_application_claim(
        claim, {item.evidence_id: item for item in graded}
    )


def test_jd_echo_and_course_and_suggestion_contamination() -> None:
    requirement = "Design and implement RAG pipelines (chunking, embeddings, retrieval)."
    echo = _candidate(1, requirement, source_identity="session")
    course = _candidate(
        2,
        "In this course unit we learn embeddings and vector retrieval.",
        source_identity="course-transcript",
    )
    suggestion = _candidate(
        3,
        "简历里建议你把 FAISS 和 RAG 写进 SoloScale bullet。",
        source_identity="session",
    )
    graded, counts = classify_evidence(
        [echo, course, suggestion], requirement_texts=[requirement]
    )
    by_id = {item.evidence_id: item for item in graded}
    assert by_id[_cid(1)].contamination is ContaminationKind.JD_QUERY_ECHO
    assert by_id[_cid(1)].authority is EvidenceAuthority.NON_SUPPORTING
    assert by_id[_cid(2)].contamination is ContaminationKind.COURSE_EXPOSURE
    assert by_id[_cid(3)].contamination is ContaminationKind.MODEL_SUGGESTION
    assert counts[ContaminationKind.JD_QUERY_ECHO] == 1


def test_metric_inflation_rejected() -> None:
    claim = ApplicationClaim(
        claim_id="CLAIM-01",
        requirement_id="REQ-01",
        claim_class=ClaimClass.VERIFIED,
        strength=ClaimStrength.STRONG,
        proposed_text="Improved retrieval latency by 40%.",
        evidence_ids=[_cid(1)],
        authority=EvidenceAuthority.HIGH,
        ownership=EvidenceOwnership.PROVEN,
        derivation_rationale="exact implementation",
        truth_boundary="verified",
        technology_vocabulary=["retrieval"],
    )
    graded = {
        _cid(1): GradedEvidence(
            evidence_id=_cid(1),
            source_kind=ResumeRetrievalSourceKind.CODEX,
            authority=EvidenceAuthority.HIGH,
            ownership=EvidenceOwnership.PROVEN,
            contamination=ContaminationKind.NONE,
            capability_terms=["retrieval"],
            is_implementation=True,
        )
    }
    assert ClaimValidationCode.NEW_NUMBER in validate_application_claim(claim, graded)


def test_application_excludes_gaps_and_target_labels_them() -> None:
    candidates = [
        _candidate(
            1,
            _FAISS_CHROMA_TEXT,
            signals=["faiss", "chroma", "vector", "similarity"],
        )
    ]
    result = build_claim_truth_result(
        job_description=(
            "Design and implement RAG pipelines on Google Cloud / Vertex AI (chunking, "
            "embeddings, indexing, retrieval, reranking, grounding)."
        ),
        coverage_map=_coverage_map(
            (
                "Design and implement RAG pipelines on Google Cloud / Vertex AI (chunking, "
                "embeddings, indexing, retrieval, reranking, grounding)."
            ),
            candidates,
        ),
        owned_project_markers=("owned-project",),
    )
    assert result.application_claims
    assert all(
        claim.claim_class
        in {ClaimClass.VERIFIED, ClaimClass.SUPPORTED_DERIVATION}
        for claim in result.application_claims
    )
    assert result.target_gaps
    assert all(
        gap.claim_class in {ClaimClass.HIGH_VALUE_GAP, ClaimClass.UNSUPPORTED}
        for gap in result.target_gaps
    )
    assert all(GapAction.LEARNING_CASE in gap.actions for gap in result.target_gaps)
    vertex = next(
        gap
        for gap in result.target_gaps
        if "Vertex" in gap.suggested_wording
    )
    assert vertex.suggested_wording == (
        "Deployed RAG pipelines on Google Cloud / Vertex AI with reranking "
        "and grounding evaluation."
    )


def test_unsupported_technology_not_inferred_in_application() -> None:
    candidates = [
        _candidate(
            1,
            _FAISS_CHROMA_TEXT,
            signals=["faiss", "chroma", "vector", "similarity"],
        )
    ]
    result = build_claim_truth_result(
        job_description=(
            "Design and implement RAG pipelines on Google Cloud / Vertex AI (chunking, "
            "embeddings, indexing, retrieval, reranking, grounding)."
        ),
        coverage_map=_coverage_map(
            (
                "Design and implement RAG pipelines on Google Cloud / Vertex AI (chunking, "
                "embeddings, indexing, retrieval, reranking, grounding)."
            ),
            candidates,
        ),
        owned_project_markers=("owned-project",),
    )
    combined = " ".join(claim.proposed_text for claim in result.application_claims).casefold()
    assert "vertex" not in combined
    assert "reranking" not in combined
    assert "grounding" not in combined
    assert "embedding generation" not in combined
    components = {
        item.component
        for requirement in result.requirement_maps
        for item in requirement.components
    }
    assert "embedding generation" in components
    assert "Vertex AI / Google Cloud RAG" in components
    assert "reranking" in components


def test_jd_echo_never_supports_a_claim() -> None:
    requirement = "Design and implement RAG pipelines with embeddings."
    candidates = [
        _candidate(1, requirement, source_identity="session", signals=["embeddings"]),
        _candidate(
            2,
            _FAISS_CHROMA_TEXT,
            signals=["faiss", "chroma", "vector", "similarity"],
        ),
    ]
    result = build_claim_truth_result(
        job_description=requirement,
        coverage_map=_coverage_map(requirement, candidates),
        owned_project_markers=("owned-project",),
    )
    for claim in result.application_claims:
        assert _cid(1) not in claim.evidence_ids


def test_propose_claim_text_strength_ladder() -> None:
    verified = propose_claim_text(
        "rag_embedding_retrieval",
        "FAISS/Chroma vector-store implementation",
        {"faiss", "chroma", "vector", "similarity"},
        ClaimClass.VERIFIED,
    )
    assert verified == "Implemented a FAISS/Chroma vector store for document and query vectors."
    supported = propose_claim_text(
        "rag_embedding_retrieval",
        "vector / similarity retrieval",
        {"faiss", "chroma", "vector", "similarity"},
        ClaimClass.SUPPORTED_DERIVATION,
    )
    assert supported == (
        "Built a RAG retrieval layer using FAISS and Chroma for vector similarity search."
    )
