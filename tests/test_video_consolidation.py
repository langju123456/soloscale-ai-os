from __future__ import annotations

import json
from pathlib import Path

from soloscale.content_models import (
    ClaimStatus,
    ContentBrief,
    ContentClaim,
    ContentRun,
)
from soloscale.content_workspace import run_content_workspace
from soloscale.video_story import (
    LocalVideoJobManager,
    build_content_run_story,
    local_video_job_directory,
)


def _brief() -> ContentBrief:
    return ContentBrief(
        topic="Evidence-first product integration",
        audience="AI engineers and solo builders",
        language="English",
        call_to_action="Follow the next measured iteration.",
        source_label="https://github.com/example/solo-scale/pull/8",
        claims=[
            ContentClaim(
                id="CLAIM-01",
                text="Python 3.11 and 3.12 CI checks passed.",
                status=ClaimStatus.VERIFIED,
                receipt="https://github.com/example/solo-scale/actions/runs/8",
                limits="This does not prove production readiness.",
            )
        ],
    )


def _content_run(tmp_path: Path) -> tuple[Path, ContentRun]:
    data_root = tmp_path / ".soloscale"
    run = run_content_workspace(data_root=data_root, brief=_brief())
    return data_root, run


def test_build_content_run_story_consumes_canonical_run_facts(tmp_path: Path) -> None:
    data_root, run = _content_run(tmp_path)
    story = build_content_run_story(data_root=data_root, content_run_id=run.run_id)

    assert story.story_id == f"content-{run.run_id}"
    assert story.title == run.brief.topic
    assert story.language == "en-US"
    assert len(story.scenes) == len(run.drafts.storyboard)
    assert story.scenes[0].id == "SCENE-01"
    assert story.scenes[0].visual_kind == "content"
    assert story.scenes[0].voiceover == run.drafts.storyboard[0].voiceover
    assert story.scenes[0].evidence_ids == run.drafts.storyboard[0].claim_ids
    assert [item.evidence_id for item in story.evidence] == [
        claim.id for claim in run.brief.claims
    ]
    assert story.evidence[0].kind == "content_claim"
    assert story.evidence[0].locator == f"run:{run.run_id}:claim:CLAIM-01"
    assert 10 <= story.duration_seconds <= 180


def test_local_video_submit_records_content_run_downstream(tmp_path: Path) -> None:
    data_root, run = _content_run(tmp_path)
    manager = LocalVideoJobManager()
    try:
        job_id = manager.submit(
            data_root=data_root,
            repository_root=tmp_path,
            content_run_id=run.run_id,
        )
    finally:
        manager.shutdown()

    assert job_id.startswith("video-story-")
    record_path = local_video_job_directory(data_root, job_id) / "job.json"
    assert record_path.is_file()
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["story_id"] == f"content-{run.run_id}"
    assert record["content_run_id"] == run.run_id
