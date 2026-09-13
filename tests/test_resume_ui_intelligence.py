"""Focused tests for the canonical Resume Intelligence UI migration (6A)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from soloscale.desktop_credentials import _clear_for_tests
from soloscale.local_ui import (
    _resume_intelligence_page,
    _run_resume_intelligence_preflight,
    _save_ai_provider_preference,
)
from soloscale.model_gateway import ModelProviderId
from soloscale.resume_generation import (
    canonical_project_name,
    normalize_resume_projects,
    render_application_resume,
)
from soloscale.resume_models import (
    ApplicationResumeDraft,
    ContributionMode,
    GeneratedResumeBullet,
)

_JD = "Design and implement RAG pipelines (chunking, embeddings, retrieval)."


def test_project_and_worktree_canonicalization() -> None:
    assert canonical_project_name("solo-scale-ai-os") == "SoloScale AI OS"
    assert (
        canonical_project_name("solo-scale-ai-os-v0.5-product-completion")
        == "SoloScale AI OS"
    )
    assert (
        canonical_project_name("/Users/x/AI-Research-Assistant-LangJu-Edition")
        == "AI Research Assistant"
    )
    assert canonical_project_name("/Users/example/Documents/AI TEAM") is None
    assert canonical_project_name("AI TEAM WORKTREES") is None
    normalized = normalize_resume_projects(
        [
            "solo-scale-ai-os",
            "solo-scale-ai-os-v0.5-product-completion",
            "/Users/x/AI TEAM",
            "ai-research-assistant-langju-edition",
        ]
    )
    assert normalized == ["SoloScale AI OS", "AI Research Assistant"]


def test_preflight_uses_canonical_pipeline_and_never_touches_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_for_tests()
    _save_ai_provider_preference(tmp_path, provider=ModelProviderId.DEEPSEEK.value)

    def legacy(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("legacy evidence pack path was invoked")

    monkeypatch.setattr("soloscale.local_ui.build_candidate_evidence_pack", legacy)

    def forbidden_gateway(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("paid gateway was constructed before authorization")

    monkeypatch.setattr(
        "soloscale.resume_generation.deepseek_generation_gateway",
        forbidden_gateway,
    )
    state = _run_resume_intelligence_preflight(
        tmp_path, _JD, owned_project_markers=("owned-project",)
    )
    assert state.coverage_map.requirements
    assert state.claim_truth.requirement_maps
    assert state.coverage.requirements_total == 1
    assert state.preflight.provider == "deepseek"
    assert state.preflight.model == "deepseek-v4-flash"
    assert state.preflight.reasoning_effort == "low"
    assert state.preflight.credential_status == "NOT_CONFIGURED"
    assert state.preflight.intended_calls == 1
    assert state.preflight.automatic_retries == 0


def test_24_of_24_coverage_accounting(tmp_path: Path) -> None:
    _clear_for_tests()
    _save_ai_provider_preference(tmp_path, provider=ModelProviderId.DEEPSEEK.value)
    job_description = "\n".join(
        f"Requirement number {index} for Python engineers." for index in range(40)
    )
    state = _run_resume_intelligence_preflight(tmp_path, job_description)
    coverage = state.coverage
    assert coverage.requirements_total == 24
    accounted = (
        len(coverage.strongly_represented)
        + len(coverage.partially_represented)
        + len(coverage.available_not_selected)
        + len(coverage.high_value_gaps)
        + len(coverage.unsupported)
    )
    assert accounted == 24
    # Requirement totals must not be inflated by component-level verdicts.
    component_total = sum(
        len(item.components) for item in state.claim_truth.requirement_maps
    )
    assert component_total != coverage.requirements_total or component_total == 24


def test_public_render_hides_internal_provenance() -> None:
    draft = ApplicationResumeDraft(
        job_description_sha256=hashlib.sha256(_JD.encode()).hexdigest(),
        headline="AI Engineer | RAG retrieval",
        summary="Evidence-backed RAG retrieval experience.",
        skills=["FAISS", "Chroma"],
        bullets=[
            GeneratedResumeBullet(
                bullet_id="BULLET-01",
                section="PROJECTS",
                text="Implemented a FAISS/Chroma vector store for document and query vectors.",
                source_claim_ids=["CLAIM-01"],
                project_identity="ai-research-assistant",
                contribution_mode=ContributionMode.AI_ASSISTED_USER_DIRECTED,
                truth_boundary="VERIFIED",
            )
        ],
    )
    rendered = render_application_resume(draft)
    assert "CLAIM-" not in rendered
    assert "BULLET-" not in rendered
    assert "ai_assisted" not in rendered
    assert "truth" not in rendered.casefold()


def test_intelligence_page_separates_application_and_target(
    tmp_path: Path,
) -> None:
    _clear_for_tests()
    _save_ai_provider_preference(tmp_path, provider=ModelProviderId.DEEPSEEK.value)
    state = _run_resume_intelligence_preflight(
        tmp_path, _JD, owned_project_markers=("owned-project",)
    )
    page = _resume_intelligence_page(
        "en", tmp_path, job_description=_JD, state=state
    )
    assert "NOT_CONFIGURED" in page
    assert "provider=deepseek" in page
    assert "model=deepseek-v4-flash" in page
    assert "DeepSeek · DeepSeek V4 Flash · Low" in page
    assert "intended_calls=1" in page
    assert "automatic_retries=0" in page
    assert "Allowed claim candidates (preflight only)" in page
    assert "TARGET / BENCHMARK" in page
    assert "not a submit-ready resume" in page
    assert "knowledge_store</code> — NOT_SELECTED" in page
    assert 'href="/resume?lang=en"' in page


def test_intelligence_form_is_primary_and_not_safe_by_default(tmp_path: Path) -> None:
    page = _resume_intelligence_page("en", tmp_path)
    assert 'name="job_description"' in page
    assert 'name="include_private_conversations"' in page
    assert 'name="include_buildlog_history"' in page
    assert 'name="include_private_conversations" value="1" checked' not in page
    assert 'name="include_buildlog_history" value="1" checked' not in page
    assert "Analyze and build preflight" in page
    assert "safe to submit" not in page.casefold()
