"""Human-approved Google Vertex AI Veo jobs for the local Creator Video page."""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from soloscale.resume_workspace import (
    _atomic_private_write,
    _atomic_private_write_bytes,
    _ensure_private_directory,
)

VideoStatus = Literal["AWAITING_APPROVAL", "SUBMITTED", "RUNNING", "SUCCEEDED", "FAILED"]


class VideoGenerationRequest(BaseModel):
    topic: str = Field(min_length=3, max_length=400)
    script: str = Field(min_length=3, max_length=8000)
    platform: str = Field(default="Short video", max_length=80)
    language: str = Field(default="English", max_length=40)
    style: str = Field(default="Cinematic product demo", max_length=240)
    duration_seconds: Literal[4, 6, 8] = 8
    aspect_ratio: Literal["9:16", "16:9"] = "9:16"
    resolution: Literal["720p", "1080p"] = "720p"
    generate_audio: bool = True
    content_run_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    evidence_excerpts: list[str] = Field(default_factory=list, max_length=12)
    asset_names: list[str] = Field(default_factory=list, max_length=3)

    def external_payload(self) -> dict[str, object]:
        return {
            "topic": self.topic,
            "script": self.script,
            "platform": self.platform,
            "language": self.language,
            "style": self.style,
            "duration_seconds": self.duration_seconds,
            "aspect_ratio": self.aspect_ratio,
            "resolution": self.resolution,
            "generate_audio": self.generate_audio,
            "evidence_excerpts": self.evidence_excerpts,
            "asset_names": self.asset_names,
        }


class VideoGenerationJob(BaseModel):
    job_id: str
    request: VideoGenerationRequest
    request_hash: str
    provider: Literal["google_vertex_ai"] = "google_vertex_ai"
    model: str = "veo-3.1-fast-generate-001"
    fallback_model: str = "veo-3.1-generate-001"
    status: VideoStatus = "AWAITING_APPROVAL"
    estimated_cost_usd: float = 0.80
    operation_id: str | None = None
    output_uri: str | None = None
    output_path: str | None = None
    output_hash: str | None = None
    generated_at: datetime | None = None
    error: str | None = None

    @property
    def provider_configured(self) -> bool:
        return bool(os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip())


class VideoGenerationError(ValueError):
    pass


def create_job(data_root: Path, request: VideoGenerationRequest) -> VideoGenerationJob:
    _ensure_private_directory(data_root / "video-jobs", parents=True)
    payload = request.external_payload()
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    job = VideoGenerationJob(
        job_id=f"video-{uuid4().hex[:12]}", request=request, request_hash=digest
    )
    save_job(data_root, job)
    return job


def load_job(data_root: Path, job_id: str) -> VideoGenerationJob:
    if not re.fullmatch(r"video-[a-f0-9]{12}", job_id):
        raise VideoGenerationError("invalid video job")
    try:
        return VideoGenerationJob.model_validate_json(
            (data_root / "video-jobs" / job_id / "job.json").read_text()
        )
    except (OSError, ValueError) as exc:
        raise VideoGenerationError("video job is unavailable") from exc


def save_job(data_root: Path, job: VideoGenerationJob) -> None:
    directory = data_root / "video-jobs" / job.job_id
    _ensure_private_directory(directory)
    _atomic_private_write(directory / "job.json", job.model_dump_json(indent=2) + "\n")


def provider_status() -> Literal["READY", "PROVIDER_NOT_CONFIGURED"]:
    """Report configuration without initializing the SDK or touching ADC."""

    return (
        "READY" if os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip() else "PROVIDER_NOT_CONFIGURED"
    )


def _download_vertex_uri(uri: str) -> bytes:
    """Download a Vertex-owned GCS result with ADC after explicit submission."""

    parsed = urllib.parse.urlsplit(uri)
    if parsed.scheme == "gs" and parsed.netloc and parsed.path.strip("/"):
        bucket = urllib.parse.quote(parsed.netloc, safe="")
        object_name = urllib.parse.quote(parsed.path.lstrip("/"), safe="")
        download_url = (
            f"https://storage.googleapis.com/download/storage/v1/b/{bucket}/o/"
            f"{object_name}?alt=media"
        )
    elif parsed.scheme == "https" and parsed.hostname == "storage.googleapis.com":
        download_url = uri
    else:
        raise VideoGenerationError("Vertex AI returned an unsupported output URI")
    try:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        session = AuthorizedSession(credentials)  # type: ignore[no-untyped-call]
        response = session.get(download_url, timeout=120)
        response.raise_for_status()
    except Exception as exc:
        raise VideoGenerationError("Vertex AI output download failed") from exc
    return bytes(response.content)


class GoogleVeoClient:
    """Thin Vertex-only client. Credentials remain entirely in ADC."""

    def __init__(self) -> None:
        self.project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
        if not self.project:
            raise VideoGenerationError("GOOGLE_CLOUD_PROJECT is required for Vertex AI")
        try:
            from google import genai
        except ImportError as exc:
            raise VideoGenerationError("Install google-genai to use cloud video") from exc
        self._client = genai.Client(vertexai=True, project=self.project, location="global")

    def submit(self, job: VideoGenerationJob) -> VideoGenerationJob:
        from google.genai import types

        config = types.GenerateVideosConfig(
            aspect_ratio=job.request.aspect_ratio,
            resolution=job.request.resolution,
            duration_seconds=job.request.duration_seconds,
            generate_audio=job.request.generate_audio,
        )
        prompt = "\n\n".join(
            [
                f"Topic: {job.request.topic}",
                f"Language: {job.request.language}",
                f"Style: {job.request.style}",
                f"Script/design: {job.request.script}",
                *[f"Approved evidence excerpt: {item}" for item in job.request.evidence_excerpts],
            ]
        )
        try:
            operation = self._client.models.generate_videos(
                model=job.model, prompt=prompt, config=config
            )
        except Exception:
            job.status = "FAILED"
            job.error = "Vertex AI submission failed"
            return job
        job.operation_id = str(operation.name)
        job.status = "SUBMITTED"
        return job

    def poll(self, job: VideoGenerationJob, *, data_root: Path) -> VideoGenerationJob:
        if not job.operation_id:
            raise VideoGenerationError("video job has no operation")
        try:
            from google.genai import types

            operation = self._client.operations.get(
                operation=types.GenerateVideosOperation.model_validate({"name": job.operation_id})
            )
        except Exception:
            job.status = "FAILED"
            job.error = "Vertex AI status check failed"
            return job
        if not operation.done:
            job.status = "RUNNING"
            return job
        response = operation.response or operation.result
        videos = getattr(response, "generated_videos", None) if response else None
        if not videos:
            job.status = "FAILED"
            job.error = "Vertex AI returned no video"
            return job
        video = videos[0].video
        job.output_uri = str(video.uri) if video.uri else None
        target = data_root / "video-jobs" / job.job_id / "output.mp4"
        try:
            payload = video.video_bytes or _download_vertex_uri(str(video.uri))
            if not target.exists():
                _atomic_private_write_bytes(target, payload)
        except (OSError, ValueError):
            job.status = "FAILED"
            job.error = "Vertex AI output download failed"
            return job
        job.output_path = str(target)
        job.output_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        job.status = "SUCCEEDED"
        job.generated_at = datetime.now(UTC)
        return job
