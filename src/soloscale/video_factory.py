from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from soloscale.content_models import ContentRun
from soloscale.content_workspace import (
    ContentWorkspaceError,
    content_run_directory,
    load_content_run,
)
from soloscale.media_cost import (
    CostReceiptStatus,
    MediaCostError,
    estimate_local_operation,
    make_cost_receipt,
    save_cost_receipt,
)
from soloscale.presenter_assets import (
    PresenterAssetError,
    PresenterMode,
    materialize_reusable_presenter_assets,
    prepare_presenter_plan,
)
from soloscale.resume_workspace import ResumeWorkspaceStorageError, _atomic_private_write
from soloscale.voice_provider import VoiceProviderError, create_narration_assets

_INPUT_NAME = "09_creator_video_input.json"
_VIDEO_NAME = "10_creator_video.mp4"
_YOUTUBE_INPUT_NAME = "21_creator_video_youtube_input.json"
_YOUTUBE_VIDEO_NAME = "21_creator_video_youtube.mp4"
_THUMBNAIL_NAME = "22_creator_video_thumbnail.png"
_HANDOFF_NAME = "23_heygen_handoff.json"
_AVATAR_MAP_NAME = "24_avatar_segments.json"
_SUBTITLES_NAME = "25_creator_video_subtitles.srt"
_RECEIPT_NAME = "11_creator_video_render.json"
_MACOS_CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
_NODE_CANDIDATES = (
    Path("/opt/homebrew/bin/node"),
    Path("/usr/local/bin/node"),
)


class CreatorVideoError(ValueError):
    """Raised when a local Creator Video render cannot be completed safely."""


@dataclass(frozen=True)
class CreatorVideoJobSnapshot:
    run_id: str
    phase: str
    error: str | None
    elapsed_ms: int


@dataclass
class _CreatorVideoJobRecord:
    run_id: str
    phase: str
    started_at: float
    error: str | None = None
    finished_at: float | None = None


class CreatorVideoJobManager:
    """Run one local Creator Video render at a time without blocking the UI server."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="soloscale-creator-video"
        )
        self._lock = threading.Lock()
        self._jobs: dict[str, _CreatorVideoJobRecord] = {}

    def start(
        self, *, data_root: Path, run_id: str, repository_root: Path
    ) -> CreatorVideoJobSnapshot:
        with self._lock:
            current = self._jobs.get(run_id)
            if current is not None and current.phase in {"QUEUED", "RENDERING"}:
                return self._snapshot(current)
            record = _CreatorVideoJobRecord(
                run_id=run_id,
                phase="QUEUED",
                started_at=time.monotonic(),
            )
            self._jobs[run_id] = record
            self._executor.submit(
                self._execute,
                data_root,
                run_id,
                repository_root,
            )
            return self._snapshot(record)

    def get(self, run_id: str) -> CreatorVideoJobSnapshot | None:
        with self._lock:
            record = self._jobs.get(run_id)
            return self._snapshot(record) if record is not None else None

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _snapshot(record: _CreatorVideoJobRecord) -> CreatorVideoJobSnapshot:
        end = record.finished_at if record.finished_at is not None else time.monotonic()
        return CreatorVideoJobSnapshot(
            run_id=record.run_id,
            phase=record.phase,
            error=record.error,
            elapsed_ms=max(0, int((end - record.started_at) * 1000)),
        )

    def _transition(self, run_id: str, phase: str, *, error: str | None = None) -> None:
        with self._lock:
            record = self._jobs[run_id]
            record.phase = phase
            record.error = error
            if phase in {"COMPLETE", "FAILED"}:
                record.finished_at = time.monotonic()

    def _execute(self, data_root: Path, run_id: str, repository_root: Path) -> None:
        self._transition(run_id, "RENDERING")
        try:
            render_creator_video(
                data_root=data_root,
                run_id=run_id,
                repository_root=repository_root,
            )
        except Exception as exc:
            self._transition(run_id, "FAILED", error=str(exc))
            return
        self._transition(run_id, "COMPLETE")


def creator_video_ready(data_root: Path, run_id: str) -> bool:
    try:
        run_dir = content_run_directory(data_root, run_id)
        paths = (run_dir / _VIDEO_NAME, run_dir / _YOUTUBE_VIDEO_NAME)
        metadata = [path.lstat() for path in paths]
    except (ContentWorkspaceError, FileNotFoundError):
        return False
    return all(
        stat.S_ISREG(item.st_mode)
        and not path.is_symlink()
        and item.st_size > 0
        for path, item in zip(paths, metadata, strict=True)
    )


def _node_executable(factory_root: Path) -> Path | None:
    bundled = factory_root / "runtime" / "node"
    candidates = (bundled, *_NODE_CANDIDATES)
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    discovered = shutil.which("node")
    return Path(discovered) if discovered else None


def creator_video_runtime_available(repository_root: Path) -> bool:
    factory_root = repository_root / "video_factory"
    return (
        (factory_root / "render.mjs").is_file()
        and (factory_root / "node_modules" / "@remotion" / "renderer").is_dir()
        and _node_executable(factory_root) is not None
        and _MACOS_CHROME.is_file()
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def prepare_heygen_handoff(*, data_root: Path, run_id: str) -> Path:
    """Create the exact, user-reviewable avatar payload without any network call."""

    run = load_content_run(data_root, run_id)
    run_dir = content_run_directory(data_root, run_id)
    path = run_dir / _HANDOFF_NAME
    plan = prepare_presenter_plan(data_root=data_root, run_id=run_id)
    plan_summary = {
        "total_scenes": plan.total_scenes,
        "presenter_scenes": plan.presenter_scenes,
        "reusable_presenter_scenes": plan.reusable_presenter_scenes,
        "dynamic_avatar_scenes": plan.dynamic_avatar_scenes,
        "dynamic_avatar_seconds": plan.dynamic_avatar_seconds,
        "reusable_asset_ratio": plan.reusable_asset_ratio,
    }
    if path.exists() or path.is_symlink():
        if not path.is_file() or path.is_symlink():
            raise CreatorVideoError("HeyGen handoff path is unsafe")
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CreatorVideoError("HeyGen handoff is invalid") from exc
        if existing.get("presenter_plan") == plan_summary:
            return path
    scenes_by_id = {scene.id: scene for scene in run.drafts.storyboard}
    dynamic_scenes = [
        item for item in plan.scenes if item.mode is PresenterMode.DYNAMIC_AVATAR
    ]
    payload = {
        "schema_version": "1.0",
        "status": (
            "READY_FOR_MANUAL_HEYGEN_EXPORT"
            if dynamic_scenes
            else "NO_DYNAMIC_AVATAR_REQUIRED"
        ),
        "run_id": run_id,
        "provider": "heygen",
        "network_used": False,
        "publication_performed": False,
        "presenter_plan": plan_summary,
        "instructions": (
            "Generate only these short presenter segments in HeyGen, download each MP4, "
            "then import it into the matching SoloScale scene."
        ),
        "external_submission_preview": {
            "text_fields": ["scene_id", "purpose", "voiceover"],
            "files": [],
            "raw_conversations_included": False,
            "project_files_included": False,
        },
        "segments": [
            {
                "scene_id": scene_plan.scene_id,
                "purpose": scenes_by_id[scene_plan.scene_id].purpose,
                "voiceover": scenes_by_id[scene_plan.scene_id].voiceover,
                "preferred_duration_seconds": scene_plan.duration_seconds,
                "required_exports": ["16:9", "9:16"],
            }
            for scene_plan in dynamic_scenes
        ],
    }
    try:
        _atomic_private_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    except (OSError, ResumeWorkspaceStorageError) as exc:
        raise CreatorVideoError("Could not save the HeyGen handoff") from exc
    return path


def _avatar_map(run_dir: Path) -> dict[str, dict[str, str]]:
    path = run_dir / _AVATAR_MAP_NAME
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        segments = raw.get("segments", {})
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CreatorVideoError("Avatar segment map is invalid") from exc
    if not isinstance(segments, dict):
        raise CreatorVideoError("Avatar segment map is invalid")
    result: dict[str, dict[str, str]] = {}
    for scene_id, value in segments.items():
        if isinstance(scene_id, str) and isinstance(value, dict):
            result[scene_id] = {
                key: str(value.get(key, ""))
                for key in ("path", "sha256", "source_filename")
            }
    return result


def import_avatar_segment(
    *,
    data_root: Path,
    run_id: str,
    scene_id: str,
    source_filename: str,
    content: bytes,
) -> Path:
    """Attach one user-selected MP4 to an exact storyboard scene."""

    run = load_content_run(data_root, run_id)
    allowed_ids = {scene.id for scene in run.drafts.storyboard}
    if scene_id not in allowed_ids:
        raise CreatorVideoError("Select a valid storyboard scene")
    if not content or len(content) > 12 * 1024 * 1024 or b"ftyp" not in content[:64]:
        raise CreatorVideoError("Avatar segment must be a valid MP4 up to 12 MB")
    run_dir = content_run_directory(data_root, run_id)
    avatar_dir = run_dir / "avatar-segments"
    try:
        avatar_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(avatar_dir, 0o700)
    except OSError as exc:
        raise CreatorVideoError("Could not prepare private avatar storage") from exc
    target = avatar_dir / f"{scene_id}.mp4"
    if target.exists() or target.is_symlink():
        raise CreatorVideoError("This scene already has an imported avatar segment")
    try:
        target.write_bytes(content)
        os.chmod(target, 0o600)
        segments = _avatar_map(run_dir)
        segments[scene_id] = {
            "path": target.relative_to(run_dir).as_posix(),
            "sha256": _sha256_bytes(content),
            "source_filename": Path(source_filename).name[:180],
        }
        _atomic_private_write(
            run_dir / _AVATAR_MAP_NAME,
            json.dumps(
                {
                    "schema_version": "1.0",
                    "run_id": run_id,
                    "segments": segments,
                    "network_used": False,
                    "publication_performed": False,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
    except (OSError, ResumeWorkspaceStorageError) as exc:
        target.unlink(missing_ok=True)
        raise CreatorVideoError("Could not import the avatar segment") from exc
    return target


def _avatar_public_assets(run_dir: Path, public_dir: Path) -> dict[str, str]:
    assets: dict[str, str] = {}
    for scene_id, metadata in _avatar_map(run_dir).items():
        source = run_dir / metadata["path"]
        if source.is_symlink() or not source.is_file():
            raise CreatorVideoError("An imported avatar segment is unavailable")
        if _sha256_bytes(source.read_bytes()) != metadata["sha256"]:
            raise CreatorVideoError("An imported avatar segment hash does not match")
        name = f"avatar-{scene_id}.mp4"
        shutil.copyfile(source, public_dir / name)
        assets[scene_id] = name
    return assets


def _srt_timestamp(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},000"


def _render_subtitles(run: ContentRun) -> str:
    return "\n\n".join(
        (
            f"{index}\n{_srt_timestamp(scene.start_second)} --> "
            f"{_srt_timestamp(scene.end_second)}\n{scene.voiceover}"
        )
        for index, scene in enumerate(run.drafts.storyboard, start=1)
    ) + "\n"


def _render_input(
    *,
    run: ContentRun,
    width: int,
    height: int,
    avatar_assets: dict[str, str],
    audio_assets: dict[str, str],
    presenter_layouts: dict[str, str],
) -> dict[str, object]:
    return {
        "topic": run.brief.topic,
        "sourceLabel": run.brief.source_label,
        "width": width,
        "height": height,
        "scenes": [
            {
                "id": scene.id,
                "start_second": scene.start_second,
                "end_second": scene.end_second,
                "purpose": scene.purpose,
                "voiceover": scene.voiceover,
                "on_screen_text": scene.on_screen_text,
                "claim_ids": scene.claim_ids,
                "avatar_clip_asset": avatar_assets.get(scene.id),
                "audio_asset": audio_assets.get(scene.id),
                "presenter_layout": presenter_layouts.get(scene.id),
            }
            for scene in run.drafts.storyboard
        ],
    }


def _run_renderer(
    *,
    renderer: Path,
    factory_root: Path,
    input_path: Path,
    output_path: Path,
    public_dir: Path,
    thumbnail_path: Path | None,
    environment: dict[str, str],
) -> None:
    node = _node_executable(factory_root)
    if node is None:
        raise CreatorVideoError("Creator Video requires a local Node runtime")
    command = [
        str(node),
        str(renderer),
        "--input",
        str(input_path),
        "--output",
        str(output_path),
        "--public-dir",
        str(public_dir),
    ]
    if thumbnail_path is not None:
        command.extend(["--thumbnail", str(thumbnail_path)])
    completed = subprocess.run(
        command,
        cwd=factory_root,
        capture_output=True,
        check=False,
        text=True,
        timeout=900,
        env=environment,
    )
    if completed.returncode != 0 or not output_path.is_file() or output_path.stat().st_size == 0:
        output_path.unlink(missing_ok=True)
        raise CreatorVideoError("Creator Video render failed; review the local renderer setup")
    os.chmod(output_path, 0o600)


def render_creator_video(*, data_root: Path, run_id: str, repository_root: Path) -> Path:
    """Render one saved storyboard as both 16:9 and 9:16 local MP4s."""

    render_started_at = datetime.now(UTC)
    render_started = time.monotonic()
    run = load_content_run(data_root, run_id)
    run_dir = content_run_directory(data_root, run_id)
    input_path = run_dir / _INPUT_NAME
    output_path = run_dir / _VIDEO_NAME
    youtube_input_path = run_dir / _YOUTUBE_INPUT_NAME
    youtube_output_path = run_dir / _YOUTUBE_VIDEO_NAME
    thumbnail_path = run_dir / _THUMBNAIL_NAME
    subtitles_path = run_dir / _SUBTITLES_NAME
    receipt_path = run_dir / _RECEIPT_NAME
    protected = (
        input_path,
        output_path,
        youtube_input_path,
        youtube_output_path,
        thumbnail_path,
        subtitles_path,
        receipt_path,
    )
    if any(path.exists() or path.is_symlink() for path in protected):
        raise CreatorVideoError("This run already has a Creator Video render")
    factory_root = repository_root / "video_factory"
    renderer = factory_root / "render.mjs"
    if not creator_video_runtime_available(repository_root):
        raise CreatorVideoError(
            "Creator Video runtime is unavailable; Node, Chrome, or Remotion is missing"
        )
    environment = os.environ.copy()
    if _MACOS_CHROME.is_file():
        environment.setdefault("REMOTION_BROWSER_EXECUTABLE", str(_MACOS_CHROME))
    try:
        with tempfile.TemporaryDirectory(prefix="soloscale-video-assets-") as raw_public:
            public_dir = Path(raw_public)
            presenter_plan = prepare_presenter_plan(data_root=data_root, run_id=run_id)
            reusable_assets = materialize_reusable_presenter_assets(
                data_root=data_root,
                plan=presenter_plan,
                public_dir=public_dir,
            )
            dynamic_avatar_assets = _avatar_public_assets(run_dir, public_dir)
            avatar_assets = {**reusable_assets, **dynamic_avatar_assets}
            presenter_layouts = {
                item.scene_id: item.layout.value
                for item in presenter_plan.scenes
                if item.mode is not PresenterMode.NONE
            }
            narration = create_narration_assets(
                run=run,
                public_dir=public_dir,
                avatar_assets=dynamic_avatar_assets,
                data_root=data_root,
                resource_root=repository_root,
            )
            audio_assets = narration.assets
            short_input = _render_input(
                run=run,
                width=1080,
                height=1920,
                avatar_assets=avatar_assets,
                audio_assets=audio_assets,
                presenter_layouts=presenter_layouts,
            )
            youtube_input = _render_input(
                run=run,
                width=1920,
                height=1080,
                avatar_assets=avatar_assets,
                audio_assets=audio_assets,
                presenter_layouts=presenter_layouts,
            )
            _atomic_private_write(subtitles_path, _render_subtitles(run))
            _atomic_private_write(
                input_path, json.dumps(short_input, ensure_ascii=False) + "\n"
            )
            _atomic_private_write(
                youtube_input_path,
                json.dumps(youtube_input, ensure_ascii=False) + "\n",
            )
            _run_renderer(
                renderer=renderer,
                factory_root=factory_root,
                input_path=youtube_input_path,
                output_path=youtube_output_path,
                public_dir=public_dir,
                thumbnail_path=thumbnail_path,
                environment=environment,
            )
            _run_renderer(
                renderer=renderer,
                factory_root=factory_root,
                input_path=input_path,
                output_path=output_path,
                public_dir=public_dir,
                thumbnail_path=None,
                environment=environment,
            )
    except (
        OSError,
        ResumeWorkspaceStorageError,
        CreatorVideoError,
        PresenterAssetError,
        VoiceProviderError,
        subprocess.TimeoutExpired,
    ) as exc:
        for path in protected:
            path.unlink(missing_ok=True)
        if isinstance(exc, CreatorVideoError):
            raise
        raise CreatorVideoError("Could not complete the local Creator Video render") from exc
    if not creator_video_ready(data_root, run_id):
        raise CreatorVideoError("Creator Video did not create both required outputs")
    os.chmod(thumbnail_path, 0o600)
    render_finished_at = datetime.now(UTC)
    render_duration_ms = max(0, int((time.monotonic() - render_started) * 1000))
    try:
        cost_receipt = make_cost_receipt(
            run_id=run_id,
            video_run_id=run_id,
            estimate=estimate_local_operation(
                service="remotion",
                feature="content_video",
                operation="render_package",
            ),
            status=CostReceiptStatus.SUCCEEDED,
            started_at=render_started_at,
            finished_at=render_finished_at,
            duration_ms=render_duration_ms,
            actual_cost_usd=Decimal(0),
        )
        save_cost_receipt(data_root, cost_receipt)
    except (MediaCostError, OSError, ValueError) as exc:
        raise CreatorVideoError(
            "Creator Video rendered, but its local cost receipt could not be saved"
        ) from exc
    receipt = {
        "status": "RENDERED_LOCAL_VIDEO_PACKAGE",
        "short_video": _VIDEO_NAME,
        "youtube_video": _YOUTUBE_VIDEO_NAME,
        "thumbnail": _THUMBNAIL_NAME,
        "short_input": _INPUT_NAME,
        "youtube_input": _YOUTUBE_INPUT_NAME,
        "run_id": run_id,
        "avatar_segment_count": len(_avatar_map(run_dir)),
        "reusable_presenter_scene_count": presenter_plan.reusable_presenter_scenes,
        "dynamic_avatar_scene_count": presenter_plan.dynamic_avatar_scenes,
        "dynamic_avatar_seconds": presenter_plan.dynamic_avatar_seconds,
        "reusable_asset_ratio": presenter_plan.reusable_asset_ratio,
        "local_narration_scene_count": len(audio_assets),
        "narration_provider": narration.provider,
        "narration_model": narration.model,
        "narration_locale": narration.locale,
        "voice_reference_sha256": narration.reference_audio_sha256,
        "subtitles": _SUBTITLES_NAME,
        "subtitles_sha256": _sha256_bytes(subtitles_path.read_bytes()),
        "short_sha256": _sha256_bytes(output_path.read_bytes()),
        "youtube_sha256": _sha256_bytes(youtube_output_path.read_bytes()),
        "network_used": False,
        "publication_performed": False,
        "renderer": "Remotion 4.0.421",
        "cost_receipt_id": cost_receipt.receipt_id,
        "local_api_cost_usd": "0",
        "render_duration_ms": render_duration_ms,
    }
    try:
        _atomic_private_write(
            receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
        )
    except (OSError, ResumeWorkspaceStorageError) as exc:
        raise CreatorVideoError(
            "Creator Video was rendered but its receipt could not be saved"
        ) from exc
    return output_path
