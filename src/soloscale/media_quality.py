"""Human media-quality review bound to exact rendered artifacts."""

from __future__ import annotations

import hashlib
import json
import stat
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from soloscale.content_workspace import (
    content_run_directory,
    load_content_run,
)
from soloscale.resume_workspace import ResumeWorkspaceStorageError, _atomic_private_write

MEDIA_QUALITY_FILENAME = "28_media_quality_review.json"
_DISTRIBUTION_FILENAME = "26_distribution_package.json"
_MEDIA_ARTIFACTS = {
    "youtube_video": "21_creator_video_youtube.mp4",
    "short_video": "10_creator_video.mp4",
    "thumbnail": "22_creator_video_thumbnail.png",
    "subtitles": "25_creator_video_subtitles.srt",
}


class MediaQualityError(ValueError):
    """Raised when the human media-quality boundary is incomplete or stale."""


class MediaQualityDecision(StrEnum):
    APPROVED = "APPROVED"
    NEEDS_CHANGES = "NEEDS_CHANGES"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MediaQualityChecklist(_StrictModel):
    voice_natural: bool = False
    pacing_natural: bool = False
    no_static_visual_too_long: bool = False
    presenter_adds_value: bool = False
    language_natural: bool = False
    claims_evidence_backed: bool = False
    reference_influenced_without_copying: bool = False
    would_publish: bool = False

    @property
    def all_passed(self) -> bool:
        return all(self.model_dump(mode="python").values())


class MediaQualityReviewReceipt(_StrictModel):
    schema_version: str = "1.0"
    run_id: str = Field(pattern=r"^content-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{10}$")
    revision: int = Field(ge=1)
    decision: MediaQualityDecision
    checklist: MediaQualityChecklist
    notes: str = Field(default="", max_length=2000)
    artifact_sha256: dict[str, str]
    reviewed_at: datetime
    reviewer: Literal["human"] = "human"
    network_used: Literal[False] = False
    publication_performed: Literal[False] = False

    @model_validator(mode="after")
    def decision_and_artifacts_are_consistent(self) -> MediaQualityReviewReceipt:
        expected = set(_MEDIA_ARTIFACTS)
        if set(self.artifact_sha256) != expected:
            raise ValueError("media-quality receipt must cover every rendered artifact")
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in self.artifact_sha256.values()
        ):
            raise ValueError("media-quality artifact hashes are invalid")
        expected_decision = (
            MediaQualityDecision.APPROVED
            if self.checklist.all_passed
            else MediaQualityDecision.NEEDS_CHANGES
        )
        if self.decision is not expected_decision:
            raise ValueError("media-quality decision does not match its checklist")
        return self


def _artifact_hashes(run_dir: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for key, filename in _MEDIA_ARTIFACTS.items():
        path = run_dir / filename
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise MediaQualityError(f"Missing rendered artifact: {filename}") from exc
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
            raise MediaQualityError(f"Unsafe rendered artifact: {filename}")
        hashes[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def load_media_quality_review(
    data_root: Path, run_id: str
) -> MediaQualityReviewReceipt | None:
    run_dir = content_run_directory(data_root, run_id)
    path = run_dir / MEDIA_QUALITY_FILENAME
    if not path.exists():
        return None
    try:
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise MediaQualityError("Media-quality review path is unsafe")
        receipt = MediaQualityReviewReceipt.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValueError) as exc:
        if isinstance(exc, MediaQualityError):
            raise
        raise MediaQualityError("Media-quality review is invalid") from exc
    if receipt.run_id != run_id:
        raise MediaQualityError("Media-quality review belongs to another run")
    return receipt


def save_media_quality_review(
    *,
    data_root: Path,
    run_id: str,
    checklist: MediaQualityChecklist,
    notes: str = "",
) -> MediaQualityReviewReceipt:
    """Persist a human decision for the exact current video package."""

    load_content_run(data_root, run_id)
    run_dir = content_run_directory(data_root, run_id)
    if (run_dir / _DISTRIBUTION_FILENAME).exists():
        raise MediaQualityError("The distribution package is already sealed")
    previous = load_media_quality_review(data_root, run_id)
    receipt = MediaQualityReviewReceipt(
        run_id=run_id,
        revision=(previous.revision + 1 if previous is not None else 1),
        decision=(
            MediaQualityDecision.APPROVED
            if checklist.all_passed
            else MediaQualityDecision.NEEDS_CHANGES
        ),
        checklist=checklist,
        notes=notes.strip(),
        artifact_sha256=_artifact_hashes(run_dir),
        reviewed_at=datetime.now(UTC),
    )
    try:
        _atomic_private_write(
            run_dir / MEDIA_QUALITY_FILENAME,
            json.dumps(receipt.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        )
    except (OSError, ResumeWorkspaceStorageError) as exc:
        raise MediaQualityError("Could not save the media-quality review") from exc
    return receipt


def require_approved_media_quality_review(
    data_root: Path, run_id: str
) -> MediaQualityReviewReceipt:
    receipt = load_media_quality_review(data_root, run_id)
    if receipt is None or receipt.decision is not MediaQualityDecision.APPROVED:
        raise MediaQualityError("Approve the human media-quality checklist first")
    current_hashes = _artifact_hashes(content_run_directory(data_root, run_id))
    if current_hashes != receipt.artifact_sha256:
        raise MediaQualityError("Rendered media changed after its quality review")
    return receipt
