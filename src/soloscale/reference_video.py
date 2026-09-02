"""Private local-video reference analysis for reusable presentation grammar."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Literal

from PIL import Image, ImageChops, ImageStat

from soloscale.media_profile import MediaProfileError, load_media_profile, media_runtime_root
from soloscale.reference_intelligence import (
    ContentPattern,
    ReferenceAsset,
    ReferenceSourceKind,
    ReferenceVideoPattern,
    extract_content_pattern,
)
from soloscale.resume_workspace import ResumeWorkspaceStorageError, _atomic_private_write

MAX_REFERENCE_VIDEO_BYTES = 200 * 1024 * 1024
_SHOT_CHANGE_THRESHOLD = 0.18
_SHOT_SAMPLE_LIMIT = 240


class ReferenceVideoError(ValueError):
    """Raised when a selected local reference MP4 cannot be analyzed safely."""


@dataclass(frozen=True)
class ReferenceVideoAnalysis:
    asset: ReferenceAsset
    pattern: ContentPattern
    library_path: Path


def reference_library_root(data_root: Path) -> Path:
    return data_root / "reference-library"


def _library_dir(data_root: Path, reference_id: str) -> Path:
    if re.fullmatch(r"reference-[a-f0-9]{16}", reference_id) is None:
        raise ReferenceVideoError("Reference ID is invalid")
    return reference_library_root(data_root) / reference_id


def _load_json(path: Path) -> dict[str, object]:
    try:
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise ReferenceVideoError("Reference library record is unsafe")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReferenceVideoError("Reference library record is unavailable") from exc
    if not isinstance(value, dict):
        raise ReferenceVideoError("Reference library record is invalid")
    return value


def load_reference_video(
    data_root: Path, reference_id: str
) -> ReferenceVideoAnalysis:
    directory = _library_dir(data_root, reference_id)
    asset = ReferenceAsset.model_validate(_load_json(directory / "asset.json"))
    pattern = ContentPattern.model_validate(_load_json(directory / "pattern.json"))
    if asset.source_kind is not ReferenceSourceKind.LOCAL_VIDEO:
        raise ReferenceVideoError("Selected reference is not a local video")
    if pattern.reference_id != asset.reference_id:
        raise ReferenceVideoError("Reference pattern does not belong to this video")
    return ReferenceVideoAnalysis(asset=asset, pattern=pattern, library_path=directory)


def recent_reference_videos(
    data_root: Path, *, limit: int = 8
) -> list[ReferenceVideoAnalysis]:
    root = reference_library_root(data_root)
    if not root.is_dir() or root.is_symlink():
        return []
    results: list[ReferenceVideoAnalysis] = []
    try:
        directories = sorted(
            root.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True
        )
    except OSError:
        return []
    for directory in directories:
        try:
            results.append(load_reference_video(data_root, directory.name))
        except (OSError, ReferenceVideoError, ValueError):
            continue
        if len(results) >= limit:
            break
    return results


def _media_tools(resource_root: Path) -> tuple[Path, Path, dict[str, str]]:
    compositor = (
        resource_root
        / "video_factory"
        / "node_modules"
        / "@remotion"
        / "compositor-darwin-arm64"
    )
    ffmpeg = compositor / "ffmpeg"
    ffprobe = compositor / "ffprobe"
    if not ffmpeg.is_file() or not ffprobe.is_file():
        raise ReferenceVideoError("The packaged local media analyzer is unavailable")
    environment = os.environ.copy()
    existing = environment.get("DYLD_LIBRARY_PATH")
    environment["DYLD_LIBRARY_PATH"] = (
        f"{compositor}:{existing}" if existing else str(compositor)
    )
    return ffmpeg, ffprobe, environment


def _run(
    command: list[str], *, environment: dict[str, str], timeout: int
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise ReferenceVideoError("Local reference video analysis timed out") from exc


def _fraction(value: object) -> float:
    try:
        return float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _probe(
    source: Path, *, ffprobe: Path, environment: dict[str, str]
) -> tuple[float, int, int, float, bool]:
    completed = _run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(source),
        ],
        environment=environment,
        timeout=60,
    )
    if completed.returncode != 0:
        raise ReferenceVideoError("The selected file is not a readable MP4")
    try:
        payload = json.loads(completed.stdout)
        streams = payload.get("streams", [])
        video = next(item for item in streams if item.get("codec_type") == "video")
        format_data = payload.get("format", {})
        duration = float(video.get("duration") or format_data.get("duration"))
        width = int(video["width"])
        height = int(video["height"])
        fps = _fraction(video.get("avg_frame_rate") or video.get("r_frame_rate"))
        has_audio = any(item.get("codec_type") == "audio" for item in streams)
    except (KeyError, StopIteration, TypeError, ValueError) as exc:
        raise ReferenceVideoError("The selected MP4 has no usable video stream") from exc
    if not 0 < duration <= 3_600 or not width or not height or not fps:
        raise ReferenceVideoError("The selected MP4 metadata is unsupported")
    return duration, width, height, fps, has_audio


def _shot_times(
    source: Path,
    *,
    duration: float,
    ffmpeg: Path,
    environment: dict[str, str],
) -> list[float]:
    sample_rate = min(2.0, _SHOT_SAMPLE_LIMIT / duration)
    temporary = Path(tempfile.mkdtemp(prefix="soloscale-shot-analysis-"))
    os.chmod(temporary, 0o700)
    try:
        completed = _run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-vf",
                "scale=160:-2",
                "-r",
                f"{sample_rate:.6f}",
                "-frames:v",
                str(_SHOT_SAMPLE_LIMIT),
                str(temporary / "frame-%04d.jpg"),
            ],
            environment=environment,
            timeout=300,
        )
        if completed.returncode != 0:
            return []
        frames = sorted(temporary.glob("frame-*.jpg"))
        return _detect_shot_timestamps(frames, sample_rate=sample_rate)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _detect_shot_timestamps(
    frames: list[Path], *, sample_rate: float
) -> list[float]:
    if sample_rate <= 0:
        return []
    timestamps: list[float] = []
    previous: Image.Image | None = None
    for index, frame in enumerate(frames):
        try:
            with Image.open(frame) as opened:
                current = opened.convert("RGB")
        except (OSError, ValueError):
            continue
        if previous is not None:
            mean = ImageStat.Stat(ImageChops.difference(previous, current)).mean
            change = sum(mean) / (len(mean) * 255.0)
            timestamp = index / sample_rate
            if change >= _SHOT_CHANGE_THRESHOLD and (
                not timestamps or timestamp - timestamps[-1] >= 0.75
            ):
                timestamps.append(round(timestamp, 3))
        previous = current
    return timestamps[:255]


def _sample_frames(
    source: Path,
    frames_dir: Path,
    *,
    duration: float,
    ffmpeg: Path,
    environment: dict[str, str],
) -> list[dict[str, object]]:
    frames_dir.mkdir(mode=0o700)
    sample_rate = min(1.0, 8.0 / duration)
    completed = _run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-vf",
            "scale=640:-2",
            "-r",
            f"{sample_rate:.6f}",
            "-frames:v",
            "8",
            str(frames_dir / "frame-%02d.jpg"),
        ],
        environment=environment,
        timeout=180,
    )
    if completed.returncode != 0:
        raise ReferenceVideoError("Could not sample reference video keyframes")
    records: list[dict[str, object]] = []
    for frame in sorted(frames_dir.glob("frame-*.jpg")):
        os.chmod(frame, 0o600)
        records.append(
            {
                "filename": frame.name,
                "sha256": hashlib.sha256(frame.read_bytes()).hexdigest(),
                "bytes": frame.stat().st_size,
            }
        )
    if not records:
        raise ReferenceVideoError("Reference video produced no usable keyframes")
    return records


def _transcribe(
    source: Path,
    transcript: Path,
    *,
    data_root: Path,
    resource_root: Path,
    ffmpeg: Path,
    environment: dict[str, str],
    model: str,
) -> str:
    audio = transcript.with_name("audio.wav")
    extracted = _run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(audio),
        ],
        environment=environment,
        timeout=180,
    )
    if extracted.returncode != 0 or not audio.is_file():
        raise ReferenceVideoError("Could not extract reference video audio")
    os.chmod(audio, 0o600)
    runtime = media_runtime_root(data_root)
    python = runtime / "venv" / "bin" / "python"
    worker = resource_root / "media_runtime" / "qwen_mlx_worker.py"
    if not python.is_file() or not worker.is_file():
        raise ReferenceVideoError("Local Qwen transcription runtime is unavailable")
    worker_environment = environment.copy()
    model_cache = runtime / "models"
    model_cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    worker_environment["HF_HOME"] = str(model_cache)
    worker_environment["HF_HUB_CACHE"] = str(model_cache / "hub")
    worker_environment["HF_HUB_OFFLINE"] = "1"
    worker_environment["TRANSFORMERS_OFFLINE"] = "1"
    try:
        completed = _run(
            [
                str(python),
                str(worker),
                "transcribe",
                "--audio",
                str(audio),
                "--output",
                str(transcript),
                "--model",
                model,
                "--language",
                "auto",
            ],
            environment=worker_environment,
            timeout=1_800,
        )
    finally:
        audio.unlink(missing_ok=True)
    if completed.returncode != 0 or not transcript.is_file():
        raise ReferenceVideoError("Local Qwen transcription failed")
    os.chmod(transcript, 0o600)
    try:
        text = " ".join(transcript.read_text(encoding="utf-8").split()).strip()
    except (OSError, UnicodeError) as exc:
        raise ReferenceVideoError("Reference video transcript is unreadable") from exc
    if not text:
        raise ReferenceVideoError("Reference video transcript is empty")
    return text


def analyze_reference_video(
    *,
    data_root: Path,
    resource_root: Path,
    filename: str,
    content: bytes,
    title: str = "",
    author: str = "",
) -> ReferenceVideoAnalysis:
    """Analyze one explicitly selected MP4; raw media remains private and local."""

    if (
        not content
        or len(content) > MAX_REFERENCE_VIDEO_BYTES
        or b"ftyp" not in content[:64]
    ):
        raise ReferenceVideoError("Choose a valid MP4 up to 200 MB")
    raw_sha256 = hashlib.sha256(content).hexdigest()
    reference_id = f"reference-{raw_sha256[:16]}"
    final = _library_dir(data_root, reference_id)
    if final.exists():
        existing = load_reference_video(data_root, reference_id)
        if existing.asset.raw_sha256 != raw_sha256:
            raise ReferenceVideoError("Reference ID collision detected")
        return existing
    root = reference_library_root(data_root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise ReferenceVideoError("Reference library root is unsafe")
    temporary = Path(tempfile.mkdtemp(prefix=".reference-video-", dir=root))
    os.chmod(temporary, 0o700)
    try:
        source = temporary / "source.mp4"
        source.write_bytes(content)
        os.chmod(source, 0o600)
        ffmpeg, ffprobe, environment = _media_tools(resource_root)
        duration, width, height, fps, has_audio = _probe(
            source, ffprobe=ffprobe, environment=environment
        )
        shot_times = sorted(
            {
                round(value, 3)
                for value in _shot_times(
                    source,
                    duration=duration,
                    ffmpeg=ffmpeg,
                    environment=environment,
                )
                if 0 < value < duration
            }
        )[:255]
        keyframes = _sample_frames(
            source,
            temporary / "frames",
            duration=duration,
            ffmpeg=ffmpeg,
            environment=environment,
        )
        transcript_status: Literal["complete", "not_available"] = "not_available"
        transcript_sha256: str | None = None
        transcript_error_code: str | None = None
        transcript = ""
        if has_audio:
            try:
                profile = load_media_profile(data_root)
                transcript = _transcribe(
                    source,
                    temporary / "transcript.txt",
                    data_root=data_root,
                    resource_root=resource_root,
                    ffmpeg=ffmpeg,
                    environment=environment,
                    model=profile.asr_model,
                )
            except (MediaProfileError, ReferenceVideoError) as exc:
                (temporary / "transcript.txt").unlink(missing_ok=True)
                transcript_error_code = (
                    "local_asr_unavailable"
                    if any(
                        marker in str(exc).casefold()
                        for marker in ("unavailable", "missing")
                    )
                    else "local_asr_failed"
                )
            else:
                transcript_status = "complete"
                transcript_sha256 = hashlib.sha256(
                    (transcript + "\n").encode("utf-8")
                ).hexdigest()
        analysis_text = transcript[:20_000] or (
            "A local reference video was supplied for visual pacing and shot structure analysis."
        )
        orientation = "portrait framing" if height > width else "landscape framing"
        shot_count = len(shot_times) + 1
        boundaries = [0.0, *shot_times, duration]
        shot_durations = [
            round(end - start, 3)
            for start, end in zip(boundaries, boundaries[1:], strict=False)
            if end > start
        ]
        average_shot = duration / shot_count
        cuts_per_minute = len(shot_times) * 60 / duration
        cadence: Literal["fast", "moderate", "steady"] = (
            "fast"
            if average_shot < 2.5
            else "moderate"
            if average_shot < 6
            else "steady"
        )
        visual_elements = [orientation, "sampled keyframes", f"{cadence} shot cadence"]
        if has_audio:
            visual_elements.append("spoken narration")
        _, base_pattern, _ = extract_content_pattern(
            analysis_text,
            title=title,
            author=author,
            visual_notes=", ".join(visual_elements),
        )
        video_pattern = ReferenceVideoPattern(
            estimated_duration_seconds=max(1, round(duration)),
            measured_duration_seconds=round(duration, 3),
            shot_cadence=cadence,
            shot_count=shot_count,
            shot_timestamps_seconds=shot_times,
            shot_durations_seconds=shot_durations,
            average_shot_duration_seconds=round(average_shot, 3),
            cuts_per_minute=round(cuts_per_minute, 3),
            opening_segment_seconds=shot_durations[0],
            ending_segment_seconds=shot_durations[-1],
            aspect_ratio=f"{width}:{height}",
            frame_sample_count=len(keyframes),
            has_audio=has_audio,
            visual_elements=visual_elements,
            captions="unknown",
            transitions="observed" if shot_times else "not_observed",
        )
        asset = ReferenceAsset(
            reference_id=reference_id,
            source_kind=ReferenceSourceKind.LOCAL_VIDEO,
            title=title.strip() or Path(filename).stem[:180] or None,
            author=author.strip() or None,
            raw_sha256=raw_sha256,
            raw_character_count=len(transcript),
            source_filename=Path(filename).name[:240],
            source_bytes=len(content),
            duration_seconds=round(duration, 3),
            width=width,
            height=height,
            frames_per_second=round(fps, 3),
            has_audio=has_audio,
            transcript_status=transcript_status,
            transcript_sha256=transcript_sha256,
        )
        pattern_payload = base_pattern.model_dump(mode="json")
        pattern_payload["reference_id"] = reference_id
        pattern_payload["video"] = video_pattern.model_dump(mode="json")
        pattern_payload["unknowns"] = [
            "Keyframes were sampled locally; no identity or factual claims were inferred.",
            "Caption presence remains unknown because the local analyzer does not OCR frames.",
            "Audience and performance metrics were not inferred from the media file.",
        ]
        pattern_payload.pop("pattern_id", None)
        pattern_sha256 = hashlib.sha256(
            json.dumps(
                pattern_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        pattern = ContentPattern(
            pattern_id=f"pattern-{pattern_sha256[:16]}", **pattern_payload
        )
        analysis = {
            "schema_version": "1.0",
            "status": "ANALYZED_LOCALLY",
            "reference_id": reference_id,
            "probe": {
                "duration_seconds": round(duration, 3),
                "width": width,
                "height": height,
                "frames_per_second": round(fps, 3),
                "has_audio": has_audio,
            },
            "shot_timestamps_seconds": shot_times,
            "shot_durations_seconds": shot_durations,
            "cuts_per_minute": round(cuts_per_minute, 3),
            "opening_segment_seconds": shot_durations[0],
            "ending_segment_seconds": shot_durations[-1],
            "keyframes": keyframes,
            "transcript_status": transcript_status,
            "transcript_error_code": transcript_error_code,
            "network_used_for_media": False,
            "publication_performed": False,
        }
        _atomic_private_write(
            temporary / "asset.json",
            json.dumps(asset.model_dump(mode="json"), ensure_ascii=False, indent=2)
            + "\n",
        )
        _atomic_private_write(
            temporary / "pattern.json",
            json.dumps(pattern.model_dump(mode="json"), ensure_ascii=False, indent=2)
            + "\n",
        )
        _atomic_private_write(
            temporary / "analysis.json",
            json.dumps(analysis, ensure_ascii=False, indent=2) + "\n",
        )
        os.replace(temporary, final)
    except (OSError, ResumeWorkspaceStorageError, MediaProfileError, ValueError) as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        if isinstance(exc, ReferenceVideoError):
            raise
        raise ReferenceVideoError(str(exc) or "Reference video analysis failed") from exc
    return load_reference_video(data_root, reference_id)
