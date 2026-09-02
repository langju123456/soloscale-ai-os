"""In-process handoff from reviewed SoloScale content to BuildLog publishing."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal, cast

from soloscale.content_workspace import (
    approved_content_artifact,
    content_run_directory,
    load_content_run,
)
from soloscale.evidence_capture import capture_assets, capture_outcome
from soloscale.evidence_hub import EvidenceHub
from soloscale.resume_workspace import ResumeWorkspaceStorageError, _atomic_private_write
from soloscale.runtime_paths import resolve_resource_root

Channel = Literal["linkedin", "x"]
_SOURCE_NAMES: dict[Channel, str] = {"linkedin": "02_linkedin.md", "x": "03_x_post.md"}


class BuildLogHandoffError(ValueError):
    """Raised when BuildLog cannot safely preview or publish an artifact."""


def _gateway(data_root: Path, channel: Channel):  # type: ignore[no-untyped-def]
    try:
        from buildlog.publishing_gateway import PublishingGateway
    except ImportError as exc:
        raise BuildLogHandoffError("BuildLog internal package is unavailable") from exc
    config_value = os.environ.get("BUILDLOG_CONFIG_ROOT", "").strip()
    config_root = (
        Path(config_value).expanduser().absolute()
        if config_value
        else resolve_resource_root() / "packages" / "buildlog"
    )
    return PublishingGateway(
        data_root=data_root / "publishing",
        config_root=config_root,
        channel=channel,
    )


def stage_for_buildlog(*, data_root: Path, run_id: str, channel: Channel) -> dict[str, str]:
    """Stage one exact artifact inside BuildLog without authenticating or publishing."""

    run_dir = content_run_directory(data_root, run_id)
    record_path = run_dir / f"12_buildlog_{channel}.json"
    if record_path.exists() or record_path.is_symlink():
        return _load_string_record(record_path)
    source_artifact, source, review = approved_content_artifact(
        data_root, run_id, channel
    )
    if source.is_symlink() or not source.is_file():
        raise BuildLogHandoffError("Content artifact is unavailable")
    try:
        buildlog_run_id = _gateway(data_root, channel).stage(
            source_path=source,
            source_run_id=run_id,
        )
    except Exception as exc:
        raise BuildLogHandoffError("BuildLog could not stage this artifact") from exc
    record = {
        "status": "STAGED_FOR_BUILDLOG_APPROVAL",
        "channel": channel,
        "source_run_id": run_id,
        "source_artifact": source_artifact,
        "review_revision": str(review.revision),
        "buildlog_run_id": buildlog_run_id,
    }
    _write_record(record_path, record)
    return record


def preview_for_buildlog(*, data_root: Path, run_id: str, channel: Channel) -> dict[str, object]:
    """Resolve identity and exact content without making a publication request."""

    run_dir = content_run_directory(data_root, run_id)
    preview_path = run_dir / f"12_buildlog_{channel}_preview.json"
    if preview_path.is_file() and not preview_path.is_symlink():
        return _load_record(preview_path)
    handoff = stage_for_buildlog(data_root=data_root, run_id=run_id, channel=channel)
    try:
        preview = _gateway(data_root, channel).preview(handoff["buildlog_run_id"])
    except Exception as exc:
        raise BuildLogHandoffError(
            f"BuildLog {channel} publishing is not configured or authorized"
        ) from exc
    record: dict[str, object] = {
        "status": "READY_FOR_EXPLICIT_PUBLISH_APPROVAL",
        "channel": channel,
        "source_run_id": run_id,
        "buildlog_run_id": handoff["buildlog_run_id"],
        "platform": preview.platform.value,
        "account_reference": preview.account_reference,
        "account_display_name": preview.account_display_name,
        "content": preview.content,
        "content_hash": preview.content_hash,
        "content_length": preview.content_length,
        "duplicate_found": preview.duplicate_found,
        "indeterminate_found": preview.indeterminate_found,
        "network_publication_performed": False,
    }
    _replace_record(preview_path, record)
    return record


def publish_via_buildlog(
    *,
    data_root: Path,
    run_id: str,
    channel: Channel,
    confirmation: str,
    evidence_hub: EvidenceHub | None = None,
) -> dict[str, str]:
    """Publish through BuildLog only after approval bound to a stored exact preview."""

    if confirmation != "PUBLISH":
        raise BuildLogHandoffError("Type PUBLISH to authorize this exact publication")
    run_dir = content_run_directory(data_root, run_id)
    preview = _load_record(run_dir / f"12_buildlog_{channel}_preview.json")
    handoff = _load_string_record(run_dir / f"12_buildlog_{channel}.json")
    required = ("buildlog_run_id", "content_hash", "account_reference")
    if any(not isinstance(preview.get(key), str) or not preview[key] for key in required):
        raise BuildLogHandoffError("BuildLog preview is incomplete; preview again")
    try:
        receipt = _gateway(data_root, channel).publish(
            cast(str, preview["buildlog_run_id"]),
            confirmation=confirmation,
            approved_content_hash=cast(str, preview["content_hash"]),
            approved_account_reference=cast(str, preview["account_reference"]),
        )
    except Exception as exc:
        raise BuildLogHandoffError("BuildLog publication failed; inspect its receipt") from exc
    if receipt.external_post_id is None or receipt.published_at is None:
        raise BuildLogHandoffError("BuildLog did not return a successful receipt")
    record = {
        "receipt_id": receipt.receipt_id,
        "platform": receipt.platform.value,
        "status": receipt.status.value,
        "external_post_id": receipt.external_post_id,
        "published_at": receipt.published_at.isoformat(),
        "buildlog_run_id": receipt.run_id,
        "source_run_id": run_id,
    }
    _write_record(run_dir / f"13_buildlog_{channel}_receipt.json", record)
    source_artifact = handoff.get("source_artifact", _SOURCE_NAMES[channel])
    artifact_path = Path(source_artifact)
    if artifact_path.is_absolute() or ".." in artifact_path.parts:
        raise BuildLogHandoffError("BuildLog source artifact is invalid")
    source = run_dir / source_artifact
    try:
        source.relative_to(run_dir)
    except ValueError as exc:
        raise BuildLogHandoffError("BuildLog source artifact is invalid") from exc
    if source.is_symlink() or not source.is_file():
        raise BuildLogHandoffError("BuildLog source artifact is unavailable")
    content_run = load_content_run(data_root, run_id)
    try:
        final_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    except OSError:
        # The capture helper will retain a private retry warning for the invalid digest.
        final_sha256 = ""
    captured_assets = capture_assets(
        data_root=data_root,
        run_dir=run_dir,
        owner="content",
        run_id=run_id,
        artifact_names=[source_artifact],
        evidence_bundle_id=content_run.brief.evidence_bundle_id,
        evidence_item_ids=content_run.brief.evidence_item_ids,
        evidence_hub=evidence_hub,
    )
    capture_outcome(
        data_root=data_root,
        run_dir=run_dir,
        owner="buildlog",
        run_id=run_id,
        outcome_type="publication",
        platform=receipt.platform.value,
        status=receipt.status.value,
        final_sha256=final_sha256,
        external_id=receipt.external_post_id,
        metadata={
            "channel": channel,
            "buildlog_run_id": receipt.run_id,
            "receipt_id": receipt.receipt_id,
            "published_at": receipt.published_at.isoformat(),
        },
        evidence_item_ids=content_run.brief.evidence_item_ids,
        asset_id=captured_assets.get(source_artifact),
        evidence_hub=evidence_hub,
    )
    return record


def buildlog_handoff_status(
    data_root: Path, run_id: str, channel: Channel
) -> tuple[dict[str, str] | None, dict[str, object] | None, dict[str, str] | None]:
    run_dir = content_run_directory(data_root, run_id)
    handoff_path = run_dir / f"12_buildlog_{channel}.json"
    preview_path = run_dir / f"12_buildlog_{channel}_preview.json"
    receipt_path = run_dir / f"13_buildlog_{channel}_receipt.json"
    handoff = _load_string_record(handoff_path) if handoff_path.is_file() else None
    preview = _load_record(preview_path) if preview_path.is_file() else None
    receipt = _load_string_record(receipt_path) if receipt_path.is_file() else None
    return handoff, preview, receipt


def _write_record(path: Path, record: dict[str, str]) -> None:
    try:
        _atomic_private_write(path, json.dumps(record, indent=2) + "\n")
    except (OSError, ResumeWorkspaceStorageError) as exc:
        raise BuildLogHandoffError("BuildLog result could not be recorded") from exc


def _replace_record(path: Path, record: dict[str, object]) -> None:
    try:
        _atomic_private_write(path, json.dumps(record, indent=2) + "\n")
    except (OSError, ResumeWorkspaceStorageError) as exc:
        raise BuildLogHandoffError("BuildLog preview could not be recorded") from exc


def _load_record(path: Path) -> dict[str, object]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuildLogHandoffError("BuildLog handoff record is invalid") from exc
    if not isinstance(loaded, dict):
        raise BuildLogHandoffError("BuildLog handoff record is invalid")
    return cast(dict[str, object], loaded)


def _load_string_record(path: Path) -> dict[str, str]:
    loaded = _load_record(path)
    if not all(isinstance(value, str) for value in loaded.values()):
        raise BuildLogHandoffError("BuildLog handoff record is invalid")
    return cast(dict[str, str], loaded)
