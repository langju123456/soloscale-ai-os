"""Isolated MLX Audio worker. User text never goes to stdout or logs."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def _regular_input(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is unavailable")
    return path


def _private_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _transcribe(audio: Path, output: Path, model_id: str, language: str) -> None:
    from mlx_audio.stt.utils import load_model

    model = load_model(model_id)
    result = model.generate(
        str(_regular_input(audio, "Audio")),
        language=language,
        temperature=0.0,
        verbose=False,
    )
    text = " ".join(str(result.text).split()).strip()
    if not text:
        raise ValueError("Transcription was empty")
    output.write_text(text + "\n", encoding="utf-8")
    os.chmod(output, 0o600)


def _narrate(request_path: Path, response_path: Path) -> None:
    import mlx.core as mx
    from mlx_audio.audio_io import write as audio_write
    from mlx_audio.tts.utils import load_model

    request = json.loads(_regular_input(request_path, "Request").read_text(encoding="utf-8"))
    if request.get("schema_version") != "1.0":
        raise ValueError("Unsupported request schema")
    model_id = str(request["model"])
    locale = str(request["locale"])
    lang_code = "chinese" if locale == "zh-CN" else "english"
    reference_audio = _regular_input(Path(str(request["reference_audio"])), "Reference audio")
    reference_text = " ".join(str(request["reference_text"]).split()).strip()
    items = request.get("items")
    if not reference_text or not isinstance(items, list) or not items:
        raise ValueError("Narration request is incomplete")
    model = load_model(model_id)
    completed = 0
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError("Narration item is invalid")
        text = " ".join(str(item.get("text", "")).split()).strip()
        output = Path(str(item.get("output_path", "")))
        if not text or output.suffix.casefold() != ".wav" or output.is_symlink():
            raise ValueError("Narration item is invalid")
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        mx.random.seed(7_000 + index)
        chunks = [
            result.audio
            for result in model.generate(
                text=text,
                lang_code=lang_code,
                ref_audio=str(reference_audio),
                ref_text=reference_text,
                temperature=0.7,
                top_k=40,
                top_p=0.95,
                max_tokens=3_072,
                verbose=False,
            )
        ]
        if not chunks:
            raise ValueError("Narration produced no audio")
        audio = chunks[0] if len(chunks) == 1 else mx.concatenate(chunks)
        audio_write(str(output), audio, model.sample_rate, format="wav")
        os.chmod(output, 0o600)
        completed += 1
        mx.clear_cache()
    _private_json(
        response_path,
        {"schema_version": "1.0", "status": "complete", "count": completed},
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    transcribe = subparsers.add_parser("transcribe")
    transcribe.add_argument("--audio", type=Path, required=True)
    transcribe.add_argument("--output", type=Path, required=True)
    transcribe.add_argument("--model", required=True)
    transcribe.add_argument("--language", default="en")
    narrate = subparsers.add_parser("narrate")
    narrate.add_argument("--request", type=Path, required=True)
    narrate.add_argument("--response", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "transcribe":
        _transcribe(args.audio, args.output, args.model, args.language)
    else:
        _narrate(args.request, args.response)


if __name__ == "__main__":
    main()
