from pathlib import Path

import pytest

from soloscale.content_models import ClaimStatus, ContentBrief, ContentClaim
from soloscale.content_workspace import content_run_directory, run_content_workspace
from soloscale.media_quality import (
    MediaQualityChecklist,
    MediaQualityDecision,
    MediaQualityError,
    load_media_quality_review,
    require_approved_media_quality_review,
    save_media_quality_review,
)


def _rendered_run(data_root: Path) -> str:
    run = run_content_workspace(
        data_root=data_root,
        brief=ContentBrief(
            topic="Human media review",
            audience="AI builders",
            language="English",
            call_to_action="Review the finished output.",
            source_label="git:test",
            claims=[
                ContentClaim(
                    id="CLAIM-01",
                    text="A local video package was rendered.",
                    status=ClaimStatus.VERIFIED,
                    receipt="test:render",
                )
            ],
        ),
    )
    run_dir = content_run_directory(data_root, run.run_id)
    for filename in (
        "21_creator_video_youtube.mp4",
        "10_creator_video.mp4",
        "22_creator_video_thumbnail.png",
        "25_creator_video_subtitles.srt",
    ):
        (run_dir / filename).write_bytes(f"artifact:{filename}".encode())
    return run.run_id


def _approved_checklist() -> MediaQualityChecklist:
    return MediaQualityChecklist(
        voice_natural=True,
        pacing_natural=True,
        no_static_visual_too_long=True,
        presenter_adds_value=True,
        language_natural=True,
        claims_evidence_backed=True,
        reference_influenced_without_copying=True,
        would_publish=True,
    )


def test_incomplete_review_is_private_and_does_not_unlock_distribution(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    run_id = _rendered_run(data_root)

    receipt = save_media_quality_review(
        data_root=data_root,
        run_id=run_id,
        checklist=MediaQualityChecklist(voice_natural=True),
        notes="The pacing still needs work.",
    )

    assert receipt.decision is MediaQualityDecision.NEEDS_CHANGES
    assert load_media_quality_review(data_root, run_id) == receipt
    path = content_run_directory(data_root, run_id) / "28_media_quality_review.json"
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(MediaQualityError, match="Approve"):
        require_approved_media_quality_review(data_root, run_id)


def test_approved_review_is_bound_to_exact_rendered_artifacts(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    run_id = _rendered_run(data_root)
    approved = save_media_quality_review(
        data_root=data_root,
        run_id=run_id,
        checklist=_approved_checklist(),
    )

    assert approved.decision is MediaQualityDecision.APPROVED
    assert require_approved_media_quality_review(data_root, run_id) == approved

    run_dir = content_run_directory(data_root, run_id)
    (run_dir / "10_creator_video.mp4").write_bytes(b"changed-after-review")
    with pytest.raises(MediaQualityError, match="changed"):
        require_approved_media_quality_review(data_root, run_id)
