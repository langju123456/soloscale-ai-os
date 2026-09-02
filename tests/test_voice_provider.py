import hashlib
import json
from pathlib import Path

import pytest

from soloscale.content_models import (
    ClaimStatus,
    ContentBrief,
    ContentClaim,
)
from soloscale.content_workspace import run_content_workspace
from soloscale.media_profile import MediaProfile, save_media_profile
from soloscale.voice_provider import VoiceProviderError, create_narration_assets


def _run(tmp_path: Path):
    return run_content_workspace(
        data_root=tmp_path,
        brief=ContentBrief(
            topic="Grounded media",
            audience="AI builders",
            language="中文",
            call_to_action="继续验证。",
            source_label="git:test",
            claims=[
                ContentClaim(
                    id="CLAIM-01",
                    text="本地视频已通过后台任务生成。",
                    status=ClaimStatus.VERIFIED,
                    receipt="git:test",
                )
            ],
        ),
    )


def test_qwen_voice_is_explicit_and_never_silently_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "data"
    voice_dir = data_root / "media" / "voice"
    voice_dir.mkdir(parents=True)
    audio = voice_dir / "reference.wav"
    transcript = voice_dir / "reference.txt"
    audio.write_bytes(b"RIFF-safe-wave")
    transcript.write_text("This is the approved reference voice transcript.\n")
    runtime_python = data_root / "media-runtime" / "venv" / "bin" / "python"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_text("#!/bin/sh\n")
    runtime_python.chmod(0o700)
    resource_root = tmp_path / "resources"
    worker = resource_root / "media_runtime" / "qwen_mlx_worker.py"
    worker.parent.mkdir(parents=True)
    worker.write_text("# worker\n")
    save_media_profile(
        data_root,
        MediaProfile(
            reference_audio_path="media/voice/reference.wav",
            reference_audio_sha256=hashlib.sha256(audio.read_bytes()).hexdigest(),
            reference_text_path="media/voice/reference.txt",
            reference_text_sha256=hashlib.sha256(transcript.read_bytes()).hexdigest(),
        ),
    )
    run = _run(data_root)
    public_dir = tmp_path / "public"
    public_dir.mkdir()

    def complete(command: list[str], **_: object) -> object:
        request_path = Path(command[command.index("--request") + 1])
        response_path = Path(command[command.index("--response") + 1])
        request = json.loads(request_path.read_text())
        for item in request["items"]:
            Path(item["output_path"]).write_bytes(b"RIFF" + b"audio" * 20)
        response_path.write_text(
            json.dumps({"status": "complete", "count": len(request["items"])})
        )
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("soloscale.voice_provider.subprocess.run", complete)
    result = create_narration_assets(
        run=run,
        public_dir=public_dir,
        avatar_assets={},
        data_root=data_root,
        resource_root=resource_root,
    )
    assert result.provider == "qwen3_tts_mlx"
    assert result.locale == "zh-CN"
    assert len(result.assets) == len(run.drafts.storyboard)

    monkeypatch.setattr(
        "soloscale.voice_provider.subprocess.run",
        lambda *args, **kwargs: type(
            "Result", (), {"returncode": 1, "stdout": "", "stderr": "failed"}
        )(),
    )
    for path in public_dir.glob("*.wav"):
        path.unlink()
    with pytest.raises(VoiceProviderError, match="Qwen narration failed"):
        create_narration_assets(
            run=run,
            public_dir=public_dir,
            avatar_assets={},
            data_root=data_root,
            resource_root=resource_root,
        )
