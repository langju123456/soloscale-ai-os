from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from soloscale.content_models import (
    ClaimStatus,
    ContentBrief,
    ContentClaim,
    ContentReviewDecision,
)
from soloscale.content_workspace import run_content_workspace, save_content_review
from soloscale.creator_production import (
    CreatorProductionError,
    assign_artifact_to_account,
    create_run_artifacts,
)
from soloscale.platform_accounts import (
    ConnectedIdentity,
    save_connected_identity,
)
from soloscale.youtube_publishing import (
    YouTubeJobSnapshot,
    YouTubePublishingJobManager,
)


def _stale_youtube_job(job_id: str) -> YouTubeJobSnapshot:
    now = datetime.now(UTC).isoformat()
    return YouTubeJobSnapshot(
        job_id=job_id,
        kind="connect",
        phase="WAITING_FOR_AUTHORIZATION",
        created_at=now,
        updated_at=now,
        authorization_url="https://accounts.google.com/o/oauth2/auth?stale=1",
    )


def test_stale_youtube_waiting_connect_recovers_to_failed_after_restart(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    writer = YouTubePublishingJobManager()
    writer._put(data_root, _stale_youtube_job("youtube-auth-abcdef123456"))
    reader = YouTubePublishingJobManager()

    snapshot = reader.get(data_root, "youtube-auth-abcdef123456")

    assert snapshot is not None
    assert snapshot.phase == "FAILED"
    assert snapshot.error_code == "OAUTH_INTERRUPTED"
    assert snapshot.authorization_url is None
    assert "connect again" in (snapshot.error_message or "")


def test_queue_assignment_rejects_platform_mismatch(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    brief = ContentBrief(
        topic="A verified engineering story",
        audience="AI engineers",
        language="English",
        call_to_action="Share your experience.",
        source_label="git:abc123",
        claims=[
            ContentClaim(
                id="CLAIM-01",
                text="A focused test passed.",
                status=ClaimStatus.VERIFIED,
                receipt="git:abc123",
            )
        ],
    )
    run = run_content_workspace(data_root=data_root, brief=brief)
    save_content_review(
        data_root=data_root,
        run_id=run.run_id,
        decision=ContentReviewDecision.APPROVED,
        updates={},
    )
    artifacts = create_run_artifacts(
        data_root=data_root,
        content_project_id="project-test",
        run_id=run.run_id,
        outputs=["ARTICLE"],
    )
    linkedin_artifact = next(item for item in artifacts if item.platform == "linkedin")

    save_connected_identity(
        data_root,
        ConnectedIdentity(
            platform="youtube",
            external_account_id="UCyoutube123",
            display_name="Video Channel",
            handle="video-channel",
            avatar_url=None,
            scopes=("https://www.googleapis.com/auth/youtube.upload",),
            token_reference="",
            connected_at=datetime.now(UTC).isoformat(),
        ),
        token_payload={
            "access_token": "test-token",
            "refresh_token": "test-refresh",
            "expires_at": (datetime.now(UTC).isoformat()),
        },
    )

    with pytest.raises(CreatorProductionError, match="not publish-capable"):
        assign_artifact_to_account(
            data_root=data_root,
            artifact_id=linkedin_artifact.artifact_id,
            channel_account_id="UCyoutube123",
        )
