"""Prepare one exact, non-publishing distribution package from approved content."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from typing import cast

from soloscale.content_models import ContentReviewDecision
from soloscale.content_workspace import (
    ContentWorkspaceError,
    content_run_directory,
    load_content_review,
    load_content_run,
)
from soloscale.media_quality import (
    MEDIA_QUALITY_FILENAME,
    MediaQualityError,
    require_approved_media_quality_review,
)
from soloscale.resume_workspace import ResumeWorkspaceStorageError, _atomic_private_write

_PACKAGE_NAME = "26_distribution_package.json"
_YOUTUBE_NAME = "27_youtube_upload.json"
_REQUIRED_MEDIA = {
    "video": "21_creator_video_youtube.mp4",
    "short": "10_creator_video.mp4",
    "thumbnail": "22_creator_video_thumbnail.png",
    "subtitles": "25_creator_video_subtitles.srt",
}
_DOWNLOAD_NAMES = {
    "video": "youtube-video.mp4",
    "short": "creator-video.mp4",
    "thumbnail": "video-thumbnail.png",
    "subtitles": "video-subtitles.srt",
}


class ContentDistributionError(ValueError):
    """Raised when an approved content run cannot be sealed for distribution."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _regular_artifact(run_dir: Path, filename: str) -> Path:
    path = run_dir / filename
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ContentDistributionError(f"Missing distribution artifact: {filename}") from exc
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
        raise ContentDistributionError(f"Unsafe distribution artifact: {filename}")
    return path


def prepare_distribution_package(*, data_root: Path, run_id: str) -> Path:
    """Seal approved text and rendered media without publishing any channel."""

    run = load_content_run(data_root, run_id)
    run_dir = content_run_directory(data_root, run_id)
    package_path = run_dir / _PACKAGE_NAME
    youtube_path = run_dir / _YOUTUBE_NAME
    if package_path.is_file() and not package_path.is_symlink():
        return package_path
    if package_path.exists() or package_path.is_symlink() or youtube_path.exists():
        raise ContentDistributionError("Distribution package path is unsafe")
    review = load_content_review(data_root, run_id)
    if review is None or review[0].decision is not ContentReviewDecision.APPROVED:
        raise ContentDistributionError("Approve the unified content review first")
    receipt, values = review
    media = {
        key: _regular_artifact(run_dir, filename)
        for key, filename in _REQUIRED_MEDIA.items()
    }
    try:
        media_quality = require_approved_media_quality_review(data_root, run_id)
    except MediaQualityError as exc:
        raise ContentDistributionError(str(exc)) from exc
    media_quality_path = _regular_artifact(run_dir, MEDIA_QUALITY_FILENAME)
    title = run.brief.topic.strip()[:100]
    description = values["canonical_story"].strip()
    locale = (
        run.locale_variant.locale
        if run.locale_variant is not None
        else ("zh-CN" if run.brief.language == "中文" else "en-US")
    )
    variant_group_id = (
        run.locale_variant.variant_group_id if run.locale_variant is not None else None
    )
    youtube = {
        "schema_version": "1.0",
        "status": "READY_FOR_HUMAN_YOUTUBE_UPLOAD",
        "run_id": run_id,
        "locale": locale,
        "variant_group_id": variant_group_id,
        "review_revision": receipt.revision,
        "media_quality_revision": media_quality.revision,
        "title": title,
        "description": description,
        "video": media["video"].name,
        "thumbnail": media["thumbnail"].name,
        "subtitles": media["subtitles"].name,
        "visibility": "private",
        "upload_performed": False,
        "publication_performed": False,
    }
    package = {
        "schema_version": "1.0",
        "status": "READY_FOR_EXACT_CHANNEL_PREVIEW",
        "run_id": run_id,
        "locale": locale,
        "variant_group_id": variant_group_id,
        "review_revision": receipt.revision,
        "media_quality_review": {
            "filename": MEDIA_QUALITY_FILENAME,
            "revision": media_quality.revision,
            "decision": media_quality.decision.value,
            "sha256": _sha256(media_quality_path),
        },
        "channels": {
            "linkedin": {
                "source": "approved review linkedin.md",
                "sha256": receipt.artifact_sha256["linkedin.md"],
                "publisher": "BuildLog",
            },
            "x": {
                "post_source": "approved review x-post.md",
                "post_sha256": receipt.artifact_sha256["x-post.md"],
                "thread_source": "approved review x-thread.md",
                "thread_sha256": receipt.artifact_sha256["x-thread.md"],
                "publisher": "BuildLog",
            },
            "youtube": {
                "metadata": _YOUTUBE_NAME,
                "adapter": "youtube-data-api-v3",
                "direct_upload_enabled": True,
            },
        },
        "artifacts": {
            key: {
                "filename": path.name,
                "sha256": _sha256(path),
                "download_path": f"/content/downloads/{run_id}/{_DOWNLOAD_NAMES[key]}",
            }
            for key, path in media.items()
        },
        "youtube_metadata_sha256": hashlib.sha256(
            (json.dumps(youtube, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        ).hexdigest(),
        "requires_explicit_publish_approval": True,
        "network_used": False,
        "publication_performed": False,
    }
    try:
        youtube_serialized = json.dumps(youtube, ensure_ascii=False, sort_keys=True) + "\n"
        package["youtube_metadata_sha256"] = hashlib.sha256(
            youtube_serialized.encode("utf-8")
        ).hexdigest()
        _atomic_private_write(youtube_path, youtube_serialized)
        _atomic_private_write(
            package_path,
            json.dumps(package, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
    except (OSError, ResumeWorkspaceStorageError) as exc:
        if not package_path.exists():
            youtube_path.unlink(missing_ok=True)
        raise ContentDistributionError("Could not save the distribution package") from exc
    return package_path


def load_distribution_package(data_root: Path, run_id: str) -> dict[str, object] | None:
    run_dir = content_run_directory(data_root, run_id)
    path = run_dir / _PACKAGE_NAME
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ContentDistributionError("Distribution package is unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContentDistributionError("Distribution package is invalid") from exc
    if not isinstance(value, dict) or value.get("run_id") != run_id:
        raise ContentDistributionError("Distribution package is invalid")
    return cast(dict[str, object], value)


def recent_distribution_packages(data_root: Path, *, limit: int = 8) -> list[dict[str, object]]:
    root = data_root / "content-runs"
    if root.is_symlink() or not root.is_dir():
        return []
    packages: list[dict[str, object]] = []
    for candidate in sorted(root.iterdir(), key=lambda path: path.name, reverse=True):
        if len(packages) >= limit:
            break
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        try:
            package = load_distribution_package(data_root, candidate.name)
        except (ContentDistributionError, ContentWorkspaceError):
            continue
        if package is not None:
            packages.append(package)
    return packages
