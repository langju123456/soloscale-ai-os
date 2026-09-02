import json
import time
from pathlib import Path

import pytest

from soloscale.local_ui import _video_page
from soloscale.video_story import (
    EngineeringStory,
    LocalVideoJobManager,
    LocalVideoJobSnapshot,
    VideoStoryEvidence,
    build_resume_latency_story,
    load_local_video_job,
    local_video_artifact,
)


def _write_resume_timing_receipt(
    data_root: Path,
    run_id: str,
    *,
    post_ms: int,
    model_ms: int,
    total_ms: int,
) -> None:
    run_dir = data_root / "resume-runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": "DRAFT_REQUIRES_HUMAN_REVIEW",
                "resume_job": {
                    "phase": "COMPLETE",
                    "timing_ms": {
                        "post_response_ms": post_ms,
                        "model_generation_ms": model_ms,
                        "total_ms": total_ms,
                        "docx_ms": 10,
                        "pdf_preview_ms": 900,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "application_receipt.json").write_text(
        json.dumps({"provider": "ollama", "model": "qwen3:8b"}),
        encoding="utf-8",
    )


def test_resume_latency_story_requires_and_preserves_measured_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_ids = (
        "resume-20260826T103534Z-ac947a4af6",
        "resume-20260826T103826Z-0a7e1a7d58",
    )
    _write_resume_timing_receipt(
        tmp_path,
        run_ids[0],
        post_ms=19,
        model_ms=121_609,
        total_ms=122_584,
    )
    _write_resume_timing_receipt(
        tmp_path,
        run_ids[1],
        post_ms=16,
        model_ms=127_330,
        total_ms=128_258,
    )
    monkeypatch.setattr(
        "soloscale.video_story._verify_implementation_commit",
        lambda _: VideoStoryEvidence(
            evidence_id="EVIDENCE-01",
            kind="git_commit",
            locator="git:2a0ceaf",
            sha256="a" * 64,
            verified_facts=["ResumeJobManager uses one background worker."],
        ),
    )

    story = build_resume_latency_story(data_root=tmp_path, repository_root=tmp_path)

    assert story.duration_seconds == 84
    assert (story.width, story.height, story.fps) == (1080, 1920, 30)
    assert len(story.scenes) == 7
    assert {item.evidence_id for item in story.evidence} == {
        "EVIDENCE-01",
        "EVIDENCE-02",
        "EVIDENCE-03",
    }
    assert ">99%" in story.scenes[5].on_screen_text
    assert "122.584s" in story.scenes[4].detail_lines[0]
    assert "128.258s" in story.scenes[4].detail_lines[1]


def test_local_video_job_runs_in_background_and_persists_complete_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def synthetic_story() -> EngineeringStory:
        return EngineeringStory.model_validate(
            {
                "story_id": "resume-latency-system-design-v1",
                "title": "Synthetic story",
                "subtitle": "Measured locally",
                "duration_seconds": 84,
                "layers": {
                    "fact": "fact",
                    "architecture": "architecture",
                    "decision": "decision",
                    "implementation": "implementation",
                    "failure_and_surprise": "surprise",
                    "evolution": "evolution",
                },
                "evidence": [],
                "scenes": [
                    {
                        "id": f"SCENE-{index:02d}",
                        "start_second": (index - 1) * 12,
                        "end_second": index * 12,
                        "purpose": "Synthetic",
                        "visual_kind": "hook",
                        "voiceover": "Synthetic narration",
                        "on_screen_text": "Synthetic",
                    }
                    for index in range(1, 8)
                ],
            }
        )

    monkeypatch.setattr(
        "soloscale.video_story.build_resume_latency_story",
        lambda **_: synthetic_story(),
    )

    def fake_render(*, job_dir: Path, **_: object) -> bool:
        (job_dir / "engineering-story.mp4").write_bytes(b"synthetic-video")
        (job_dir / "engineering-story-thumbnail.png").write_bytes(b"synthetic-thumbnail")
        return True

    monkeypatch.setattr(
        "soloscale.video_story._create_local_narration",
        lambda _job_dir, story: {
            scene.id: "data:audio/wav;base64,c3ludGhldGlj" for scene in story.scenes
        },
    )
    monkeypatch.setattr("soloscale.video_story._render_with_remotion", fake_render)
    manager = LocalVideoJobManager()
    try:
        job_id = manager.submit(data_root=tmp_path, repository_root=tmp_path)
        queued_or_running = manager.get(tmp_path, job_id)
        assert queued_or_running is not None
        assert queued_or_running.phase in {
            "QUEUED",
            "PREPARING_STORY",
            "PREPARING_ASSETS",
            "RENDERING",
            "COMPLETE",
        }
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            completed = manager.get(tmp_path, job_id)
            if completed is not None and completed.phase in {"COMPLETE", "FAILED"}:
                break
            time.sleep(0.01)
        assert completed is not None
        assert completed.phase == "COMPLETE"
        assert completed.audio_included is True
        assert completed.output_sha256
        persisted = load_local_video_job(tmp_path, job_id)
        assert persisted.phase == "COMPLETE"
        latest = manager.latest(tmp_path)
        assert latest is not None
        assert latest.job_id == job_id
        assert latest.phase == "COMPLETE"
        assert local_video_artifact(tmp_path, job_id, "video").read_bytes() == b"synthetic-video"
        receipt = json.loads(local_video_artifact(tmp_path, job_id, "receipt").read_text())
        assert receipt["publication_performed"] is False
        assert receipt["network_used"] is False
    finally:
        manager.shutdown()

    persisted_reader = LocalVideoJobManager()
    try:
        persisted_latest = persisted_reader.latest(tmp_path)
        assert persisted_latest is not None
        assert persisted_latest.total_elapsed_ms >= max(
            persisted_latest.stage_durations_ms.values(), default=0
        )
    finally:
        persisted_reader.shutdown()


def test_video_page_exposes_background_progress_and_completed_downloads(tmp_path: Path) -> None:
    rendering = LocalVideoJobSnapshot(
        job_id="video-story-20260827T010101Z-aaaaaaaaaa",
        story_id="resume-latency-system-design-v1",
        phase="RENDERING",
        created_at="2026-08-27T01:01:01+00:00",
        stage_durations_ms={"story_ms": 10},
        total_elapsed_ms=1400,
        output_sha256=None,
        output_duration_seconds=None,
        audio_included=False,
        error_code=None,
        error_message=None,
    )
    progress_page = _video_page(
        tmp_path,
        local_job=rendering,
        local_video_available=True,
    )
    assert 'data-phase="RENDERING"' in progress_page
    assert "window.location.reload()" in progress_page
    assert "Resume、Learning 或 Content" in progress_page

    completed = LocalVideoJobSnapshot(
        **{
            **rendering.__dict__,
            "phase": "COMPLETE",
            "output_sha256": "b" * 64,
            "output_duration_seconds": 84.0,
            "audio_included": True,
        }
    )
    complete_page = _video_page(
        tmp_path,
        local_job=completed,
        local_video_available=True,
    )
    assert f"/video/local/downloads/{completed.job_id}/video" in complete_page
    assert "下载 MP4" in complete_page
    assert "Render receipt" in complete_page
    assert "window.location.reload()" not in complete_page
