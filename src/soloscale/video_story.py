"""Evidence-backed local engineering-story video jobs for the desktop app."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from soloscale.content_workspace import load_content_run
from soloscale.resume_workspace import (
    _atomic_private_write,
    _ensure_private_directory,
)

VideoStoryPhase = Literal[
    "QUEUED",
    "PREPARING_STORY",
    "PREPARING_ASSETS",
    "RENDERING",
    "COMPLETE",
    "FAILED",
]

_JOB_ID = re.compile(r"video-story-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{10}")
_STORY_ID = "resume-latency-system-design-v1"
_IMPLEMENTATION_COMMIT = "2a0ceaff8bf89f27a04f228bc90e2d3a77831e11"
_RECEIPT_RUN_IDS = (
    "resume-20260826T103534Z-ac947a4af6",
    "resume-20260826T103826Z-0a7e1a7d58",
)
_ARTIFACTS = {
    "video": "engineering-story.mp4",
    "subtitles": "engineering-story.zh-CN.srt",
    "thumbnail": "engineering-story-thumbnail.png",
    "story": "canonical-story.md",
    "narration": "narration.zh-CN.md",
    "manifest": "scene-manifest.json",
    "input": "render-input.json",
    "receipt": "render-receipt.json",
    "job": "job.json",
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VideoStoryEvidence(_StrictModel):
    evidence_id: str
    kind: Literal["git_commit", "resume_run_receipt", "content_claim"]
    locator: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    verified_facts: list[str]


class SixLayerStory(_StrictModel):
    fact: str
    architecture: str
    decision: str
    implementation: str
    failure_and_surprise: str
    evolution: str


class EngineeringStoryScene(_StrictModel):
    id: str = Field(pattern=r"^SCENE-[0-9]{2}$")
    start_second: int = Field(ge=0)
    end_second: int = Field(gt=0)
    purpose: str
    visual_kind: Literal[
        "hook",
        "pipeline",
        "separation",
        "implementation",
        "metrics",
        "bottleneck",
        "evolution",
        "content",
    ]
    voiceover: str
    on_screen_text: str
    detail_lines: list[str] = Field(default_factory=list, max_length=8)
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)


class EngineeringStory(_StrictModel):
    story_id: str
    title: str
    subtitle: str
    language: Literal["zh-CN", "en-US"] = "zh-CN"
    width: Literal[1080] = 1080
    height: Literal[1920] = 1920
    fps: Literal[30] = 30
    duration_seconds: int = Field(ge=10, le=180)
    layers: SixLayerStory
    evidence: list[VideoStoryEvidence]
    scenes: list[EngineeringStoryScene] = Field(min_length=1, max_length=12)


class LocalVideoJobRecord(_StrictModel):
    job_id: str
    story_id: str
    content_run_id: str | None = None
    phase: VideoStoryPhase
    created_at: str
    updated_at: str
    stage_durations_ms: dict[str, int] = Field(default_factory=dict)
    output_sha256: str | None = None
    output_duration_seconds: float | None = None
    audio_included: bool = False
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class LocalVideoJobSnapshot:
    job_id: str
    story_id: str
    phase: VideoStoryPhase
    created_at: str
    stage_durations_ms: dict[str, int]
    total_elapsed_ms: int
    output_sha256: str | None
    output_duration_seconds: float | None
    audio_included: bool
    error_code: str | None
    error_message: str | None


@dataclass
class _LiveVideoJob:
    persisted: LocalVideoJobRecord
    created_at_perf: float
    phase_started_at: float
    finished_at_perf: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class VideoStoryError(ValueError):
    """Raised when the local story or render boundary cannot be verified."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_regular_json(path: Path) -> dict[str, object]:
    try:
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise VideoStoryError("Evidence receipt is not a regular file")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VideoStoryError("Evidence receipt is unavailable") from exc
    if not isinstance(payload, dict):
        raise VideoStoryError("Evidence receipt has an invalid shape")
    return payload


def _required_int(mapping: object, key: str) -> int:
    if not isinstance(mapping, dict):
        raise VideoStoryError("Timing receipt is missing")
    value = mapping.get(key)
    if not isinstance(value, int) or value < 0:
        raise VideoStoryError(f"Timing receipt is missing {key}")
    return value


def _verify_implementation_commit(repository_root: Path) -> VideoStoryEvidence:
    completed = subprocess.run(
        ["git", "cat-file", "-e", f"{_IMPLEMENTATION_COMMIT}^{{commit}}"],
        cwd=repository_root,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if completed.returncode != 0:
        raise VideoStoryError("Verified implementation commit is unavailable")
    commit_bytes = subprocess.run(
        ["git", "show", "-s", "--format=%H%n%s", _IMPLEMENTATION_COMMIT],
        cwd=repository_root,
        capture_output=True,
        check=True,
        timeout=10,
    ).stdout
    return VideoStoryEvidence(
        evidence_id="EVIDENCE-01",
        kind="git_commit",
        locator=f"git:{_IMPLEMENTATION_COMMIT[:7]}",
        sha256=hashlib.sha256(commit_bytes).hexdigest(),
        verified_facts=[
            "Resume generation moved behind ResumeJobManager.",
            "The local job executor uses max_workers=1.",
            "DOCX_READY is exposed before PDF preview completion.",
        ],
    )


def _load_timing_evidence(
    data_root: Path, run_id: str, index: int
) -> tuple[VideoStoryEvidence, dict[str, int]]:
    run_dir = data_root / "resume-runs" / run_id
    run_path = run_dir / "run.json"
    application_path = run_dir / "application_receipt.json"
    run = _read_regular_json(run_path)
    application = _read_regular_json(application_path)
    if run.get("run_id") != run_id or run.get("status") != "DRAFT_REQUIRES_HUMAN_REVIEW":
        raise VideoStoryError("Resume timing evidence is not a completed review draft")
    if application.get("provider") != "ollama" or application.get("model") != "qwen3:8b":
        raise VideoStoryError("Resume timing evidence is not the verified qwen3:8b route")
    resume_job = run.get("resume_job")
    if not isinstance(resume_job, dict) or resume_job.get("phase") != "COMPLETE":
        raise VideoStoryError("Resume timing evidence did not complete")
    timing = resume_job.get("timing_ms")
    values = {
        key: _required_int(timing, key)
        for key in (
            "post_response_ms",
            "model_generation_ms",
            "total_ms",
            "docx_ms",
            "pdf_preview_ms",
        )
    }
    if values["total_ms"] == 0 or values["model_generation_ms"] > values["total_ms"]:
        raise VideoStoryError("Resume timing evidence is internally inconsistent")
    return (
        VideoStoryEvidence(
            evidence_id=f"EVIDENCE-{index + 2:02d}",
            kind="resume_run_receipt",
            locator=run_id,
            sha256=_sha256_file(run_path),
            verified_facts=[
                f"POST returned in {values['post_response_ms']} ms.",
                f"Model generation took {values['model_generation_ms']} ms.",
                f"The completed job took {values['total_ms']} ms.",
                (
                    f"DOCX export took {values['docx_ms']} ms and PDF preview took "
                    f"{values['pdf_preview_ms']} ms."
                ),
            ],
        ),
        values,
    )


def build_resume_latency_story(*, data_root: Path, repository_root: Path) -> EngineeringStory:
    """Build the first six-layer story only after its local evidence verifies."""

    commit_evidence = _verify_implementation_commit(repository_root)
    timing_pairs = [
        _load_timing_evidence(data_root, run_id, index)
        for index, run_id in enumerate(_RECEIPT_RUN_IDS)
    ]
    timing_evidence = [item[0] for item in timing_pairs]
    timings = [item[1] for item in timing_pairs]
    generation_share = [item["model_generation_ms"] / item["total_ms"] * 100 for item in timings]
    if not all(value > 99 for value in generation_share):
        raise VideoStoryError("Measured model-generation share no longer supports the story")

    layers = SixLayerStory(
        fact=(
            "A local AI resume workflow appeared frozen for roughly two minutes during two "
            "measured qwen3:8b runs."
        ),
        architecture=(
            "The old synchronous request coupled profile extraction, retrieval, local model "
            "generation, verification, DOCX export, and PDF preview to one HTTP response."
        ),
        decision=(
            "Separate request lifetime, background-job lifetime, UI responsiveness, and model "
            "inference without adding distributed infrastructure to a single-user desktop app."
        ),
        implementation=(
            "ResumeJobManager queues one worker, returns a job ID immediately, exposes polling "
            "states, and makes DOCX available before the PDF preview finishes."
        ),
        failure_and_surprise=(
            "Responsiveness improved, but inference did not: model generation represented more "
            "than 99% of both measured end-to-end runs."
        ),
        evolution=(
            "Keep local inference as an offline option, route high-value generation to a hosted "
            "model when appropriate, and retain deterministic truth enforcement locally."
        ),
    )
    scenes = [
        EngineeringStoryScene(
            id="SCENE-01",
            start_second=0,
            end_second=11,
            purpose="Hook",
            visual_kind="hook",
            voiceover=(
                "我做了一个本地 AI 简历工具。点击生成后，界面像卡死了两分钟。"
                "真正的问题不是页面，而是同步请求把不同生命周期绑在了一起。"
            ),
            on_screen_text="一个 AI 简历 App，为什么像卡死了两分钟？",
            detail_lines=["~2 MIN", "UI ≠ MODEL LATENCY"],
            evidence_ids=["EVIDENCE-02", "EVIDENCE-03"],
        ),
        EngineeringStoryScene(
            id="SCENE-02",
            start_second=11,
            end_second=23,
            purpose="Old architecture",
            visual_kind="pipeline",
            voiceover=(
                "旧流程从一次 HTTP 请求开始，依次完成资料提取、检索、本地模型生成、"
                "事实校验、DOCX 和 PDF。任何一步没结束，浏览器都只能等。"
            ),
            on_screen_text="一次请求，绑住整条生成链",
            detail_lines=["POST /generate", "Retrieval", "qwen3:8b", "Verify", "DOCX", "PDF"],
            evidence_ids=["EVIDENCE-01"],
        ),
        EngineeringStoryScene(
            id="SCENE-03",
            start_second=23,
            end_second=35,
            purpose="Architecture decision",
            visual_kind="separation",
            voiceover=(
                "我没有引入 Redis、Celery 或 Kubernetes。这个单用户桌面应用只需要把"
                "请求生命周期、后台任务、界面并发和模型推理解耦。"
            ),
            on_screen_text="先分离生命周期，不升级基础设施",
            detail_lines=["HTTP REQUEST", "BACKGROUND JOB", "RESPONSIVE UI", "MODEL INFERENCE"],
            evidence_ids=["EVIDENCE-01"],
        ),
        EngineeringStoryScene(
            id="SCENE-04",
            start_second=35,
            end_second=48,
            purpose="Implementation",
            visual_kind="implementation",
            voiceover=(
                "我加入 Resume Job Manager，用单线程后台队列执行生成。点击后立即返回 job ID，"
                "页面轮询明确状态；DOCX 一完成就能下载，PDF 继续在后台渲染。"
            ),
            on_screen_text="ResumeJobManager · max_workers=1",
            detail_lines=["QUEUED", "GENERATING", "VERIFYING", "DOCX_READY", "PREVIEWING"],
            evidence_ids=["EVIDENCE-01"],
        ),
        EngineeringStoryScene(
            id="SCENE-05",
            start_second=48,
            end_second=61,
            purpose="Measured result",
            visual_kind="metrics",
            voiceover=(
                "两次真实 qwen3 八 B 运行，总耗时分别约一百二十二点六秒和一百二十八点三秒。"
                "但创建后台任务的响应只用了十九和十六毫秒，界面不再等待整条链。"
            ),
            on_screen_text="真实运行：慢模型，快响应",
            detail_lines=["COLD 122.584s · POST 19ms", "WARM 128.258s · POST 16ms"],
            evidence_ids=["EVIDENCE-02", "EVIDENCE-03"],
        ),
        EngineeringStoryScene(
            id="SCENE-06",
            start_second=61,
            end_second=73,
            purpose="Failure and surprise",
            visual_kind="bottleneck",
            voiceover=(
                "修复响应性，并没有让模型变快。测量显示，模型生成占两次总耗时都超过百分之九十九。"
                "检索、校验、DOCX 和 PDF 都不是主要瓶颈。"
            ),
            on_screen_text=">99% 时间花在模型生成",
            detail_lines=["121.609s / 122.584s", "127.330s / 128.258s"],
            evidence_ids=["EVIDENCE-02", "EVIDENCE-03"],
        ),
        EngineeringStoryScene(
            id="SCENE-07",
            start_second=73,
            end_second=84,
            purpose="Evolution",
            visual_kind="evolution",
            voiceover=(
                "这改变了下一步：本地模式保留离线和隐私，托管强模型负责高价值生成，"
                "系统继续守住事实边界。先解耦，再测量，再决定。"
            ),
            on_screen_text="Build → Measure → Learn → Iterate",
            detail_lines=["LOCAL · OFFLINE", "HOSTED · HIGH VALUE", "TRUTH · LOCAL"],
            evidence_ids=["EVIDENCE-01", "EVIDENCE-02", "EVIDENCE-03"],
        ),
    ]
    return EngineeringStory(
        story_id=_STORY_ID,
        title="一个 AI 简历 App 卡住两分钟之后，我学到的系统设计",
        subtitle="模型延迟和 UI 响应性，是两个不同的系统问题",
        duration_seconds=84,
        layers=layers,
        evidence=[commit_evidence, *timing_evidence],
        scenes=scenes,
    )


def build_content_run_story(*, data_root: Path, content_run_id: str) -> EngineeringStory:
    """Build one local render blueprint from an existing ContentProject run.

    Facts are consumed from the canonical ContentRun (claims, storyboard, and
    evidence references) instead of being re-entered by a second video product.
    """

    run = load_content_run(data_root, content_run_id)
    scenes: list[EngineeringStoryScene] = []
    for index, scene in enumerate(run.drafts.storyboard, start=1):
        scenes.append(
            EngineeringStoryScene(
                id=f"SCENE-{index:02d}",
                start_second=scene.start_second,
                end_second=scene.end_second,
                purpose=scene.purpose,
                visual_kind="content",
                voiceover=scene.voiceover,
                on_screen_text=scene.on_screen_text,
                detail_lines=[line for line in (scene.visual, scene.purpose) if line],
                evidence_ids=scene.claim_ids,
            )
        )
    if not scenes:
        raise VideoStoryError("Content run has no storyboard scenes to render")
    duration_seconds = max(10, min(180, int(scenes[-1].end_second)))
    evidence = [
        VideoStoryEvidence(
            evidence_id=claim.id,
            kind="content_claim",
            locator=f"run:{run.run_id}:claim:{claim.id}",
            sha256=hashlib.sha256(claim.text.encode("utf-8")).hexdigest(),
            verified_facts=[claim.text],
        )
        for claim in run.brief.claims
    ]
    canonical_preview = " ".join(run.drafts.canonical_story.split())
    layers = SixLayerStory(
        fact="; ".join(claim.text for claim in run.brief.claims),
        architecture="Canonical ContentProject narrative consumed downstream.",
        decision="Facts and storyboard stay owned by the ContentProject.",
        implementation="Local Remotion renders this run's video script and storyboard.",
        failure_and_surprise="No second story entry; video is a downstream projection.",
        evolution="Rendered artifacts flow to the Publish Queue with trace receipts.",
    )
    return EngineeringStory(
        story_id=f"content-{run.run_id}",
        title=run.brief.topic,
        subtitle=canonical_preview[:200],
        language="en-US" if run.brief.language == "English" else "zh-CN",
        duration_seconds=duration_seconds,
        layers=layers,
        evidence=evidence,
        scenes=scenes,
    )


def _srt_timestamp(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},000"


def _render_story_markdown(story: EngineeringStory) -> str:
    layers = story.layers
    return "\n".join(
        [
            f"# {story.title}",
            "",
            f"> {story.subtitle}",
            "",
            "## Six-layer story",
            "",
            f"1. Fact — {layers.fact}",
            f"2. Architecture — {layers.architecture}",
            f"3. Decision — {layers.decision}",
            f"4. Implementation — {layers.implementation}",
            f"5. Failure and surprise — {layers.failure_and_surprise}",
            f"6. Evolution — {layers.evolution}",
            "",
            "## Evidence references",
            "",
            *[
                f"- {item.evidence_id}: {item.locator} · SHA-256 {item.sha256}"
                for item in story.evidence
            ],
            "",
            "Publication performed: false",
            "",
        ]
    )


def _write_story_sources(job_dir: Path, story: EngineeringStory) -> None:
    narration = "\n\n".join(
        [f"## {scene.id} · {scene.purpose}\n\n{scene.voiceover}" for scene in story.scenes]
    )
    subtitles = "\n\n".join(
        [
            (
                f"{index}\n{_srt_timestamp(scene.start_second)} --> "
                f"{_srt_timestamp(scene.end_second)}\n{scene.voiceover}"
            )
            for index, scene in enumerate(story.scenes, start=1)
        ]
    )
    manifest = {
        "schema_version": "1.0",
        "story": story.model_dump(mode="json"),
        "publication_performed": False,
    }
    _atomic_private_write(job_dir / _ARTIFACTS["story"], _render_story_markdown(story))
    _atomic_private_write(job_dir / _ARTIFACTS["narration"], narration + "\n")
    _atomic_private_write(job_dir / _ARTIFACTS["subtitles"], subtitles + "\n")
    _atomic_private_write(
        job_dir / _ARTIFACTS["manifest"],
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )


def _create_local_narration(job_dir: Path, story: EngineeringStory) -> dict[str, str]:
    say = shutil.which("say")
    afconvert = shutil.which("afconvert")
    if say is None or afconvert is None:
        return {}
    audio_dir = job_dir / "audio"
    _ensure_private_directory(audio_dir)
    audio_data: dict[str, str] = {}
    for scene in story.scenes:
        aiff_path = audio_dir / f"{scene.id}.aiff"
        wav_path = audio_dir / f"{scene.id}.wav"
        spoken = subprocess.run(
            [say, "-v", "Tingting", "-r", "205", "-o", str(aiff_path), scene.voiceover],
            capture_output=True,
            check=False,
            timeout=45,
        )
        if spoken.returncode != 0 or not aiff_path.is_file() or aiff_path.stat().st_size <= 4096:
            return {}
        converted = subprocess.run(
            [afconvert, "-f", "WAVE", "-d", "LEI16@22050", str(aiff_path), str(wav_path)],
            capture_output=True,
            check=False,
            timeout=30,
        )
        aiff_path.unlink(missing_ok=True)
        if converted.returncode != 0 or not wav_path.is_file() or wav_path.stat().st_size <= 44:
            return {}
        os.chmod(wav_path, 0o600)
        encoded = base64.b64encode(wav_path.read_bytes()).decode("ascii")
        audio_data[scene.id] = f"data:audio/wav;base64,{encoded}"
    return audio_data


def _renderer_payload(story: EngineeringStory, audio_data: dict[str, str]) -> dict[str, object]:
    return {
        "topic": story.title,
        "sourceLabel": "SoloScale verified local evidence",
        "subtitle": story.subtitle,
        "scenes": [
            {
                "id": scene.id,
                "start_second": scene.start_second,
                "end_second": scene.end_second,
                "purpose": scene.purpose,
                "visual_kind": scene.visual_kind,
                "voiceover": scene.voiceover,
                "on_screen_text": scene.on_screen_text,
                "detail_lines": scene.detail_lines,
                "claim_ids": scene.evidence_ids,
                "audio_data_url": audio_data.get(scene.id),
            }
            for scene in story.scenes
        ],
    }


def _render_with_remotion(
    *,
    job_dir: Path,
    repository_root: Path,
    story: EngineeringStory,
    audio_data: dict[str, str],
) -> bool:
    factory_root = repository_root / "video_factory"
    renderer = factory_root / "render.mjs"
    if not renderer.is_file() or not (factory_root / "node_modules").is_dir():
        raise VideoStoryError("Local Remotion runtime is unavailable")
    input_path = job_dir / _ARTIFACTS["input"]
    output_path = job_dir / _ARTIFACTS["video"]
    thumbnail_path = job_dir / _ARTIFACTS["thumbnail"]
    _atomic_private_write(
        input_path,
        json.dumps(_renderer_payload(story, audio_data), ensure_ascii=False) + "\n",
    )
    environment = os.environ.copy()
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if chrome.is_file():
        environment.setdefault("REMOTION_BROWSER_EXECUTABLE", str(chrome))
    completed = subprocess.run(
        [
            "node",
            str(renderer),
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--thumbnail",
            str(thumbnail_path),
        ],
        cwd=factory_root,
        capture_output=True,
        check=False,
        text=True,
        timeout=900,
        env=environment,
    )
    if completed.returncode != 0:
        raise VideoStoryError("Local Remotion render failed")
    for path in (output_path, thumbnail_path):
        if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
            raise VideoStoryError("Local Remotion render did not create every output")
        os.chmod(path, 0o600)
    return len(audio_data) == len(story.scenes)


def _write_render_receipt(
    *, job_dir: Path, story: EngineeringStory, render_ms: int, audio_included: bool
) -> tuple[str, float]:
    output_path = job_dir / _ARTIFACTS["video"]
    output_sha256 = _sha256_file(output_path)
    receipt = {
        "schema_version": "1.0",
        "status": "RENDERED_LOCAL_MP4",
        "story_id": story.story_id,
        "evidence": [
            {
                "evidence_id": item.evidence_id,
                "kind": item.kind,
                "locator": item.locator,
                "sha256": item.sha256,
            }
            for item in story.evidence
        ],
        "renderer": "Remotion 4.0.421",
        "render_time_ms": render_ms,
        "video": _ARTIFACTS["video"],
        "subtitles": _ARTIFACTS["subtitles"],
        "thumbnail": _ARTIFACTS["thumbnail"],
        "story": _ARTIFACTS["story"],
        "narration": _ARTIFACTS["narration"],
        "manifest": _ARTIFACTS["manifest"],
        "output_sha256": output_sha256,
        "width": story.width,
        "height": story.height,
        "fps": story.fps,
        "duration_seconds": story.duration_seconds,
        "audio_included": audio_included,
        "network_used": False,
        "model_used": False,
        "publication_performed": False,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    _atomic_private_write(
        job_dir / _ARTIFACTS["receipt"],
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
    )
    return output_sha256, float(story.duration_seconds)


def local_video_job_directory(data_root: Path, job_id: str) -> Path:
    if _JOB_ID.fullmatch(job_id) is None:
        raise VideoStoryError("Invalid local video job")
    root = data_root / "video" / "local-renders"
    candidate = (root / job_id).absolute()
    if candidate.parent != root.absolute():
        raise VideoStoryError("Invalid local video job")
    return candidate


def local_video_artifact(data_root: Path, job_id: str, artifact: str) -> Path:
    if artifact not in _ARTIFACTS:
        raise VideoStoryError("Unknown local video artifact")
    path = local_video_job_directory(data_root, job_id) / _ARTIFACTS[artifact]
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise VideoStoryError("Local video artifact is unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise VideoStoryError("Local video artifact is unavailable")
    return path


def load_local_video_job(data_root: Path, job_id: str) -> LocalVideoJobRecord:
    path = local_video_job_directory(data_root, job_id) / _ARTIFACTS["job"]
    try:
        return LocalVideoJobRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VideoStoryError("Local video job is unavailable") from exc


class LocalVideoJobManager:
    """Render one private local story at a time while the desktop UI remains responsive."""

    def __init__(self, *, max_retained_jobs: int = 10) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="soloscale-video")
        self._lock = threading.Lock()
        self._jobs: dict[str, _LiveVideoJob] = {}
        self._max_retained_jobs = max(1, max_retained_jobs)

    def submit(
        self,
        *,
        data_root: Path,
        repository_root: Path,
        content_run_id: str | None = None,
    ) -> str:
        now = datetime.now(UTC)
        job_id = f"video-story-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:10]}"
        job_dir = local_video_job_directory(data_root, job_id)
        _ensure_private_directory(job_dir, parents=True)
        record = LocalVideoJobRecord(
            job_id=job_id,
            story_id=(
                f"content-{content_run_id}"
                if content_run_id is not None
                else _STORY_ID
            ),
            content_run_id=content_run_id,
            phase="QUEUED",
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
        )
        _atomic_private_write(job_dir / _ARTIFACTS["job"], record.model_dump_json(indent=2) + "\n")
        live = _LiveVideoJob(
            persisted=record,
            created_at_perf=time.perf_counter(),
            phase_started_at=time.perf_counter(),
        )
        with self._lock:
            self._prune_locked()
            self._jobs[job_id] = live
        self._executor.submit(
            self._execute, data_root, repository_root, job_id, content_run_id
        )
        return job_id

    def get(self, data_root: Path, job_id: str) -> LocalVideoJobSnapshot | None:
        if _JOB_ID.fullmatch(job_id) is None:
            return None
        with self._lock:
            live = self._jobs.get(job_id)
        if live is not None:
            with live.lock:
                return self._snapshot(live)
        try:
            record = load_local_video_job(data_root, job_id)
        except VideoStoryError:
            return None
        try:
            created_at = datetime.fromisoformat(record.created_at)
            updated_at = datetime.fromisoformat(record.updated_at)
            total_ms = max(0, int((updated_at - created_at).total_seconds() * 1000))
        except ValueError:
            total_ms = max(record.stage_durations_ms.values(), default=0)
        return LocalVideoJobSnapshot(
            job_id=record.job_id,
            story_id=record.story_id,
            phase=record.phase,
            created_at=record.created_at,
            stage_durations_ms=dict(record.stage_durations_ms),
            total_elapsed_ms=total_ms,
            output_sha256=record.output_sha256,
            output_duration_seconds=record.output_duration_seconds,
            audio_included=record.audio_included,
            error_code=record.error_code,
            error_message=record.error_message,
        )

    def latest(self, data_root: Path) -> LocalVideoJobSnapshot | None:
        """Return the newest persisted local render so navigation can resume it."""
        root = data_root / "video" / "local-renders"
        try:
            job_ids = sorted(
                (
                    path.name
                    for path in root.iterdir()
                    if not path.is_symlink()
                    and path.is_dir()
                    and _JOB_ID.fullmatch(path.name) is not None
                ),
                reverse=True,
            )
        except OSError:
            return None
        for job_id in job_ids:
            snapshot = self.get(data_root, job_id)
            if snapshot is not None:
                return snapshot
        return None

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _execute(
        self,
        data_root: Path,
        repository_root: Path,
        job_id: str,
        content_run_id: str | None,
    ) -> None:
        try:
            self._transition(data_root, job_id, "PREPARING_STORY")
            story_started = time.perf_counter()
            story = (
                build_content_run_story(
                    data_root=data_root, content_run_id=content_run_id
                )
                if content_run_id is not None
                else build_resume_latency_story(
                    data_root=data_root, repository_root=repository_root
                )
            )
            job_dir = local_video_job_directory(data_root, job_id)
            _write_story_sources(job_dir, story)
            self._record_timing(data_root, job_id, "story_ms", story_started)

            self._transition(data_root, job_id, "PREPARING_ASSETS")
            assets_started = time.perf_counter()
            audio_data = _create_local_narration(job_dir, story)
            self._record_timing(data_root, job_id, "assets_ms", assets_started)

            self._transition(data_root, job_id, "RENDERING")
            render_started = time.perf_counter()
            audio_included = _render_with_remotion(
                job_dir=job_dir,
                repository_root=repository_root,
                story=story,
                audio_data=audio_data,
            )
            render_ms = int((time.perf_counter() - render_started) * 1000)
            self._set_timing(data_root, job_id, "render_ms", render_ms)
            output_sha256, duration = _write_render_receipt(
                job_dir=job_dir,
                story=story,
                render_ms=render_ms,
                audio_included=audio_included,
            )
            self._transition(
                data_root,
                job_id,
                "COMPLETE",
                output_sha256=output_sha256,
                output_duration_seconds=duration,
                audio_included=audio_included,
            )
        except Exception as exc:
            self._transition(
                data_root,
                job_id,
                "FAILED",
                error_code=type(exc).__name__,
                error_message=str(exc) or "Local video render failed safely",
            )

    def _record_timing(self, data_root: Path, job_id: str, name: str, started: float) -> None:
        self._set_timing(data_root, job_id, name, int((time.perf_counter() - started) * 1000))

    def _set_timing(self, data_root: Path, job_id: str, name: str, elapsed_ms: int) -> None:
        with self._lock:
            live = self._jobs[job_id]
        with live.lock:
            live.persisted.stage_durations_ms[name] = max(0, elapsed_ms)
            self._persist(data_root, live.persisted)

    def _transition(
        self,
        data_root: Path,
        job_id: str,
        phase: VideoStoryPhase,
        *,
        output_sha256: str | None = None,
        output_duration_seconds: float | None = None,
        audio_included: bool | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        now_perf = time.perf_counter()
        with self._lock:
            live = self._jobs[job_id]
        with live.lock:
            previous = live.persisted.phase.lower()
            if live.persisted.phase != "QUEUED":
                live.persisted.stage_durations_ms.setdefault(
                    f"{previous}_ms", int((now_perf - live.phase_started_at) * 1000)
                )
            live.phase_started_at = now_perf
            live.persisted.phase = phase
            live.persisted.updated_at = datetime.now(UTC).isoformat()
            if output_sha256 is not None:
                live.persisted.output_sha256 = output_sha256
            if output_duration_seconds is not None:
                live.persisted.output_duration_seconds = output_duration_seconds
            if audio_included is not None:
                live.persisted.audio_included = audio_included
            live.persisted.error_code = error_code
            live.persisted.error_message = error_message
            if phase in {"COMPLETE", "FAILED"}:
                live.finished_at_perf = now_perf
            self._persist(data_root, live.persisted)

    def _persist(self, data_root: Path, record: LocalVideoJobRecord) -> None:
        path = local_video_job_directory(data_root, record.job_id) / _ARTIFACTS["job"]
        _atomic_private_write(path, record.model_dump_json(indent=2) + "\n")

    def _snapshot(self, live: _LiveVideoJob) -> LocalVideoJobSnapshot:
        end = live.finished_at_perf or time.perf_counter()
        return LocalVideoJobSnapshot(
            job_id=live.persisted.job_id,
            story_id=live.persisted.story_id,
            phase=live.persisted.phase,
            created_at=live.persisted.created_at,
            stage_durations_ms=dict(live.persisted.stage_durations_ms),
            total_elapsed_ms=max(0, int((end - live.created_at_perf) * 1000)),
            output_sha256=live.persisted.output_sha256,
            output_duration_seconds=live.persisted.output_duration_seconds,
            audio_included=live.persisted.audio_included,
            error_code=live.persisted.error_code,
            error_message=live.persisted.error_message,
        )

    def _prune_locked(self) -> None:
        completed = [
            item for item in self._jobs.values() if item.persisted.phase in {"COMPLETE", "FAILED"}
        ]
        completed.sort(key=lambda item: item.persisted.created_at)
        while len(self._jobs) >= self._max_retained_jobs and completed:
            stale = completed.pop(0)
            self._jobs.pop(stale.persisted.job_id, None)


def local_video_download_names() -> dict[str, str]:
    return dict(_ARTIFACTS)


__all__ = [
    "EngineeringStory",
    "LocalVideoJobManager",
    "LocalVideoJobSnapshot",
    "VideoStoryError",
    "build_resume_latency_story",
    "load_local_video_job",
    "local_video_artifact",
    "local_video_download_names",
]
