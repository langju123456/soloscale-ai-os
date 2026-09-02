"""Bounded HeyGen avatar-segment provider with no implicit external calls."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import stat
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from soloscale.media_cost import (
    BudgetBlockedError,
    PaidOperationAuthorization,
    validate_paid_authorization,
)
from soloscale.resume_workspace import ResumeWorkspaceStorageError, _atomic_private_write

_MAX_AUDIO_BYTES = 25 * 1024 * 1024
_MAX_RESPONSE_BYTES = 512 * 1024
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,160}$")


class HeyGenProviderError(ValueError):
    """Raised when an explicitly approved avatar-segment request is unsafe or fails."""


class AvatarAspectRatio(StrEnum):
    LANDSCAPE = "16:9"
    PORTRAIT = "9:16"


class AvatarSegmentRequest(BaseModel):
    """Private request contract; the API credential and audio bytes are never serialized."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    scene_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,120}$")
    locale: str = Field(pattern=r"^(zh-CN|en-US)$")
    aspect_ratio: AvatarAspectRatio
    avatar_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,160}$")
    avatar_style: str = Field(default="normal", pattern=r"^[A-Za-z0-9_-]{1,40}$")
    audio_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    audio_bytes: int = Field(gt=44, le=_MAX_AUDIO_BYTES)
    duration_seconds: float = Field(gt=0, le=600)


class AvatarSegmentSubmission(BaseModel):
    """Body-free provider receipt safe to retain in the private run directory."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    provider: str = "heygen"
    scene_id: str
    locale: str
    aspect_ratio: AvatarAspectRatio
    audio_sha256: str
    provider_audio_asset_id: str
    provider_request_id: str
    status: str = "submitted"
    publication_performed: bool = False


@dataclass(frozen=True)
class HeyGenUpload:
    asset_id: str


@dataclass(frozen=True)
class HeyGenSubmission:
    request_id: str


class HeyGenTransport(Protocol):
    def upload_audio(
        self, *, credential: str, content: bytes, content_type: str
    ) -> HeyGenUpload: ...

    def submit_avatar_video(
        self, *, credential: str, payload: dict[str, object]
    ) -> HeyGenSubmission: ...


class HeyGenHTTPTransport:
    """Small official-API transport; callers own budget approval and polling."""

    def __init__(
        self,
        *,
        upload_endpoint: str = "https://upload.heygen.com/v1/asset",
        generate_endpoint: str = "https://api.heygen.com/v2/video/generate",
        timeout_seconds: int = 60,
    ) -> None:
        self._upload_endpoint = upload_endpoint
        self._generate_endpoint = generate_endpoint
        self._timeout_seconds = timeout_seconds

    def _send(self, request: urllib.request.Request) -> dict[str, object]:
        try:
            with urllib.request.urlopen(  # noqa: S310 - fixed HTTPS provider endpoints
                request, timeout=self._timeout_seconds
            ) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            raise HeyGenProviderError("HeyGen request failed") from exc
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise HeyGenProviderError("HeyGen response exceeded its size limit")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HeyGenProviderError("HeyGen returned an invalid response") from exc
        if not isinstance(decoded, dict):
            raise HeyGenProviderError("HeyGen returned an invalid response")
        return cast("dict[str, object]", decoded)

    @staticmethod
    def _identifier(envelope: dict[str, object], key: str) -> str:
        data = envelope.get("data")
        value = data.get(key) if isinstance(data, dict) else None
        if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
            raise HeyGenProviderError("HeyGen returned an invalid response")
        return value

    def upload_audio(
        self, *, credential: str, content: bytes, content_type: str
    ) -> HeyGenUpload:
        envelope = self._send(
            urllib.request.Request(
                self._upload_endpoint,
                data=content,
                headers={
                    "X-Api-Key": credential,
                    "Accept": "application/json",
                    "Content-Type": content_type,
                    "User-Agent": "SoloScale-Desktop/0.4",
                },
                method="POST",
            )
        )
        return HeyGenUpload(asset_id=self._identifier(envelope, "id"))

    def submit_avatar_video(
        self, *, credential: str, payload: dict[str, object]
    ) -> HeyGenSubmission:
        envelope = self._send(
            urllib.request.Request(
                self._generate_endpoint,
                data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                headers={
                    "X-Api-Key": credential,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "SoloScale-Desktop/0.4",
                },
                method="POST",
            )
        )
        return HeyGenSubmission(request_id=self._identifier(envelope, "video_id"))


def _audio_file(path: Path) -> tuple[bytes, str]:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise HeyGenProviderError("Avatar audio is missing") from exc
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise HeyGenProviderError("Avatar audio is unsafe")
    if metadata.st_size <= 44 or metadata.st_size > _MAX_AUDIO_BYTES:
        raise HeyGenProviderError("Avatar audio size is invalid")
    content_type = mimetypes.guess_type(path.name)[0]
    if content_type not in {"audio/mpeg", "audio/wav", "audio/x-wav", "audio/mp4"}:
        raise HeyGenProviderError("Avatar audio format is unsupported")
    return path.read_bytes(), content_type


def preview_avatar_segment_request(
    *,
    scene_id: str,
    audio_path: Path,
    locale: str,
    aspect_ratio: AvatarAspectRatio,
    avatar_id: str,
    duration_seconds: float,
) -> AvatarSegmentRequest:
    """Build the exact body-free preview shown before any paid provider call."""

    content, _ = _audio_file(audio_path)
    return AvatarSegmentRequest(
        scene_id=scene_id,
        locale=locale,
        aspect_ratio=aspect_ratio,
        avatar_id=avatar_id,
        audio_sha256=hashlib.sha256(content).hexdigest(),
        audio_bytes=len(content),
        duration_seconds=duration_seconds,
    )


def generate_segment(
    *,
    request: AvatarSegmentRequest,
    audio_path: Path,
    credential: str,
    authorization: PaidOperationAuthorization,
    transport: HeyGenTransport | None = None,
) -> AvatarSegmentSubmission:
    """Submit one explicitly approved segment; never retries or polls implicitly."""

    try:
        validate_paid_authorization(
            authorization=authorization,
            operation="heygen.avatar_segment",
            subject=request,
        )
    except BudgetBlockedError as exc:
        raise HeyGenProviderError("HeyGen request has no valid budget authorization") from exc
    if not credential or credential != credential.strip():
        raise HeyGenProviderError("HeyGen is not configured")
    content, content_type = _audio_file(audio_path)
    if hashlib.sha256(content).hexdigest() != request.audio_sha256:
        raise HeyGenProviderError("Avatar audio changed after cost preview")
    if len(content) != request.audio_bytes:
        raise HeyGenProviderError("Avatar audio changed after cost preview")
    selected_transport = transport or HeyGenHTTPTransport()
    upload = selected_transport.upload_audio(
        credential=credential,
        content=content,
        content_type=content_type,
    )
    width, height = (
        (1920, 1080)
        if request.aspect_ratio is AvatarAspectRatio.LANDSCAPE
        else (1080, 1920)
    )
    submission = selected_transport.submit_avatar_video(
        credential=credential,
        payload={
            "video_inputs": [
                {
                    "character": {
                        "type": "avatar",
                        "avatar_id": request.avatar_id,
                        "avatar_style": request.avatar_style,
                    },
                    "voice": {
                        "type": "audio",
                        "audio_asset_id": upload.asset_id,
                    },
                }
            ],
            "dimension": {"width": width, "height": height},
            "caption": False,
            "test": False,
        },
    )
    return AvatarSegmentSubmission(
        scene_id=request.scene_id,
        locale=request.locale,
        aspect_ratio=request.aspect_ratio,
        audio_sha256=request.audio_sha256,
        provider_audio_asset_id=upload.asset_id,
        provider_request_id=submission.request_id,
    )


def save_avatar_submission_receipt(
    *, data_root: Path, run_id: str, receipt: AvatarSegmentSubmission
) -> Path:
    """Persist only body-free provider metadata under the private data root."""

    if re.fullmatch(r"content-[A-Za-z0-9_-]{1,100}", run_id) is None:
        raise HeyGenProviderError("Avatar run ID is invalid")
    root = data_root.resolve(strict=False)
    folder = (root / "media" / "heygen" / run_id).resolve(strict=False)
    if root not in folder.parents:
        raise HeyGenProviderError("Avatar receipt path is unsafe")
    path = folder / f"{receipt.scene_id}-{receipt.aspect_ratio.value.replace(':', 'x')}.json"
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        _atomic_private_write(
            path,
            json.dumps(receipt.model_dump(mode="json"), sort_keys=True, indent=2) + "\n",
        )
    except (OSError, ResumeWorkspaceStorageError) as exc:
        raise HeyGenProviderError("Could not save the HeyGen receipt") from exc
    return path
