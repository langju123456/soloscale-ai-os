"""Private, user-owned media identity and local runtime configuration."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from soloscale.resume_workspace import ResumeWorkspaceStorageError, _atomic_private_write


class MediaProfileError(ValueError):
    """Raised when the selected private media profile cannot be used safely."""


class VoiceProviderId(StrEnum):
    QWEN3_TTS_MLX = "qwen3_tts_mlx"
    MACOS_SYSTEM = "macos_system"


class MediaProfile(BaseModel):
    """Persisted configuration; raw voice text and credentials are not embedded."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    voice_provider: VoiceProviderId = VoiceProviderId.QWEN3_TTS_MLX
    tts_model: str = "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit"
    asr_model: str = "mlx-community/Qwen3-ASR-0.6B-8bit"
    reference_audio_path: str | None = None
    reference_audio_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    reference_text_path: str | None = None
    reference_text_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    supported_locales: list[str] = Field(
        default_factory=lambda: ["zh-CN", "en-US"], min_length=1, max_length=8
    )
    heygen_avatar_group_id: str | None = None
    heygen_avatar_look_id: str | None = None
    heygen_voice_id: str | None = None
    heygen_zh_voice_id: str | None = None
    heygen_en_voice_id: str | None = None


def media_profile_path(data_root: Path) -> Path:
    return data_root / "media" / "profile.json"


def media_runtime_root(data_root: Path) -> Path:
    return data_root / "media-runtime"


def _private_regular_file(path: Path, *, label: str) -> Path:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise MediaProfileError(f"{label} is missing") from exc
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise MediaProfileError(f"{label} is unsafe")
    return path


def _resolve_private_profile_path(data_root: Path, relative: str, *, label: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise MediaProfileError(f"{label} path is unsafe")
    root = data_root.resolve(strict=False)
    resolved = (root / candidate).resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise MediaProfileError(f"{label} path escapes private storage")
    return _private_regular_file(resolved, label=label)


def load_media_profile(data_root: Path) -> MediaProfile:
    profile = load_media_profile_settings(data_root)
    path = media_profile_path(data_root)
    _private_regular_file(path, label="Media profile")
    if profile.voice_provider is VoiceProviderId.QWEN3_TTS_MLX:
        if not profile.reference_audio_path or not profile.reference_text_path:
            raise MediaProfileError("Your local Qwen voice is not configured")
        audio = _resolve_private_profile_path(
            data_root, profile.reference_audio_path, label="Voice reference audio"
        )
        transcript = _resolve_private_profile_path(
            data_root, profile.reference_text_path, label="Voice reference transcript"
        )
        if hashlib.sha256(audio.read_bytes()).hexdigest() != profile.reference_audio_sha256:
            raise MediaProfileError("Voice reference audio no longer matches its profile")
        if (
            hashlib.sha256(transcript.read_bytes()).hexdigest()
            != profile.reference_text_sha256
        ):
            raise MediaProfileError("Voice reference transcript no longer matches its profile")
    return profile


def load_media_profile_settings(data_root: Path) -> MediaProfile:
    """Load configuration without requiring optional voice assets to be installed."""

    path = media_profile_path(data_root)
    if not path.exists():
        return MediaProfile()
    _private_regular_file(path, label="Media profile")
    try:
        profile = MediaProfile.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise MediaProfileError("Media profile is invalid") from exc
    return profile


def save_media_profile(data_root: Path, profile: MediaProfile) -> Path:
    path = media_profile_path(data_root)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        _atomic_private_write(
            path,
            json.dumps(profile.model_dump(mode="json"), ensure_ascii=False, indent=2)
            + "\n",
        )
    except (OSError, ResumeWorkspaceStorageError) as exc:
        raise MediaProfileError("Could not save the private media profile") from exc
    return path


def resolved_voice_assets(data_root: Path, profile: MediaProfile) -> tuple[Path, Path]:
    if not profile.reference_audio_path or not profile.reference_text_path:
        raise MediaProfileError("Your local Qwen voice is not configured")
    return (
        _resolve_private_profile_path(
            data_root, profile.reference_audio_path, label="Voice reference audio"
        ),
        _resolve_private_profile_path(
            data_root, profile.reference_text_path, label="Voice reference transcript"
        ),
    )
