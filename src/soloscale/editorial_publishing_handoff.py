# ruff: noqa: E501
"""Read-only handoff from a sealed editorial day to BuildLog publication plans."""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from soloscale.editorial_workspace import validate_public_editorial_text
from soloscale.platform_accounts import (
    PlatformAccountError,
    require_publish_account_reference,
)
from soloscale.resume_workspace import (
    ResumeWorkspaceStorageError,
    _atomic_private_write,
    _reject_symlink_ancestry,
)
from soloscale.runtime_paths import resolve_resource_root

EditorialChannel = Literal["linkedin", "x"]
_X_MARKER = re.compile(r"(?m)^(?P<number>[1-9][0-9]*)/(?P<total>[1-9][0-9]*)(?:\s|$)")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class EditorialPublishingError(ValueError):
    """Raised when a sealed editorial package cannot be safely handed off."""


def _gateway(data_root: Path, channel: EditorialChannel) -> Any:
    try:
        from buildlog.publication_plan_gateway import (
            PublicationPlanGateway,
        )
    except ImportError as exc:
        raise EditorialPublishingError("BuildLog publication-plan package is unavailable") from exc
    configured_root = os.environ.get("BUILDLOG_CONFIG_ROOT", "").strip()
    config_root = (
        Path(configured_root).expanduser().absolute()
        if configured_root
        else resolve_resource_root() / "packages" / "buildlog"
    )
    return PublicationPlanGateway(
        data_root=data_root / "publishing",
        config_root=config_root,
        platform=channel,
    )


def preview_editorial_day(
    *, data_root: Path, day_directory: Path, channel: EditorialChannel
) -> dict[str, object]:
    """Verify a finalized external day and create a non-publishing BuildLog preview."""

    package = _load_package(day_directory)
    parts = [package["linkedin"]] if channel == "linkedin" else package["x_parts"]
    try:
        plan = _gateway(data_root, channel).stage(
            text_parts=parts,
            image_path=package["image_path"],
            alt_text=package["alt_text"],
            source_package_id=package["package_id"],
            source_receipt_hash=package["final_validation_hash"],
        )
        preview = _gateway(data_root, channel).preview(plan.plan_id)
    except Exception as exc:
        raise EditorialPublishingError("BuildLog could not stage or preview this editorial day") from exc
    record: dict[str, object] = {
        "status": "READY_FOR_EXPLICIT_CHANNEL_APPROVAL",
        "channel": channel,
        "package_id": package["package_id"],
        "source_receipt_hash": package["final_validation_hash"],
        "plan_id": preview.plan_id,
        "plan_hash": preview.plan_hash,
        "account_reference": preview.account_reference,
        "account_display_name": preview.account_display_name,
        "parts": list(preview.parts),
        "image": _model_dump(preview.image),
        "source_image_path": f"visual/{cast(Path, package['image_path']).name}",
        "duplicate": preview.duplicate_found,
        "indeterminate": preview.indeterminate_found,
        "network_publication_performed": False,
    }
    _write_preview(data_root, channel, record)
    return record


def publish_editorial_preview(
    *, data_root: Path, channel: EditorialChannel, confirmation: str
) -> dict[str, object]:
    """Publish only the server-stored channel preview; form fields never bind approval."""

    if confirmation != "PUBLISH":
        raise EditorialPublishingError("Type PUBLISH to authorize this exact channel plan")
    preview = _load_preview(data_root, channel)
    required = ("plan_id", "plan_hash", "account_reference")
    if any(not isinstance(preview.get(key), str) or not preview[key] for key in required):
        raise EditorialPublishingError("Editorial preview is incomplete; preview the channel again")
    try:
        require_publish_account_reference(
            data_root,
            channel,
            cast(str, preview["account_reference"]),
        )
    except PlatformAccountError as exc:
        raise EditorialPublishingError(
            "Editorial preview account does not match an eligible connected account"
        ) from exc
    try:
        result = _gateway(data_root, channel).publish(
            cast(str, preview["plan_id"]),
            confirmation=confirmation,
            approved_plan_hash=cast(str, preview["plan_hash"]),
            approved_account_reference=cast(str, preview["account_reference"]),
        )
    except Exception as exc:
        _write_receipt(
            data_root,
            channel,
            {
                "status": "PUBLICATION_FAILED_OR_INDETERMINATE_DO_NOT_RETRY",
                "channel": channel,
                "plan_id": preview["plan_id"],
                "plan_hash": preview["plan_hash"],
                "error_category": type(exc).__name__,
                "requires_manual_inspection": True,
                "recorded_at": datetime.now(UTC).isoformat(),
            },
        )
        raise EditorialPublishingError("BuildLog publication failed; inspect its plan receipt") from exc
    record = _model_dump(result)
    _write_receipt(data_root, channel, record)
    return record


def editorial_publishing_status(
    data_root: Path, channel: EditorialChannel
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    return _optional_record(_preview_path(data_root, channel)), _optional_record(_receipt_path(data_root, channel))


def editorial_image_preview(data_root: Path, channel: EditorialChannel) -> bytes:
    """Return only the hash-verified PNG staged for the current channel preview."""

    preview = _load_preview(data_root, channel)
    plan_id = preview.get("plan_id")
    image = preview.get("image")
    if (
        not isinstance(plan_id, str)
        or re.fullmatch(r"plan-[0-9a-f-]+", plan_id) is None
        or not isinstance(image, dict)
        or not isinstance(image.get("sha256"), str)
    ):
        raise EditorialPublishingError("editorial image preview is unavailable")
    path = (
        data_root
        / "publishing"
        / "publication-plans"
        / plan_id
        / "image.png"
    )
    try:
        _reject_symlink_ancestry(path)
    except ResumeWorkspaceStorageError as exc:
        raise EditorialPublishingError("editorial image preview is unsafe") from exc
    raw = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
    if raw[:8] != _PNG_SIGNATURE or hashlib.sha256(raw).hexdigest() != image["sha256"]:
        raise EditorialPublishingError("editorial image preview does not match the plan")
    return raw


def _load_package(day_directory: Path) -> dict[str, object]:
    day_root = day_directory.expanduser().absolute()
    batch_root = day_root.parent
    try:
        _reject_symlink_ancestry(day_root)
    except ResumeWorkspaceStorageError as exc:
        raise EditorialPublishingError("editorial package ancestry cannot contain a symlink") from exc
    if not day_root.is_dir() or not re.fullmatch(r"day-[0-9]{2}", day_root.name):
        raise EditorialPublishingError("select a regular finalized day-01 through day-31 directory")
    receipt_path = day_root / "receipt.json"
    final_path = batch_root / "final-validation-receipt.json"
    week_path = batch_root / "week-receipt.json"
    receipt = _read_object(receipt_path, "day receipt")
    final = _read_object(final_path, "final-validation receipt")
    week = _read_object(week_path, "week receipt")
    _verify_day_receipt(day_root, receipt)
    _verify_batch_receipts(day_root, receipt_path, final_path, week_path, final, week)
    linkedin = _read_text(day_root / "linkedin.md", "LinkedIn text")
    x_parts = _parse_x_thread(_read_text(day_root / "x-thread.md", "X thread"))
    image_path, width, height = _single_png(day_root / "visual")
    alt_text = _read_text(day_root / "visual" / "alt-text.md", "image alt text").rstrip("\n")
    if not alt_text.strip():
        raise EditorialPublishingError("image alt text must be nonblank")
    try:
        validate_public_editorial_text(linkedin)
        for part in x_parts:
            validate_public_editorial_text(part)
        validate_public_editorial_text(alt_text)
    except ValueError as exc:
        raise EditorialPublishingError(
            "publication text contains a private path or credential-like value"
        ) from exc
    required_artifacts = {
        "linkedin.md",
        "x-thread.md",
        "visual/alt-text.md",
        f"visual/{image_path.name}",
    }
    receipt_artifacts = receipt.get("artifacts")
    if not isinstance(receipt_artifacts, dict) or not required_artifacts <= set(receipt_artifacts):
        raise EditorialPublishingError("day receipt does not hash every publication source artifact")
    package_id = receipt.get("package_id")
    if not isinstance(package_id, str) or not package_id:
        raise EditorialPublishingError("day receipt package ID is invalid")
    return {
        "package_id": package_id,
        "linkedin": linkedin.rstrip("\n"),
        "x_parts": x_parts,
        "image_path": image_path,
        "image_width": width,
        "image_height": height,
        "alt_text": alt_text,
        "final_validation_hash": _sha256(final_path),
    }


def _verify_day_receipt(day_root: Path, receipt: dict[str, object]) -> None:
    if receipt.get("publication_performed") is not False or receipt.get("human_gate_required") is not True:
        raise EditorialPublishingError("day receipt is not a sealed human-gated package")
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, dict):
        raise EditorialPublishingError("day receipt artifacts are invalid")
    for name, expected in artifacts.items():
        if not isinstance(name, str) or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise EditorialPublishingError("day receipt artifact hash is invalid")
        relative = Path(name)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise EditorialPublishingError("day receipt artifact path is unsafe")
        artifact = day_root / relative
        if not artifact.is_relative_to(day_root) or artifact.is_symlink() or not artifact.is_file():
            raise EditorialPublishingError("day receipt artifact is unavailable or unsafe")
        if _sha256(artifact) != expected:
            raise EditorialPublishingError("day receipt artifact hash does not match")


def _verify_batch_receipts(
    day_root: Path, receipt_path: Path, final_path: Path, week_path: Path,
    final: dict[str, object], week: dict[str, object],
) -> None:
    if final.get("status") != "READY_FOR_HUMAN_PUBLICATION" or final.get("publication_performed") is not False:
        raise EditorialPublishingError("final-validation receipt is not publication-ready")
    if final.get("human_gate_required") is not True:
        raise EditorialPublishingError("final-validation receipt does not require a human gate")
    if final.get("week_receipt_sha256") != _sha256(week_path):
        raise EditorialPublishingError("final-validation receipt does not match the sealed week receipt")
    post_review = day_root / "post-revision-review.json"
    if final.get("post_revision_review_sha256") != _sha256(post_review):
        raise EditorialPublishingError("final-validation receipt does not match the selected day review")
    day_receipts = week.get("day_receipts")
    if not isinstance(day_receipts, dict) or day_receipts.get(f"{day_root.name}/receipt.json") != _sha256(receipt_path):
        raise EditorialPublishingError("week receipt does not match the selected day receipt")


def _parse_x_thread(text: str) -> list[str]:
    markers = list(_X_MARKER.finditer(text))
    if not markers or text[: markers[0].start()].strip():
        raise EditorialPublishingError("X thread must begin with an anchored N/Total marker")
    total = int(markers[0].group("total"))
    if total > 12 or len(markers) != total:
        raise EditorialPublishingError("X thread must contain 1 through N anchored parts, with N at most 12")
    parts: list[str] = []
    for index, marker in enumerate(markers, start=1):
        if int(marker.group("number")) != index or int(marker.group("total")) != total:
            raise EditorialPublishingError("X thread markers must be consecutive and use one common total")
        end = markers[index].start() if index < len(markers) else len(text)
        part = text[marker.start() : end].rstrip("\n")
        if not part.strip():
            raise EditorialPublishingError("X thread parts must be nonblank")
        parts.append(part)
    return parts


def _single_png(visual_root: Path) -> tuple[Path, int, int]:
    if visual_root.is_symlink() or not visual_root.is_dir():
        raise EditorialPublishingError("visual package is unavailable or unsafe")
    images = [path for path in visual_root.iterdir() if path.suffix.lower() == ".png"]
    if len(images) != 1 or images[0].is_symlink() or not images[0].is_file():
        raise EditorialPublishingError("editorial package must contain exactly one regular PNG")
    raw = images[0].read_bytes()
    if raw[:8] != _PNG_SIGNATURE or raw[12:16] != b"IHDR" or len(raw) < 24:
        raise EditorialPublishingError("editorial image is not a valid PNG")
    width, height = struct.unpack(">II", raw[16:24])
    if width < 1 or height < 1:
        raise EditorialPublishingError("editorial PNG dimensions are invalid")
    return images[0], width, height


def _read_object(path: Path, label: str) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise EditorialPublishingError(f"{label} is unavailable or unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EditorialPublishingError(f"{label} is invalid") from exc
    if not isinstance(value, dict):
        raise EditorialPublishingError(f"{label} is invalid")
    return cast(dict[str, object], value)


def _read_text(path: Path, label: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise EditorialPublishingError(f"{label} is unavailable or unsafe")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise EditorialPublishingError(f"{label} is unreadable") from exc


def _sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise EditorialPublishingError("sealed editorial artifact is unavailable or unsafe")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _model_dump(value: object) -> dict[str, object]:
    dumped = value.model_dump(mode="json") if hasattr(value, "model_dump") else vars(value)
    if not isinstance(dumped, dict):
        raise EditorialPublishingError("BuildLog returned an invalid publication-plan record")
    return cast(dict[str, object], dumped)


def _records_root(data_root: Path) -> Path:
    root = data_root / "editorial-publishing"
    try:
        _reject_symlink_ancestry(root)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            root.chmod(0o700)
    except (OSError, ResumeWorkspaceStorageError) as exc:
        raise EditorialPublishingError("private editorial preview storage is unavailable") from exc
    return root


def _preview_path(data_root: Path, channel: EditorialChannel) -> Path:
    return _records_root(data_root) / f"{channel}-preview.json"


def _receipt_path(data_root: Path, channel: EditorialChannel) -> Path:
    return _records_root(data_root) / f"{channel}-receipt.json"


def _write_preview(data_root: Path, channel: EditorialChannel, record: dict[str, object]) -> None:
    _write_record(_preview_path(data_root, channel), record)


def _load_preview(data_root: Path, channel: EditorialChannel) -> dict[str, object]:
    return _read_object(_preview_path(data_root, channel), "editorial publication preview")


def _write_receipt(data_root: Path, channel: EditorialChannel, record: dict[str, object]) -> None:
    _write_record(_receipt_path(data_root, channel), record)


def _write_record(path: Path, record: dict[str, object]) -> None:
    try:
        _atomic_private_write(path, json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    except (OSError, ResumeWorkspaceStorageError) as exc:
        raise EditorialPublishingError("editorial publication record could not be saved") from exc


def _optional_record(path: Path) -> dict[str, object] | None:
    if not path.is_file() or path.is_symlink():
        return None
    return _read_object(path, "editorial publication record")
