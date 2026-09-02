from __future__ import annotations

import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from soloscale.editorial_models import (
    EditorialProvenance,
    EditorialRole,
    ProviderIdentity,
    ProviderKind,
    RunStatus,
)
from soloscale.editorial_pipeline import (
    HuggingFaceOpenAIAdapter,
    HuggingFaceOpenAIConfig,
    PrivateWriteError,
    make_provenance,
    write_private_once,
)
from soloscale.visual_planner import (
    VisualBrief,
    VisualType,
    plan_visual,
    render_editable_svg,
    verify_visual_package,
    write_visual_package,
)


def test_provenance_records_private_hashes_and_fresh_context() -> None:
    provenance = make_provenance(
        role=EditorialRole.WRITER,
        provider=ProviderIdentity(kind=ProviderKind.TEMPLATE, provider="local"),
        prompt_version="editorial-v1",
        input_artifacts={"prompt": "private prompt"},
        output_artifacts={"draft": "draft"},
    )
    assert provenance.input_artifact_hashes["prompt"] != "private prompt"
    assert provenance.status is RunStatus.SUCCEEDED
    assert provenance.fresh_context is True


def test_provenance_rejects_success_without_output_hash() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        EditorialProvenance(
            role=EditorialRole.WRITER,
            provider=ProviderIdentity(kind=ProviderKind.TEMPLATE, provider="local"),
            exact_model="deterministic-template-v1",
            prompt_version="v1",
            input_artifact_hashes={"prompt": "a" * 64},
            started_at=now,
            status=RunStatus.SUCCEEDED,
        )


def test_unconfigured_huggingface_adapter_performs_no_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("network must not be contacted")

    monkeypatch.setattr("soloscale.editorial_pipeline.request.urlopen", fail_if_called)
    adapter = HuggingFaceOpenAIAdapter(HuggingFaceOpenAIConfig())
    assert adapter.configured is False
    assert adapter.list_models() == []


def test_visual_labels_render_complete_package_and_private_content_fails_closed(
    tmp_path: Path,
) -> None:
    brief = VisualBrief(
        visual_type=VisualType.PROCESS_FLOW,
        title="Evidence flow",
        single_idea="Review precedes publishing.",
        exact_labels=["Claim ledger", "Human review"],
        layout="A vertical two-step flow.",
        visual_hierarchy=["Claim ledger", "Human review"],
        alt_text="A process flow from claim ledger to human review.",
        unsupported_information_check="No metrics or private content are shown.",
    )
    svg = render_editable_svg(plan_visual(brief))
    assert "Claim ledger" in svg
    hashes = write_visual_package(tmp_path / "visual", plan_visual(brief), try_png=False)
    assert "visual-receipt.json" in hashes
    assert verify_visual_package(tmp_path / "visual") is True
    with pytest.raises(ValidationError):
        VisualBrief(
            visual_type=VisualType.INSIGHT_CARD,
            title="/Users/operator/private.txt",
            single_idea="No public conclusion",
            layout="Centered card.",
            visual_hierarchy=["Title"],
            alt_text="Unsafe",
            unsupported_information_check="No unsupported claims.",
        )


def test_private_writer_uses_permissions_and_never_overwrites(tmp_path: Path) -> None:
    artifact = tmp_path / "run" / "receipt.json"
    write_private_once(artifact, "private")
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert stat.S_IMODE(artifact.parent.stat().st_mode) == 0o700
    with pytest.raises(PrivateWriteError):
        write_private_once(artifact, "replacement")
