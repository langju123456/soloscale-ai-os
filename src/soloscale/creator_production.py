"""Canonical Creator production artifacts and exact-account queue records."""

from __future__ import annotations

import hashlib
import json
import stat
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from soloscale.content_models import ContentReviewDecision
from soloscale.content_workspace import (
    approved_content_artifact,
    content_run_directory,
    load_content_review,
    load_content_run,
)
from soloscale.media_profile import MediaProfileError
from soloscale.platform_accounts import eligible_publish_identities
from soloscale.resume_workspace import ResumeWorkspaceStorageError, _atomic_private_write
from soloscale.voice_provider import VoiceProviderError

ArtifactType = Literal["ARTICLE", "THREAD", "VIDEO"]
ArtifactPlatform = Literal["linkedin", "x", "youtube", "douyin"]
ProductionOutput = Literal["ARTICLE", "VIDEO"]
ProductionPhase = Literal[
    "QUEUED",
    "GENERATING_CONTENT",
    "RENDERING_VIDEO",
    "READY",
    "AI_NOT_EXECUTED",
    "FAILED",
]
QueueStatus = Literal["DRAFT", "READY", "PUBLISHED", "FAILED"]
_RUNNING_PHASES = frozenset({"QUEUED", "GENERATING_CONTENT", "RENDERING_VIDEO"})

_PROJECT_ROOT = "creator-projects"
_ARTIFACT_ROOT = "creator-artifacts"
_QUEUE_ROOT = "creator-publish-queue"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreatorProductionRequest(_StrictModel):
    source_kind: Literal["STORY", "CREATE"]
    source_story_id: str | None = None
    outputs: list[ProductionOutput] = Field(min_length=1, max_length=2)
    language: Literal["English", "中文"]
    ai_editorial: bool
    add_to_queue: bool = False


class CreatorProductionJob(_StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: str
    content_project_id: str
    request: CreatorProductionRequest
    phase: ProductionPhase
    created_at: str
    updated_at: str
    content_run_id: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    queue_item_ids: list[str] = Field(default_factory=list)
    model_calls: int = Field(default=0, ge=0)
    provider: str | None = None
    model: str | None = None
    stage: str | None = None
    timeout_seconds: int | None = Field(default=None, ge=0)
    error_code: str | None = None
    error_cause: str | None = None


class PublicationArtifact(_StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    artifact_id: str
    content_project_id: str
    source_run_id: str
    artifact_type: ArtifactType
    platform: ArtifactPlatform
    locale: Literal["zh-CN", "en-US"]
    source_path: str
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    truth_status: Literal["VALIDATED"] = "VALIDATED"
    review_status: Literal["DRAFT", "APPROVED"]
    status: Literal["READY"] = "READY"
    created_at: str


class PublishQueueItem(_StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    queue_item_id: str
    artifact_id: str
    channel_account_id: str
    channel_account_name: str
    platform: ArtifactPlatform
    publish_mode: Literal["NOW", "SCHEDULED"] = "NOW"
    scheduled_at: str | None = None
    status: QueueStatus
    publication_receipt_id: str | None = None
    created_at: str


class CreatorProductionError(ValueError):
    """Raised when Creator production cannot preserve its artifact boundary."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def job_elapsed_seconds(job: CreatorProductionJob, *, now: datetime | None = None) -> int:
    """Return whole elapsed seconds for a persisted production job.

    A running job uses a live clock from its creation time. A terminal job uses
    its last persisted transition (``updated_at``) so its displayed duration is
    stable across refresh, navigation, and history rendering.
    """

    try:
        started = datetime.fromisoformat(job.created_at)
    except ValueError:
        return 0
    if job.phase in _RUNNING_PHASES:
        reference = now or datetime.now(UTC)
    else:
        try:
            reference = datetime.fromisoformat(job.updated_at)
        except ValueError:
            reference = started
    elapsed = (reference - started).total_seconds()
    return max(0, int(elapsed))


def _creator_error_cause(exc: BaseException) -> str | None:
    """Normalize one safe, actionable cause from a Creator execution failure.

    The raw internal code stays on ``error_code``; this adds only the narrow
    causes the user can act on (currently: missing voice configuration).
    """

    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, (MediaProfileError, VoiceProviderError)):
            return "VOICE_NOT_CONFIGURED"
        current = current.__cause__
    return None


def _private_root(data_root: Path, name: str) -> Path:
    root = data_root.absolute() / name
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise CreatorProductionError("Creator storage is unsafe")
        root.chmod(0o700)
    except OSError as exc:
        raise CreatorProductionError("Creator storage is unavailable") from exc
    return root


def _write_model(path: Path, value: BaseModel) -> None:
    try:
        _atomic_private_write(
            path,
            json.dumps(value.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        )
    except (OSError, ResumeWorkspaceStorageError) as exc:
        raise CreatorProductionError("Creator state could not be saved") from exc


def _load_model(path: Path, model_type: type[_StrictModel]) -> _StrictModel:
    try:
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise CreatorProductionError("Creator state is unsafe")
        return model_type.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        if isinstance(exc, CreatorProductionError):
            raise
        raise CreatorProductionError("Creator state is invalid") from exc


def _artifact_id(run_id: str, platform: ArtifactPlatform) -> str:
    return f"artifact-{run_id}-{platform}"


def _regular_source(run_dir: Path, relative: str) -> Path:
    path = run_dir / relative
    try:
        path.relative_to(run_dir)
        metadata = path.lstat()
    except (ValueError, OSError) as exc:
        raise CreatorProductionError("Publication artifact is unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
        raise CreatorProductionError("Publication artifact is unsafe")
    return path


def _save_artifact(data_root: Path, artifact: PublicationArtifact) -> PublicationArtifact:
    path = _private_root(data_root, _ARTIFACT_ROOT) / f"{artifact.artifact_id}.json"
    _write_model(path, artifact)
    return artifact


def _artifact_from_path(
    *,
    data_root: Path,
    content_project_id: str,
    run_id: str,
    artifact_type: ArtifactType,
    platform: ArtifactPlatform,
    path: Path,
    review_status: Literal["DRAFT", "APPROVED"],
) -> PublicationArtifact:
    run_dir = content_run_directory(data_root, run_id)
    try:
        relative = path.relative_to(run_dir).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ValueError) as exc:
        raise CreatorProductionError("Publication artifact could not be sealed") from exc
    run = load_content_run(data_root, run_id)
    locale = (
        run.locale_variant.locale
        if run.locale_variant is not None
        else ("zh-CN" if run.brief.language == "中文" else "en-US")
    )
    return _save_artifact(
        data_root,
        PublicationArtifact(
            artifact_id=_artifact_id(run_id, platform),
            content_project_id=content_project_id,
            source_run_id=run_id,
            artifact_type=artifact_type,
            platform=platform,
            locale=locale,
            source_path=relative,
            source_sha256=digest,
            review_status=review_status,
            created_at=_now(),
        ),
    )


def create_run_artifacts(
    *,
    data_root: Path,
    content_project_id: str,
    run_id: str,
    outputs: list[ProductionOutput],
) -> tuple[PublicationArtifact, ...]:
    """Project one ContentRun into persisted article/video artifacts."""

    run_dir = content_run_directory(data_root, run_id)
    review = load_content_review(data_root, run_id)
    review_status: Literal["DRAFT", "APPROVED"] = (
        "APPROVED"
        if review is not None and review[0].decision is ContentReviewDecision.APPROVED
        else "DRAFT"
    )
    artifacts: list[PublicationArtifact] = []
    if "ARTICLE" in outputs:
        if review_status == "APPROVED":
            _, linkedin_path, _ = approved_content_artifact(data_root, run_id, "linkedin")
            _, x_path, _ = approved_content_artifact(data_root, run_id, "x")
        else:
            linkedin_path = _regular_source(run_dir, "02_linkedin.md")
            x_path = _regular_source(run_dir, "03_x_post.md")
        artifacts.extend(
            (
                _artifact_from_path(
                    data_root=data_root,
                    content_project_id=content_project_id,
                    run_id=run_id,
                    artifact_type="ARTICLE",
                    platform="linkedin",
                    path=linkedin_path,
                    review_status=review_status,
                ),
                _artifact_from_path(
                    data_root=data_root,
                    content_project_id=content_project_id,
                    run_id=run_id,
                    artifact_type="THREAD",
                    platform="x",
                    path=x_path,
                    review_status=review_status,
                ),
            )
        )
    if "VIDEO" in outputs:
        video_sources: tuple[tuple[ArtifactPlatform, str], ...] = (
            ("youtube", "21_creator_video_youtube.mp4"),
            ("douyin", "10_creator_video.mp4"),
        )
        for platform, relative in video_sources:
            path = _regular_source(run_dir, relative)
            artifacts.append(
                _artifact_from_path(
                    data_root=data_root,
                    content_project_id=content_project_id,
                    run_id=run_id,
                    artifact_type="VIDEO",
                    platform=platform,
                    path=path,
                    review_status=review_status,
                )
            )
    return tuple(artifacts)


def sync_distribution_artifacts(
    data_root: Path, run_id: str
) -> tuple[PublicationArtifact, ...]:
    """Expose one sealed DistributionPackage through the canonical artifact store."""

    return create_run_artifacts(
        data_root=data_root,
        content_project_id=f"project-{run_id}",
        run_id=run_id,
        outputs=["ARTICLE", "VIDEO"],
    )


def load_publication_artifacts(
    data_root: Path, *, run_id: str | None = None
) -> tuple[PublicationArtifact, ...]:
    root = _private_root(data_root, _ARTIFACT_ROOT)
    result: list[PublicationArtifact] = []
    for path in sorted(root.glob("artifact-*.json"), reverse=True):
        artifact = PublicationArtifact.model_validate(_load_model(path, PublicationArtifact))
        if run_id is None or artifact.source_run_id == run_id:
            result.append(artifact)
    return tuple(result)


def load_publish_queue(data_root: Path) -> tuple[PublishQueueItem, ...]:
    root = _private_root(data_root, _QUEUE_ROOT)
    return tuple(
        PublishQueueItem.model_validate(_load_model(path, PublishQueueItem))
        for path in sorted(root.glob("queue-*.json"), reverse=True)
    )


def load_creator_production_job(
    data_root: Path, job_id: str
) -> CreatorProductionJob | None:
    """Load one persisted ContentProject job without requiring the live manager."""

    path = data_root.absolute() / _PROJECT_ROOT / job_id / "project.json"
    if path.is_symlink() or not path.is_file():
        return None
    try:
        return CreatorProductionJob.model_validate(_load_model(path, CreatorProductionJob))
    except CreatorProductionError:
        return None


def load_creator_production_jobs(
    data_root: Path, *, limit: int = 12
) -> tuple[CreatorProductionJob, ...]:
    """Return recent persisted ContentProject jobs, newest first."""

    root = data_root.absolute() / _PROJECT_ROOT
    if root.is_symlink() or not root.is_dir():
        return ()
    jobs: list[CreatorProductionJob] = []
    for job_dir in root.iterdir():
        if job_dir.is_symlink() or not job_dir.is_dir():
            continue
        path = job_dir / "project.json"
        if path.is_symlink() or not path.is_file():
            continue
        try:
            jobs.append(
                CreatorProductionJob.model_validate(_load_model(path, CreatorProductionJob))
            )
        except CreatorProductionError:
            continue
    jobs.sort(key=lambda item: item.updated_at, reverse=True)
    return tuple(jobs[:limit])


def assign_artifact_to_account(
    *,
    data_root: Path,
    artifact_id: str,
    channel_account_id: str,
) -> PublishQueueItem:
    artifacts = {item.artifact_id: item for item in load_publication_artifacts(data_root)}
    artifact = artifacts.get(artifact_id)
    if artifact is None:
        raise CreatorProductionError("Publication artifact is unavailable")
    if artifact.review_status != "APPROVED":
        raise CreatorProductionError("Approve this artifact before assigning a channel")
    identities = eligible_publish_identities(data_root).get(artifact.platform, ())
    identity = next(
        (item for item in identities if item.external_account_id == channel_account_id),
        None,
    )
    if identity is None:
        raise CreatorProductionError("Selected channel account is not publish-capable")
    existing = next(
        (
            item
            for item in load_publish_queue(data_root)
            if item.artifact_id == artifact_id
            and item.channel_account_id == channel_account_id
        ),
        None,
    )
    if existing is not None:
        return existing
    item = PublishQueueItem(
        queue_item_id=f"queue-{uuid4().hex[:16]}",
        artifact_id=artifact_id,
        channel_account_id=identity.external_account_id,
        channel_account_name=identity.display_name,
        platform=artifact.platform,
        status="READY",
        created_at=_now(),
    )
    _write_model(
        _private_root(data_root, _QUEUE_ROOT) / f"{item.queue_item_id}.json",
        item,
    )
    return item


class CreatorProductionJobManager:
    """Persist one semantic production job while executing it off the UI thread."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="soloscale-creator-production"
        )
        self._lock = threading.Lock()

    def submit(
        self,
        *,
        data_root: Path,
        request: CreatorProductionRequest,
        runner: Callable[[], str],
        renderer: Callable[[str], None] | None = None,
        provider: str | None = None,
        model: str | None = None,
        timeout_seconds: int | None = None,
    ) -> CreatorProductionJob:
        job_id = f"creator-job-{uuid4().hex[:16]}"
        job = CreatorProductionJob(
            job_id=job_id,
            content_project_id=f"project-{uuid4().hex[:16]}",
            request=request,
            phase="QUEUED",
            stage="Queued",
            provider=provider,
            model=model,
            timeout_seconds=timeout_seconds,
            created_at=_now(),
            updated_at=_now(),
        )
        self._save(data_root, job)
        self._executor.submit(self._execute, data_root, job_id, runner, renderer)
        return job

    def get(self, data_root: Path, job_id: str) -> CreatorProductionJob | None:
        path = _private_root(data_root, _PROJECT_ROOT) / job_id / "project.json"
        if not path.exists():
            return None
        return CreatorProductionJob.model_validate(_load_model(path, CreatorProductionJob))

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _save(self, data_root: Path, job: CreatorProductionJob) -> None:
        with self._lock:
            root = _private_root(data_root, _PROJECT_ROOT) / job.job_id
            try:
                root.mkdir(mode=0o700, exist_ok=True)
                root.chmod(0o700)
            except OSError as exc:
                raise CreatorProductionError("ContentProject storage is unavailable") from exc
            _write_model(root / "project.json", job)

    def _transition(
        self,
        data_root: Path,
        job: CreatorProductionJob,
        phase: ProductionPhase,
        **updates: object,
    ) -> CreatorProductionJob:
        updated = job.model_copy(
            update={"phase": phase, "updated_at": _now(), **updates}
        )
        self._save(data_root, updated)
        return updated

    def _execute(
        self,
        data_root: Path,
        job_id: str,
        runner: Callable[[], str],
        renderer: Callable[[str], None] | None,
    ) -> None:
        job = self.get(data_root, job_id)
        if job is None:
            return
        try:
            job = self._transition(
                data_root,
                job,
                "GENERATING_CONTENT",
                stage=(
                    "AI generation"
                    if job.request.ai_editorial
                    else "Template generation"
                ),
            )
            run_id = runner()
            run = load_content_run(data_root, run_id)
            model_calls = int(run.model_used)
            if job.request.ai_editorial and model_calls == 0:
                self._transition(
                    data_root,
                    job,
                    "AI_NOT_EXECUTED",
                    content_run_id=run_id,
                    model_calls=0,
                    error_code="AI_NOT_EXECUTED",
                )
                return
            job = self._transition(
                data_root,
                job,
                "GENERATING_CONTENT",
                content_run_id=run_id,
                model_calls=model_calls,
            )
            if "VIDEO" in job.request.outputs:
                if renderer is None:
                    raise CreatorProductionError("Video renderer is unavailable")
                job = self._transition(
                    data_root, job, "RENDERING_VIDEO", stage="Video rendering"
                )
                renderer(run_id)
            artifacts = create_run_artifacts(
                data_root=data_root,
                content_project_id=job.content_project_id,
                run_id=run_id,
                outputs=job.request.outputs,
            )
            queue_ids: list[str] = []
            if job.request.add_to_queue:
                eligible = eligible_publish_identities(data_root)
                for artifact in artifacts:
                    accounts = eligible.get(artifact.platform, ())
                    if artifact.review_status == "APPROVED" and len(accounts) == 1:
                        queue_ids.append(
                            assign_artifact_to_account(
                                data_root=data_root,
                                artifact_id=artifact.artifact_id,
                                channel_account_id=accounts[0].external_account_id,
                            ).queue_item_id
                        )
            self._transition(
                data_root,
                job,
                "READY",
                stage="Artifacts sealed",
                artifact_ids=[item.artifact_id for item in artifacts],
                queue_item_ids=queue_ids,
            )
        except Exception as exc:
            error_code = (
                "AI_NOT_EXECUTED"
                if str(exc) == "AI_NOT_EXECUTED"
                else type(exc).__name__.upper()
            )
            error_cause = _creator_error_cause(exc)
            current = self.get(data_root, job_id) or job
            self._transition(
                data_root,
                current,
                "AI_NOT_EXECUTED" if error_code == "AI_NOT_EXECUTED" else "FAILED",
                stage="AI generation" if error_code == "AI_NOT_EXECUTED" else "Failed",
                error_code=error_code,
                error_cause=error_cause,
            )


def wait_for_creator_job(
    manager: CreatorProductionJobManager,
    data_root: Path,
    job_id: str,
    *,
    timeout: float = 5.0,
) -> CreatorProductionJob:
    """Small deterministic helper for focused tests."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.get(data_root, job_id)
        if job is not None and job.phase not in {
            "QUEUED",
            "GENERATING_CONTENT",
            "RENDERING_VIDEO",
        }:
            return job
        time.sleep(0.01)
    raise CreatorProductionError("Creator production job did not finish")
