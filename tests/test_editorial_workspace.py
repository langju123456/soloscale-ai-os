import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from soloscale.editorial_models import (
    AuthorVoiceProfile,
    EditorialProvenance,
    EditorialRole,
    ProviderIdentity,
    ProviderKind,
    ReviewResult,
    RevisionResult,
)
from soloscale.editorial_pipeline import PrivateWriteError, make_provenance
from soloscale.editorial_workspace import (
    EditorialArtifacts,
    EditorialPackage,
    EvidenceAnchor,
    finalize_post_revision_review,
    verify_editorial_package,
    write_author_voice_profile,
    write_editorial_package,
)
from soloscale.visual_planner import VisualBrief, VisualType, plan_visual


def _provenance(
    role: EditorialRole, output_artifacts: dict[str, str]
) -> EditorialProvenance:
    return make_provenance(
        role=role,
        provider=ProviderIdentity(
            kind=ProviderKind.CODEX_SESSION,
            provider="codex",
            model="gpt-5.6-sol",
        ),
        prompt_version="editorial-v1",
        input_artifacts={"input": "grounded input"},
        output_artifacts=output_artifacts,
        reasoning="max",
        network_used=False,
        cost_usd=0,
    )


def test_editorial_package_is_private_traceable_and_non_overwriting(tmp_path: Path) -> None:
    artifacts = EditorialArtifacts(
        canonical_story="I built the workflow. I have not proved audience value.",
        linkedin="I built the workflow. I have not proved audience value.",
        x_thread=["1/1 I built the workflow. I have not proved audience value."],
        x_post="I built the workflow. I have not proved audience value.",
    )
    final_outputs = {
        "canonical-story.md": artifacts.canonical_story.rstrip() + "\n",
        "linkedin.md": artifacts.linkedin.rstrip() + "\n",
        "x-thread.md": "\n\n".join(artifacts.x_thread) + "\n",
        "x-post.md": artifacts.x_post.rstrip() + "\n",
    }
    writer = _provenance(EditorialRole.WRITER, {"canonical-story.md": "first draft"})
    reviewer_receipt = _provenance(
        EditorialRole.REVIEWER, {"structured-review.json": "review"}
    )
    reviser_receipt = _provenance(EditorialRole.REVISER, final_outputs)
    package = EditorialPackage(
        package_id="day-01-grounded-story",
        day=1,
        status="READY_FOR_HUMAN_PUBLICATION",
        topic="A grounded story",
        audience="Solo builders",
        author_voice_profile_id="solo-builder-peer-v1",
        evidence_manifest=[
            EvidenceAnchor(
                evidence_id="git:abc123",
                source_label="Git receipt",
                factual_boundary="This proves code exists, not external value.",
            )
        ],
        artifacts=artifacts,
        visual_plan=plan_visual(
            VisualBrief(
                visual_type=VisualType.INSIGHT_CARD,
                single_idea="Software completion is not external validation.",
                title="Built is not validated",
                exact_labels=["Built", "Not externally validated"],
                layout="Two stacked statements.",
                visual_hierarchy=["Built", "Boundary"],
                alt_text="A card contrasting built software with missing external validation.",
                unsupported_information_check="No external outcome is claimed.",
            )
        ),
        writer=writer,
        reviewer=ReviewResult(
            overall_verdict="Ready for human review",
            publication_recommendation="Human fact-check before publication.",
            provenance=reviewer_receipt,
        ),
        revision=RevisionResult(decisions=[], provenance=reviser_receipt),
    )
    root = tmp_path / "day-01"
    hashes = write_editorial_package(root, package, try_png=False)
    assert "receipt.json" in hashes
    assert verify_editorial_package(root) is True
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in root.rglob("*") if path.is_file())
    with pytest.raises(PrivateWriteError):
        write_editorial_package(root, package, try_png=False)

    voice = AuthorVoiceProfile(
        profile_id="solo-builder-peer",
        version="v1",
        voice_traits=["Concrete and first-person"],
        updated_at=datetime.now(UTC),
    )
    assert write_author_voice_profile(tmp_path / "voices", voice).is_file()


def test_editorial_package_rejects_overlength_x_post() -> None:
    with pytest.raises(ValueError, match="280"):
        EditorialArtifacts(
            canonical_story="Grounded story",
            linkedin="Grounded story",
            x_thread=["short"],
            x_post="x" * 281,
        )


def test_post_revision_review_finalizes_without_rewriting_week_receipt(tmp_path: Path) -> None:
    batch = tmp_path / "week"
    (batch / "day-01").mkdir(parents=True)
    week_receipt = batch / "week-receipt.json"
    week_receipt.write_text('{"publication_performed":false}\n', encoding="utf-8")
    review = tmp_path / "review.json"
    review.write_text(
        json.dumps(
            {
                "overall_verdict": "READY_FOR_HUMAN_PUBLICATION",
                "material_findings": [],
                "invocation": {
                    "role": "reviewer",
                    "provider": "codex_session",
                    "model": "gpt-5.6-sol",
                    "reasoning": "max",
                    "prompt_version": "editorial-post-revision-review-v1",
                    "fresh_context": True,
                    "network_used": False,
                    "cost_usd": 0,
                    "status": "completed",
                    "errors": [],
                },
            }
        ),
        encoding="utf-8",
    )

    original = week_receipt.read_bytes()
    finalize_post_revision_review(batch_root=batch, review_path=review)
    assert week_receipt.read_bytes() == original
    assert (batch / "day-01" / "post-revision-review.json").is_file()
    receipt = json.loads((batch / "final-validation-receipt.json").read_text())
    assert receipt["publication_performed"] is False
