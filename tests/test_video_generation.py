from pathlib import Path
from types import SimpleNamespace

import pytest

from soloscale.video_generation import (
    GoogleVeoClient,
    VideoGenerationRequest,
    create_job,
    load_job,
    provider_status,
)


def test_video_job_persists_external_preview_without_submitting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    request = VideoGenerationRequest(
        topic="Evidence-backed Creator Video",
        script="Show a product workflow with a clear human approval gate.",
        evidence_ids=["chunk-001"],
        evidence_excerpts=["The product preserves a verification receipt."],
    )
    job = create_job(tmp_path / ".soloscale", request)
    loaded = load_job(tmp_path / ".soloscale", job.job_id)
    assert loaded.status == "AWAITING_APPROVAL"
    assert loaded.request_hash
    assert loaded.request.external_payload()["evidence_excerpts"] == request.evidence_excerpts
    assert provider_status() == "PROVIDER_NOT_CONFIGURED"


def test_completed_vertex_uri_is_downloaded_with_adc_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "video"
    job = create_job(
        data_root,
        VideoGenerationRequest(topic="A real topic", script="A complete video script."),
    )
    job.operation_id = "projects/example/locations/global/operations/123"
    operation = SimpleNamespace(
        done=True,
        response=SimpleNamespace(
            generated_videos=[
                SimpleNamespace(
                    video=SimpleNamespace(uri="gs://verified-output/video.mp4", video_bytes=None)
                )
            ]
        ),
        result=None,
    )
    client = GoogleVeoClient.__new__(GoogleVeoClient)
    client.__dict__["_client"] = SimpleNamespace(
        operations=SimpleNamespace(get=lambda **_: operation)
    )
    monkeypatch.setattr(
        "soloscale.video_generation._download_vertex_uri", lambda _: b"real-video-bytes"
    )

    completed = client.poll(job, data_root=data_root)

    assert completed.status == "SUCCEEDED"
    assert completed.output_uri == "gs://verified-output/video.mp4"
    assert Path(completed.output_path or "").read_bytes() == b"real-video-bytes"
    assert completed.output_hash is not None
