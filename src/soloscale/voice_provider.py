"""Explicit local narration providers for Creator Video."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from soloscale.content_models import ContentRun
from soloscale.media_profile import (
    MediaProfile,
    MediaProfileError,
    VoiceProviderId,
    load_media_profile,
    media_runtime_root,
    resolved_voice_assets,
)


class VoiceProviderError(ValueError):
    """Raised when the explicitly selected narration provider cannot complete."""


@dataclass(frozen=True)
class NarrationResult:
    assets: dict[str, str]
    provider: str
    model: str
    locale: str
    reference_audio_sha256: str | None


def _normal_scene_text(text: str) -> str:
    return " ".join(text.split()).strip()


def _qwen_narration(
    *,
    run: ContentRun,
    public_dir: Path,
    avatar_assets: dict[str, str],
    data_root: Path,
    resource_root: Path,
    profile: MediaProfile,
) -> NarrationResult:
    runtime = media_runtime_root(data_root)
    python = runtime / "venv" / "bin" / "python"
    worker = resource_root / "media_runtime" / "qwen_mlx_worker.py"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise VoiceProviderError(
            "Local Qwen voice runtime is unavailable. Reinstall Media Runtime in Settings."
        )
    if worker.is_symlink() or not worker.is_file():
        raise VoiceProviderError("The packaged Qwen voice worker is unavailable")
    try:
        reference_audio, reference_text_path = resolved_voice_assets(data_root, profile)
        reference_text = reference_text_path.read_text(encoding="utf-8").strip()
    except (MediaProfileError, OSError, UnicodeError) as exc:
        raise VoiceProviderError(str(exc)) from exc
    if not reference_text:
        raise VoiceProviderError("Your voice reference transcript is empty")
    locale = "zh-CN" if run.brief.language == "中文" else "en-US"
    requests: list[dict[str, str]] = []
    assets: dict[str, str] = {}
    for scene in run.drafts.storyboard:
        if scene.id in avatar_assets:
            continue
        identity = hashlib.sha256(scene.id.encode("utf-8")).hexdigest()[:12]
        filename = f"narration-{identity}.wav"
        assets[scene.id] = filename
        requests.append(
            {
                "scene_id": scene.id,
                "text": _normal_scene_text(scene.voiceover),
                "output_path": str(public_dir / filename),
            }
        )
    if not requests:
        return NarrationResult(
            assets={},
            provider=profile.voice_provider.value,
            model=profile.tts_model,
            locale=locale,
            reference_audio_sha256=profile.reference_audio_sha256,
        )
    request = {
        "schema_version": "1.0",
        "model": profile.tts_model,
        "locale": locale,
        "reference_audio": str(reference_audio),
        "reference_text": reference_text,
        "items": requests,
    }
    environment = os.environ.copy()
    model_cache = runtime / "models"
    model_cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    environment["HF_HOME"] = str(model_cache)
    environment["HF_HUB_CACHE"] = str(model_cache / "hub")
    with tempfile.TemporaryDirectory(prefix="soloscale-qwen-voice-") as raw_temp:
        temp = Path(raw_temp)
        request_path = temp / "request.json"
        response_path = temp / "response.json"
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        os.chmod(request_path, 0o600)
        try:
            completed = subprocess.run(
                [
                    str(python),
                    str(worker),
                    "narrate",
                    "--request",
                    str(request_path),
                    "--response",
                    str(response_path),
                ],
                capture_output=True,
                check=False,
                text=True,
                timeout=1_800,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise VoiceProviderError("Local Qwen narration timed out") from exc
        if completed.returncode != 0 or not response_path.is_file():
            raise VoiceProviderError("Local Qwen narration failed")
        try:
            response = json.loads(response_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise VoiceProviderError("Local Qwen narration returned an invalid receipt") from exc
        if response.get("status") != "complete" or response.get("count") != len(requests):
            raise VoiceProviderError("Local Qwen narration did not complete every scene")
    for filename in assets.values():
        output = public_dir / filename
        if output.is_symlink() or not output.is_file() or output.stat().st_size <= 44:
            raise VoiceProviderError("Local Qwen narration produced an invalid audio file")
        os.chmod(output, 0o600)
    return NarrationResult(
        assets=assets,
        provider=profile.voice_provider.value,
        model=profile.tts_model,
        locale=locale,
        reference_audio_sha256=profile.reference_audio_sha256,
    )


def _macos_narration(
    *,
    run: ContentRun,
    public_dir: Path,
    avatar_assets: dict[str, str],
) -> NarrationResult:
    say = shutil.which("say")
    afconvert = shutil.which("afconvert")
    if say is None or afconvert is None:
        raise VoiceProviderError("The explicitly selected macOS voice is unavailable")
    locale = "zh-CN" if run.brief.language == "中文" else "en-US"
    voice = "Tingting" if locale == "zh-CN" else "Samantha"
    assets: dict[str, str] = {}
    for scene in run.drafts.storyboard:
        if scene.id in avatar_assets:
            continue
        identity = hashlib.sha256(scene.id.encode("utf-8")).hexdigest()[:12]
        aiff = public_dir / f"narration-{identity}.aiff"
        filename = f"narration-{identity}.wav"
        output = public_dir / filename
        spoken = subprocess.run(
            [say, "-v", voice, "-r", "190", "-o", str(aiff), scene.voiceover],
            capture_output=True,
            check=False,
            timeout=60,
        )
        converted = subprocess.run(
            [afconvert, "-f", "WAVE", "-d", "LEI16@22050", str(aiff), str(output)],
            capture_output=True,
            check=False,
            timeout=30,
        )
        aiff.unlink(missing_ok=True)
        if (
            spoken.returncode != 0
            or converted.returncode != 0
            or not output.is_file()
            or output.stat().st_size <= 44
        ):
            raise VoiceProviderError("The explicitly selected macOS voice failed")
        os.chmod(output, 0o600)
        assets[scene.id] = filename
    return NarrationResult(
        assets=assets,
        provider=VoiceProviderId.MACOS_SYSTEM.value,
        model=voice,
        locale=locale,
        reference_audio_sha256=None,
    )


def create_narration_assets(
    *,
    run: ContentRun,
    public_dir: Path,
    avatar_assets: dict[str, str],
    data_root: Path,
    resource_root: Path,
) -> NarrationResult:
    """Use only the provider explicitly selected in the private media profile."""

    try:
        profile = load_media_profile(data_root)
    except MediaProfileError as exc:
        raise VoiceProviderError(str(exc)) from exc
    if profile.voice_provider is VoiceProviderId.QWEN3_TTS_MLX:
        return _qwen_narration(
            run=run,
            public_dir=public_dir,
            avatar_assets=avatar_assets,
            data_root=data_root,
            resource_root=resource_root,
            profile=profile,
        )
    if profile.voice_provider is VoiceProviderId.MACOS_SYSTEM:
        return _macos_narration(
            run=run,
            public_dir=public_dir,
            avatar_assets=avatar_assets,
        )
    raise VoiceProviderError("The selected voice provider is unsupported")
