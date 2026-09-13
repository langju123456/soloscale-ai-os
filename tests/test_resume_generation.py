"""Focused tests for the canonical evidence-intelligence Resume generation path."""

from __future__ import annotations

import hashlib
import json
from typing import TypeVar, cast

import pytest
from pydantic import BaseModel

from soloscale.model_gateway import (
    DeepSeekResponsesRequest,
    ModelGatewayTransportError,
    ModelProviderId,
    model_gateway_for,
)
from soloscale.resume_claim_truth import build_claim_truth_result
from soloscale.resume_generation import (
    GenerationProviderError,
    apply_editorial_review,
    deepseek_generation_gateway,
    generate_application_resume,
    select_allowed_claims,
    validate_application_draft,
)
from soloscale.resume_models import (
    ApplicationClaim,
    ApplicationResumeDraft,
    ClaimClass,
    ClaimStrength,
    ClaimTruthResult,
    ContaminationKind,
    ContributionMode,
    EvidenceAuthority,
    EvidenceOwnership,
    GeneratedResumeBullet,
    GenerationViolationCode,
    GradedEvidence,
    ResumeEvidenceCoverageMap,
    ResumeGenerationResult,
    ResumeRetrievalCandidate,
    ResumeRetrievalCoverage,
    ResumeRetrievalSourceKind,
)
from soloscale.resume_retrieval import normalize_requirements

_JD = (
    "Design and implement RAG pipelines on Google Cloud / Vertex AI (chunking, "
    "embeddings, indexing, retrieval, reranking, grounding)."
)
_FAISS_TEXT = (
    "app/memory/vector_store.py: def _faiss_add(self, chunks): self._faiss_add(...); "
    "def _chroma_add(self, chunks): self._chroma_add(...); similarity search over vectors"
)


def test_generated_payload_limits_match_application_draft_limits() -> None:
    from pydantic import ValidationError

    from soloscale.resume_generation import _GeneratedBulletPayload, _GeneratedResumePayload
    from soloscale.resume_models import ApplicationResumeDraft, GeneratedResumeBullet

    claim_truth = _claim_truth()
    claim_id = _claim_ids(claim_truth)[0]
    bullet = {
        "section": "PROJECTS",
        "text": "x" * 600,
        "source_claim_ids": [claim_id],
        "contribution_mode": "ai_assisted_user_directed",
    }
    assert _GeneratedBulletPayload.model_validate(bullet).text == "x" * 600
    with pytest.raises(ValidationError):
        _GeneratedBulletPayload.model_validate({**bullet, "text": "x" * 601})
    payload = {
        "headline": "Engineer",
        "summary": "y" * 800,
        "skills": ["Python"],
        "bullets": [bullet],
    }
    assert _GeneratedResumePayload.model_validate(payload).summary == "y" * 800
    with pytest.raises(ValidationError):
        _GeneratedResumePayload.model_validate({**payload, "summary": "y" * 801})
    draft = ApplicationResumeDraft(
        job_description_sha256=hashlib.sha256(_JD.encode()).hexdigest(),
        headline="Engineer",
        summary="y" * 800,
        skills=["Python"],
        bullets=[GeneratedResumeBullet(
            bullet_id="BULLET-01", section="PROJECTS", text="x" * 600,
            source_claim_ids=[claim_id],
            contribution_mode=ContributionMode.AI_ASSISTED_USER_DIRECTED,
            truth_boundary="VERIFIED",
        )],
    )
    assert draft.summary == "y" * 800
    with pytest.raises(ValidationError):
        ApplicationResumeDraft.model_validate({**draft.model_dump(), "summary": "y" * 801})

def _cid(value: int) -> str:
    return hashlib.sha256(f"candidate-{value}".encode()).hexdigest()


def _candidate(signals: list[str]) -> ResumeRetrievalCandidate:
    return ResumeRetrievalCandidate(
        candidate_id=_cid(1),
        source_kind=ResumeRetrievalSourceKind.CODEX,
        source_identity="ai-research-assistant",
        evidence_type="chunk:assistant",
        authority=f"chunk_sha256:{'a' * 16}",
        text=_FAISS_TEXT,
        signals=signals,
        rationale="test candidate",
        score=1.0,
        provenance={"document_sha256": "b" * 64},
    )


def _coverage_map() -> ResumeEvidenceCoverageMap:
    requirements = normalize_requirements(_JD)
    return ResumeEvidenceCoverageMap(
        job_description_sha256=hashlib.sha256(_JD.encode()).hexdigest(),
        requirements=requirements,
        coverage=[
            ResumeRetrievalCoverage(
                requirement_id=item.requirement_id,
                normalized_capability=item.capability,
            )
            for item in requirements
        ],
        candidates=[_candidate(["faiss", "chroma", "vector", "similarity", "retrieval"])],
        sources=[],
        retrieved_count=1,
        kept_count=1,
        irrelevant_count=0,
    )


def _claim_truth() -> ClaimTruthResult:
    return build_claim_truth_result(
        job_description=_JD,
        coverage_map=_coverage_map(),
        owned_project_markers=("ai-research-assistant",),
    )


def _claim_ids(claim_truth: ClaimTruthResult) -> list[str]:
    return [claim.claim_id for claim in claim_truth.application_claims]


def _good_payload(claim_ids: list[str]) -> dict[str, object]:
    verified = claim_ids[0]
    supported = claim_ids[1] if len(claim_ids) > 1 else claim_ids[0]
    return {
        "headline": "AI Engineer | RAG retrieval",
        "summary": "AI engineer with evidence-backed RAG retrieval experience.",
        "skills": ["FAISS", "Chroma", "vector similarity search"],
        "bullets": [
            {
                "section": "PROJECTS",
                "text": "Implemented a FAISS/Chroma vector store for document and query vectors.",
                "source_claim_ids": [verified],
                "project_identity": "ai-research-assistant",
                "contribution_mode": "ai_assisted_user_directed",
            },
            {
                "section": "PROJECTS",
                "text": (
                    "Built a RAG retrieval layer using FAISS and Chroma for vector "
                    "similarity search."
                ),
                "source_claim_ids": [supported],
                "project_identity": "ai-research-assistant",
                "contribution_mode": "ai_assisted_user_directed",
            },
        ],
    }


def _payload_bullets(payload: dict[str, object]) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], payload["bullets"])


ResponseT = TypeVar("ResponseT", bound=BaseModel)


class _FakeGateway:
    def __init__(self, payload: dict[str, object], *, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[dict[str, object]] = []
        self.last_receipt = None

    def complete(
        self,
        schema: type[ResponseT],
        *,
        system: str,
        user: str,
        reasoning_effort: object | None = None,
        thinking_enabled: bool | None = None,
    ) -> ResponseT:
        self.calls.append(
            {
                "system": system,
                "user": user,
                "reasoning_effort": reasoning_effort,
                "thinking_enabled": thinking_enabled,
            }
        )
        if self.error is not None:
            raise self.error
        return schema.model_validate(self.payload)


def _generate(
    payload: dict[str, object],
) -> tuple[ResumeGenerationResult, _FakeGateway]:
    gateway = _FakeGateway(payload)
    result = generate_application_resume(
        job_description=_JD,
        coverage_map=_coverage_map(),
        gateway=gateway,
        owned_project_markers=("ai-research-assistant",),
    )
    return result, gateway


def test_generation_consumes_retrieval_and_claim_truth() -> None:
    claim_truth = _claim_truth()
    payload = _good_payload(_claim_ids(claim_truth))
    result, gateway = _generate(payload)
    assert result.validation_report.valid is True
    assert result.application_draft.bullets
    assert "retrieval" in result.role_strategy.positioning
    assert "allowed_claims" in str(gateway.calls[0]["user"])
    assert result.application_draft.headline == payload["headline"]
    assert result.application_draft.summary == payload["summary"]
    for bullet in result.application_draft.bullets:
        assert bullet.source_claim_ids
        assert bullet.bullet_id.startswith("BULLET-")
        assert bullet.generation_status in {"MODEL_GENERATED", "DETERMINISTIC_REPAIR"}


def test_gaps_and_unsupported_technology_excluded_from_application() -> None:
    claim_truth = _claim_truth()
    result, _gateway = _generate(_good_payload(_claim_ids(claim_truth)))
    combined = (
        result.application_draft.headline
        + result.application_draft.summary
        + " ".join(result.application_draft.skills)
        + " ".join(item.text for item in result.application_draft.bullets)
    ).casefold()
    assert "vertex" not in combined
    assert "reranking" not in combined
    assert "grounding" not in combined
    assert "embedding generation" not in combined
    assert result.target_gaps


def test_unsupported_skill_is_filtered() -> None:
    claim_truth = _claim_truth()
    payload = _good_payload(_claim_ids(claim_truth))
    payload["skills"] = ["FAISS", "Vertex AI", "reranking"]
    result, _gateway = _generate(payload)
    assert "Vertex AI" in result.validation_report.rejected_skills
    assert "reranking" in result.validation_report.rejected_skills
    assert "Vertex AI" not in result.application_draft.skills
    assert "reranking" not in result.application_draft.skills


def test_invented_metric_is_repaired_away() -> None:
    claim_truth = _claim_truth()
    payload = _good_payload(_claim_ids(claim_truth))
    _payload_bullets(payload)[0]["text"] = "Improved retrieval latency by 40%."
    result, _gateway = _generate(payload)
    codes = {violation.rule_code for violation in result.validation_report.violations}
    assert GenerationViolationCode.INVENTED_METRIC in codes
    final_text = " ".join(item.text for item in result.application_draft.bullets)
    assert "40%" not in final_text
    assert result.validation_report.valid is True


def test_authorized_metric_is_preserved() -> None:
    claim_truth = _claim_truth()
    source = claim_truth.application_claims[0].model_copy(
        update={"proposed_text": "Validated FAISS retrieval with 563 passing tests."}
    )
    claim_truth = claim_truth.model_copy(
        update={"application_claims": [source, *claim_truth.application_claims[1:]]}
    )
    draft = ApplicationResumeDraft(
        job_description_sha256=hashlib.sha256(_JD.encode()).hexdigest(),
        headline="AI Engineer | RAG retrieval",
        summary="Evidence-backed RAG retrieval experience.",
        skills=["FAISS"],
        bullets=[
            GeneratedResumeBullet(
                bullet_id="BULLET-01",
                section="PROJECTS",
                text="Validated FAISS retrieval with 563 passing tests.",
                source_claim_ids=[source.claim_id],
                contribution_mode=ContributionMode.AI_ASSISTED_USER_DIRECTED,
                truth_boundary=source.truth_boundary,
            )
        ],
    )

    validated, report = validate_application_draft(draft, claim_truth)

    assert validated.bullets[0].text.endswith("563 passing tests.")
    assert GenerationViolationCode.INVENTED_METRIC not in {
        violation.rule_code for violation in report.violations
    }


def test_headline_and_summary_are_repaired_through_the_truth_gate() -> None:
    claim_truth = _claim_truth()
    payload = _good_payload(_claim_ids(claim_truth))
    payload["headline"] = "Production Vertex AI Engineer serving 1M users"
    payload["summary"] = (
        "Built and deployed a production Vertex AI platform serving 1M users."
    )

    result, _gateway = _generate(payload)

    combined = (
        result.application_draft.headline + " " + result.application_draft.summary
    ).casefold()
    assert "vertex" not in combined
    assert "1m" not in combined
    assert "production" not in combined
    assert {violation.field_name for violation in result.validation_report.violations} >= {
        "headline",
        "summary",
    }


def test_model_added_technology_is_rejected() -> None:
    claim_truth = _claim_truth()
    payload = _good_payload(_claim_ids(claim_truth))
    _payload_bullets(payload)[1]["text"] = "Built semantic retrieval using Pinecone."
    result, _gateway = _generate(payload)
    codes = {violation.rule_code for violation in result.validation_report.violations}
    assert GenerationViolationCode.UNAUTHORIZED_TECHNOLOGY in codes
    final_text = " ".join(item.text for item in result.application_draft.bullets)
    assert "Pinecone" not in final_text


def test_adversarial_vertex_reranking_claim_is_destroyed() -> None:
    claim_truth = _claim_truth()
    payload = _good_payload(_claim_ids(claim_truth))
    unsafe = "Built production Vertex AI embedding and reranking pipelines serving 100K users."
    payload["bullets"] = [
        {
            "section": "PROJECTS",
            "text": unsafe,
            "source_claim_ids": [_claim_ids(claim_truth)[0]],
            "project_identity": "ai-research-assistant",
            "contribution_mode": "ai_assisted_user_directed",
        }
    ]
    result, _gateway = _generate(payload)
    codes = {violation.rule_code for violation in result.validation_report.violations}
    assert GenerationViolationCode.UNAUTHORIZED_TECHNOLOGY in codes
    assert GenerationViolationCode.INVENTED_METRIC in codes
    assert GenerationViolationCode.SCALE_INFLATION in codes
    assert GenerationViolationCode.DEPLOYMENT_INFLATION in codes
    final_text = " ".join(item.text for item in result.application_draft.bullets)
    assert unsafe not in final_text
    assert "Vertex" not in final_text
    assert "100K" not in final_text
    assert result.validation_report.valid is True


def test_strong_verified_wording_is_not_weakened() -> None:
    claim_truth = _claim_truth()
    payload = _good_payload(_claim_ids(claim_truth))
    _payload_bullets(payload)[0]["text"] = "Familiar with FAISS and Chroma."
    result, _gateway = _generate(payload)
    codes = {violation.rule_code for violation in result.validation_report.violations}
    assert GenerationViolationCode.WEAKENED_CLAIM in codes
    final_text = " ".join(item.text for item in result.application_draft.bullets)
    assert "Implemented a FAISS/Chroma vector store" in final_text


def test_contribution_mode_wording_boundary() -> None:
    claim_truth = _claim_truth()
    payload = _good_payload(_claim_ids(claim_truth))
    _payload_bullets(payload)[0]["contribution_mode"] = "external_unknown"
    result, _gateway = _generate(payload)
    codes = {violation.rule_code for violation in result.validation_report.violations}
    assert GenerationViolationCode.CONTRIBUTION_MODE_INFLATION in codes
    assert result.validation_report.valid is True


def test_editorial_review_cannot_bypass_truth_gate() -> None:
    claim_truth = _claim_truth()
    result, _gateway = _generate(_good_payload(_claim_ids(claim_truth)))
    draft, report = apply_editorial_review(
        result.application_draft,
        claim_truth,
        editorial=lambda text: f"{text} Deployed on Vertex AI.",
    )
    assert report.post_editorial_validated is True
    final_text = " ".join(item.text for item in draft.bullets)
    assert "Deployed on Vertex AI" not in final_text
    assert report.valid is True


def test_receipt_records_actual_provider_and_model() -> None:
    claim_truth = _claim_truth()
    payload_json = json.dumps(_good_payload(_claim_ids(claim_truth)))

    class Transport:
        def __init__(self) -> None:
            self.requests: list[DeepSeekResponsesRequest] = []

        def send(self, request: DeepSeekResponsesRequest) -> str:
            self.requests.append(request)
            return payload_json

    transport = Transport()
    gateway = model_gateway_for(
        ModelProviderId.DEEPSEEK,
        model="deepseek-v4-pro",
        deepseek_api_key="synthetic-key",
        deepseek_reasoning_effort="max",
        deepseek_transport=transport,
    )
    result = generate_application_resume(
        job_description=_JD,
        coverage_map=_coverage_map(),
        gateway=deepseek_generation_gateway(gateway),
        owned_project_markers=("ai-research-assistant",),
    )
    receipt = result.generation_receipt
    assert receipt.provider == "deepseek"
    assert receipt.model == "deepseek-v4-pro"
    assert receipt.reasoning_effort == "max"
    assert receipt.cache_tokens is None
    assert receipt.real_call is False
    assert len(transport.requests) == 1
    request = transport.requests[0]
    schema = request.response_json_schema
    properties = cast(dict[str, object], schema["properties"])
    summary = cast(dict[str, object], properties["summary"])
    bullets = cast(dict[str, object], properties["bullets"])
    items = cast(dict[str, object], bullets["items"])
    bullet_ref = cast(str, items["$ref"])
    assert cast(int, summary["maxLength"]) == 800
    assert bullet_ref == "#/$defs/_GeneratedBulletPayload"
    definitions = cast(dict[str, object], schema["$defs"])
    bullet_definition = cast(dict[str, object], definitions["_GeneratedBulletPayload"])
    bullet_properties = cast(dict[str, object], bullet_definition["properties"])
    bullet_text = cast(dict[str, object], bullet_properties["text"])
    assert cast(int, bullet_text["maxLength"]) == 600
    assert request.reasoning_effort == "max"


def test_no_silent_provider_fallback() -> None:
    gateway = _FakeGateway(
        {},
        error=ModelGatewayTransportError("provider unavailable"),
    )
    with pytest.raises(GenerationProviderError):
        generate_application_resume(
            job_description=_JD,
            coverage_map=_coverage_map(),
            gateway=gateway,
            owned_project_markers=("ai-research-assistant",),
        )
    assert len(gateway.calls) == 1


def test_claim_budget_avoids_redundant_repetition() -> None:
    def claim(claim_id: str, vocabulary: list[str], strength: ClaimStrength) -> ApplicationClaim:
        return ApplicationClaim(
            claim_id=claim_id,
            requirement_id="REQ-01",
            claim_class=ClaimClass.SUPPORTED_DERIVATION,
            strength=strength,
            proposed_text="Built retrieval.",
            evidence_ids=["a" * 64],
            authority=EvidenceAuthority.HIGH,
            ownership=EvidenceOwnership.PROVEN,
            derivation_rationale="supported",
            truth_boundary="supported",
            technology_vocabulary=vocabulary,
        )

    result = ClaimTruthResult(
        job_description_sha256=hashlib.sha256(_JD.encode()).hexdigest(),
        requirement_maps=[],
        target_gaps=[],
        evidence=[
            GradedEvidence(
                evidence_id="a" * 64,
                source_kind=ResumeRetrievalSourceKind.CODEX,
                authority=EvidenceAuthority.HIGH,
                ownership=EvidenceOwnership.PROVEN,
                contamination=ContaminationKind.NONE,
                capability_terms=["faiss", "chroma", "vector", "python", "fastapi"],
                is_implementation=True,
            )
        ],
        application_claims=[
            claim("CLAIM-01", ["faiss", "chroma", "vector"], ClaimStrength.STRONG),
            claim("CLAIM-02", ["faiss", "chroma", "vector"], ClaimStrength.MODERATE),
            claim("CLAIM-03", ["python", "fastapi"], ClaimStrength.MODERATE),
        ],
        verified_count=0,
        supported_derivation_count=3,
        high_value_gap_count=0,
        unsupported_count=0,
    )
    selected = select_allowed_claims(result, max_claims=10)
    assert [item.claim_id for item in selected] == ["CLAIM-01", "CLAIM-03"]


def test_coverage_report_distinguishes_representation() -> None:
    claim_truth = _claim_truth()
    result, _gateway = _generate(_good_payload(_claim_ids(claim_truth)))
    assert result.coverage_report.requirements_total == 1
    assert result.coverage_report.strongly_represented
    assert result.target_gaps
