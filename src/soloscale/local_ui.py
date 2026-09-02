# ruff: noqa: E501
from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email import policy
from email.parser import BytesParser
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from soloscale.application_record import (
    ApplicationStatus,
    list_application_records,
    update_application_status,
)
from soloscale.casebook_models import EvidenceKind, LearningCase
from soloscale.casebook_store import CasebookError, CasebookStore
from soloscale.content_canon import load_month_one_canon
from soloscale.content_distribution import (
    ContentDistributionError,
    prepare_distribution_package,
)
from soloscale.content_models import ContentReviewDecision
from soloscale.content_ui import (
    ContentFormResult,
    content_page,
    editorial_publishing_page,
    run_content_form,
    run_month_one_story,
    run_story_candidate,
)
from soloscale.content_workspace import (
    ContentWorkspaceError,
    content_download,
    load_content_review,
    load_content_run,
    save_content_review,
)
from soloscale.creator_accounts import (
    CreatorAccountError,
    creator_accounts_page,
    normalize_account,
    save_creator_account,
)
from soloscale.creator_production import (
    CreatorProductionError,
    CreatorProductionJob,
    CreatorProductionJobManager,
    CreatorProductionRequest,
    assign_artifact_to_account,
)
from soloscale.creator_workspace import creator_history_page, creator_overview_page
from soloscale.desktop_credentials import (
    DesktopCredentialError,
    configure_desktop_credentials_from_stdin,
    github_access_token,
    github_access_token_is_configured,
    heygen_api_key_is_configured,
    openai_api_key,
    openai_api_key_is_configured,
)
from soloscale.editorial_publishing_handoff import (
    EditorialChannel,
    EditorialPublishingError,
    editorial_image_preview,
    preview_editorial_day,
    publish_editorial_preview,
)
from soloscale.evidence_hub import EvidenceHub, EvidenceHubError
from soloscale.evidence_ui import (
    ensure_local_project_evidence,
    evidence_page,
    refresh_evidence_catalog,
)
from soloscale.github_connect import (
    GitHubConnectError,
    GitHubConnectionState,
    GitHubConnectionStore,
    GitHubReadOnlyClient,
)
from soloscale.integration_status import connected_service_statuses
from soloscale.knowledge_models import RetrievalHit
from soloscale.knowledge_store import (
    KnowledgeStore,
    KnowledgeStoreError,
)
from soloscale.learning_practice import (
    ExerciseStatus,
    ExerciseType,
    LearningExercise,
    PracticeLanguage,
    capability_requirement,
    create_practice_workspace,
    generate_practice_exercise,
    ingest_practice_completion,
    list_exercises,
    load_exercise,
    open_practice_workspace,
    practice_workspace_root,
    save_exercise,
)
from soloscale.learning_traceability import (
    DEFAULT_TARGET_REQUIREMENT,
    LearningFixtureAnchorError,
    LearningTraceabilityError,
    inspect_learning_project,
    load_interview_anchor_pack,
    missing_learning_case_anchors,
    run_learning_traceability,
    save_learning_response,
)
from soloscale.media_cost import (
    BudgetPolicy,
    MediaCostError,
    load_budget_policy,
    save_budget_policy,
)
from soloscale.media_profile import (
    MediaProfile,
    MediaProfileError,
    load_media_profile_settings,
    save_media_profile,
)
from soloscale.media_quality import (
    MediaQualityChecklist,
    MediaQualityError,
    save_media_quality_review,
)
from soloscale.model_gateway import (
    GatewayConfigurationState,
    ModelGateway,
    ModelGatewayInvalidResponse,
    ModelGatewayNotConfigured,
    ModelGatewayTransportError,
    ModelProviderId,
    model_gateway_for,
)
from soloscale.platform_accounts import (
    PROVIDERS,
    PlatformAccountError,
    cancel_authorization_attempt,
    complete_authorization_response,
    disconnect_identity,
    load_authorization_attempt,
    load_developer_config,
    save_developer_config,
    start_authorization_attempt,
)
from soloscale.presenter_assets import (
    MAX_PRESENTER_ASSET_BYTES,
    PresenterAssetCategory,
    PresenterAssetError,
    PresenterAssetKind,
    PresenterLayout,
    import_presenter_asset,
    save_presenter_preferences,
)
from soloscale.reference_video import (
    MAX_REFERENCE_VIDEO_BYTES,
    ReferenceVideoError,
    analyze_reference_video,
)
from soloscale.resume_docx import (
    ResumeTemplateError,
    TailoredDocx,
    apply_resume_expert_review,
    apply_resume_template_structure,
    extract_candidate_profile,
    read_template_paragraphs,
    request_resume_expert_review,
    tailor_resume_docx,
    tailor_resume_docx_with_gateway,
)
from soloscale.resume_evidence_pack import (
    build_candidate_evidence_pack,
    build_resume_evidence_retrieval_trace,
)
from soloscale.resume_gateway_boundary import (
    ExtractedResumeUpload,
    ResumeFunnelEventType,
    ResumeUploadError,
    ResumeUploadRole,
    SelectedResumeFile,
    extract_selected_resume_files,
    normalize_text_resume_to_docx,
    record_resume_funnel_event,
)
from soloscale.resume_models import (
    CandidateProfile,
    InterviewDefenseRecord,
    ResumeClaimProvenance,
    ResumeClaimVerificationStatus,
    ResumeExpertReviewResult,
    ResumeHiringSignalReceipt,
    ResumeMode,
    ResumeProvenanceReceipt,
    build_resume_atomic_facts,
)
from soloscale.resume_template_intake import (
    ResumeTemplateReceipt,
    detect_resume_language,
    inspect_template_file,
    inspect_template_html,
    inspect_template_url,
    template_metadata_from_receipt,
)
from soloscale.resume_workspace import (
    ResumeWorkspaceStorageError,
    _atomic_private_write,
    _atomic_private_write_bytes,
    _reject_symlink_ancestry,
    load_interview_defense_records,
    map_interview_defense_bullet,
)
from soloscale.resume_workspace import run_resume_workspace as execute_resume_workspace
from soloscale.runtime_paths import resolve_runtime_paths, source_data_root
from soloscale.story_mining import (
    StoryCandidate,
    StoryMiningError,
    load_story_candidates,
    mine_story_candidates,
)
from soloscale.ui_shell import (
    DEFAULT_UI_LOCALE,
    UILocale,
    normalize_ui_locale,
    render_app_shell,
    ui_bool,
    ui_display_value,
    ui_text,
    ui_url,
)
from soloscale.video_factory import (
    CreatorVideoError,
    CreatorVideoJobManager,
    creator_video_runtime_available,
    import_avatar_segment,
    prepare_heygen_handoff,
    render_creator_video,
)
from soloscale.video_generation import (
    GoogleVeoClient,
    VideoGenerationError,
    VideoGenerationRequest,
    create_job,
    load_job,
    provider_status,
    save_job,
)
from soloscale.video_story import (
    LocalVideoJobManager,
    LocalVideoJobSnapshot,
    VideoStoryError,
    local_video_artifact,
    local_video_download_names,
)
from soloscale.work_ui import (
    CodexImportJobManager,
    WorkContextError,
    github_repositories_page,
    import_chatgpt_export,
    import_chatgpt_export_bytes,
    load_work_context,
    preflight_work_sources,
    refresh_selected_knowledge_sources,
    refresh_work_source,
    render_use_my_work,
    render_work_context_strip,
    work_page,
)
from soloscale.youtube_publishing import (
    YouTubePublishingError,
    YouTubePublishingJobManager,
    normalize_upload_request,
)

COMMAND_TIMEOUT_SECONDS = 120
MAX_UPLOAD_BYTES = 12 * 1024 * 1024
MAX_WORK_IMPORT_BYTES = 256 * 1024 * 1024
_REFERENCE_VIDEO_FORM_OVERHEAD_BYTES = 1024 * 1024
_PRESENTER_ASSET_FORM_OVERHEAD_BYTES = 1024 * 1024
_DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_RUN_ID_RE = re.compile(r"resume-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{10}")
_RESUME_JOB_ID_RE = re.compile(r"resume-job-[a-f0-9]{12}")
_PROVIDER_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}$")
_OLLAMA_DEFAULT_URL = "http://127.0.0.1:11434"
_OPENAI_DEFAULT_MODEL = "gpt-5"
_OPENAI_EXPERT_REVIEW_MODEL = "gpt-5.6-sol"
_OPENAI_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"
_DESKTOP_COOKIE_NAME = "soloscale_desktop_session"
_DESKTOP_TOKEN_RE = re.compile(r"[a-f0-9]{64}")
_DESKTOP_NONCE_RE = re.compile(r"[a-f0-9]{64}")
_DESKTOP_BOOTSTRAP_PATH = "/__desktop/bootstrap"
_DESKTOP_NONCE_HEADER = "X-SoloScale-Bootstrap-Nonce"
_DESKTOP_PROOF_HEADER = "X-SoloScale-Bootstrap-Proof"


@dataclass
class UIActionResult:
    name: str
    command: str
    return_code: int
    stdout: str
    stderr: str
    elapsed_ms: int
    diagnostics: dict[str, object] | None = None


@dataclass(frozen=True)
class UploadedFile:
    filename: str
    content_type: str
    content: bytes


@dataclass(frozen=True)
class ResumeJobSnapshot:
    job_id: str
    phase: str
    result: UIActionResult | None
    stage_durations_ms: dict[str, int]
    total_elapsed_ms: int
    preview_state: str
    failed_phase: str | None


@dataclass
class _ResumeJobRecord:
    job_id: str
    phase: str
    created_at: float
    phase_started_at: float
    result: UIActionResult | None = None
    stage_durations_ms: dict[str, int] = field(default_factory=dict)
    preview_state: str = "pending"
    failed_phase: str | None = None
    finished_at: float | None = None


class ResumeJobManager:
    """Run one local Resume generation at a time while keeping HTTP responsive."""

    def __init__(self, *, max_retained_jobs: int = 20) -> None:
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="soloscale-resume",
        )
        self._jobs: dict[str, _ResumeJobRecord] = {}
        self._max_retained_jobs = max(1, max_retained_jobs)

    def submit(
        self,
        *,
        form: dict[str, str],
        files: dict[str, UploadedFile],
        data_root: Path,
        repo_root: Path,
        evidence_repository_root: Path | None = None,
        gateway: ModelGateway | None,
        expert_gateway: ModelGateway | None = None,
        template_receipt: ResumeTemplateReceipt | None = None,
        allow_persistent_storage: bool = False,
        initial_timings_ms: dict[str, int] | None = None,
    ) -> str:
        now = time.perf_counter()
        job_id = f"resume-job-{uuid4().hex[:12]}"
        with self._lock:
            self._prune_locked()
            self._jobs[job_id] = _ResumeJobRecord(
                job_id=job_id,
                phase="QUEUED",
                created_at=now,
                phase_started_at=now,
                stage_durations_ms=dict(initial_timings_ms or {}),
            )
        self._executor.submit(
            self._execute,
            job_id,
            dict(form),
            dict(files),
            data_root,
            repo_root,
            evidence_repository_root,
            gateway,
            expert_gateway,
            template_receipt,
            allow_persistent_storage,
        )
        return job_id

    def get(self, job_id: str) -> ResumeJobSnapshot | None:
        if _RESUME_JOB_ID_RE.fullmatch(job_id) is None:
            return None
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return None
            return self._snapshot_locked(record)

    def latest(self) -> ResumeJobSnapshot | None:
        """Return the most recently submitted job for resumable UI navigation."""

        with self._lock:
            if not self._jobs:
                return None
            record = max(self._jobs.values(), key=lambda item: item.created_at)
            return self._snapshot_locked(record)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def record_timing(self, job_id: str, name: str, elapsed_ms: int) -> None:
        if not name.endswith("_ms"):
            raise ValueError("resume timing names must end in _ms")
        with self._lock:
            record = self._jobs.get(job_id)
            if record is not None:
                record.stage_durations_ms[name] = max(0, elapsed_ms)

    def _execute(
        self,
        job_id: str,
        form: dict[str, str],
        files: dict[str, UploadedFile],
        data_root: Path,
        repo_root: Path,
        evidence_repository_root: Path | None,
        gateway: ModelGateway | None,
        expert_gateway: ModelGateway | None,
        template_receipt: ResumeTemplateReceipt | None,
        allow_persistent_storage: bool,
    ) -> None:
        def progress(phase: str) -> None:
            self._transition(job_id, phase)

        def timing(name: str, elapsed_ms: int) -> None:
            self.record_timing(job_id, name, elapsed_ms)

        try:
            result = _run_user_resume(
                form,
                files,
                data_root,
                repo_root,
                evidence_repository_root=evidence_repository_root,
                gateway=gateway,
                expert_gateway=expert_gateway,
                template_receipt=template_receipt,
                allow_persistent_storage=allow_persistent_storage,
                create_preview=False,
                progress=progress,
                timing=timing,
            )
            if result.return_code != 0:
                self._transition(job_id, "FAILED", result=result)
                return
            self._transition(
                job_id,
                "DOCX_READY",
                result=result,
                preview_state="pending",
            )
            self._transition(
                job_id,
                "PREVIEWING",
                result=result,
                preview_state="rendering",
            )
            preview_started = time.perf_counter()
            try:
                preview_created = _finalize_resume_preview(data_root, result)
            except OSError:
                preview_created = False
            self.record_timing(
                job_id,
                "pdf_preview_ms",
                int((time.perf_counter() - preview_started) * 1000),
            )
            completed = UIActionResult(
                name=result.name,
                command=result.command,
                return_code=result.return_code,
                stdout=result.stdout,
                stderr=result.stderr,
                elapsed_ms=self._total_elapsed_ms(job_id),
            )
            self._transition(
                job_id,
                "COMPLETE",
                result=completed,
                preview_state="ready" if preview_created else "needs_attention",
            )
            snapshot = self.get(job_id)
            if snapshot is not None:
                try:
                    _persist_resume_job_timings(data_root, completed, snapshot)
                except OSError:
                    pass
        except Exception:
            failed = UIActionResult(
                "tailored-resume",
                "background resume generation",
                1,
                "",
                "Resume generation stopped safely; no fallback draft was created.",
                self._total_elapsed_ms(job_id),
            )
            self._transition(job_id, "FAILED", result=failed)

    def _transition(
        self,
        job_id: str,
        phase: str,
        *,
        result: UIActionResult | None = None,
        preview_state: str | None = None,
    ) -> None:
        now = time.perf_counter()
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return
            if record.phase != phase:
                elapsed_ms = max(0, int((now - record.phase_started_at) * 1000))
                key = f"{record.phase.casefold()}_ms"
                record.stage_durations_ms[key] = (
                    record.stage_durations_ms.get(key, 0) + elapsed_ms
                )
                if phase == "FAILED":
                    record.failed_phase = record.phase
                record.phase = phase
                record.phase_started_at = now
                if phase in {"COMPLETE", "FAILED"}:
                    record.finished_at = now
            if result is not None:
                record.result = result
            if preview_state is not None:
                record.preview_state = preview_state

    def _total_elapsed_ms(self, job_id: str) -> int:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return 0
            ended_at = record.finished_at or time.perf_counter()
            return max(0, int((ended_at - record.created_at) * 1000))

    def _snapshot_locked(self, record: _ResumeJobRecord) -> ResumeJobSnapshot:
        durations = dict(record.stage_durations_ms)
        if record.phase not in {"COMPLETE", "FAILED"}:
            current_key = f"{record.phase.casefold()}_ms"
            durations[current_key] = durations.get(current_key, 0) + max(
                0,
                int((time.perf_counter() - record.phase_started_at) * 1000),
            )
        ended_at = record.finished_at or time.perf_counter()
        return ResumeJobSnapshot(
            job_id=record.job_id,
            phase=record.phase,
            result=record.result,
            stage_durations_ms=durations,
            total_elapsed_ms=max(
                0,
                int((ended_at - record.created_at) * 1000),
            ),
            preview_state=record.preview_state,
            failed_phase=record.failed_phase,
        )

    def _prune_locked(self) -> None:
        if len(self._jobs) < self._max_retained_jobs:
            return
        terminal = [
            record
            for record in self._jobs.values()
            if record.phase in {"COMPLETE", "FAILED"}
        ]
        terminal.sort(key=lambda record: record.created_at)
        while len(self._jobs) >= self._max_retained_jobs and terminal:
            stale = terminal.pop(0)
            self._jobs.pop(stale.job_id, None)


@dataclass(frozen=True)
class AIProviderPreference:
    default_provider: ModelProviderId = ModelProviderId.SOLOSCALE_HOSTED
    ollama_model: str = "qwen3:8b"
    ollama_url: str = _OLLAMA_DEFAULT_URL
    openai_model: str = _OPENAI_DEFAULT_MODEL

    @property
    def provider(self) -> ModelProviderId:
        """Backward-compatible view over the one authoritative default."""

        return self.default_provider

    @property
    def model(self) -> str:
        if self.default_provider is ModelProviderId.OLLAMA:
            return self.ollama_model
        if self.default_provider is ModelProviderId.OPENAI_COMPATIBLE:
            return self.openai_model
        return "zai/glm-5.2"


@dataclass(frozen=True)
class OllamaReadiness:
    installed: bool
    reachable: bool
    model_available: bool
    models: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.installed and self.reachable and self.model_available


def _ai_provider_preference_path(data_root: Path) -> Path:
    return data_root.expanduser().absolute() / "settings" / "ai-provider.json"


def _load_ai_provider_preference(data_root: Path) -> AIProviderPreference:
    path = _ai_provider_preference_path(data_root)
    try:
        path.lstat()
        if path.is_symlink() or not path.is_file():
            return AIProviderPreference()
        payload = json.loads(path.read_text(encoding="utf-8"))
        provider = ModelProviderId(
            str(payload.get("default_ai_provider", payload.get("provider", "")))
        )
        legacy_model = str(payload.get("model", "")).strip()
        ollama_model = str(payload.get("ollama_model", legacy_model or "qwen3:8b")).strip()
        openai_model = str(payload.get("openai_model", _OPENAI_DEFAULT_MODEL)).strip()
        ollama_url = str(payload.get("ollama_url", _OLLAMA_DEFAULT_URL)).strip()
        if (
            not _PROVIDER_MODEL_RE.fullmatch(ollama_model)
            or not _PROVIDER_MODEL_RE.fullmatch(openai_model)
            or not _valid_ollama_url(ollama_url)
        ):
            return AIProviderPreference()
    except (FileNotFoundError, OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return AIProviderPreference()
    return AIProviderPreference(
        default_provider=provider,
        ollama_model=ollama_model,
        ollama_url=ollama_url,
        openai_model=openai_model,
    )


def _save_ai_provider_preference(
    data_root: Path,
    *,
    provider: str,
    model: str | None = None,
    ollama_url: str | None = None,
    openai_model: str | None = None,
    set_default: bool = True,
) -> AIProviderPreference:
    try:
        selected = ModelProviderId(provider)
    except ValueError as exc:
        raise ValueError("AI service selection is invalid") from exc
    current = _load_ai_provider_preference(data_root)
    selected_ollama_model = current.ollama_model
    if selected is ModelProviderId.OLLAMA and model is not None:
        selected_ollama_model = model.strip() or "qwen3:8b"
    selected_openai_model = current.openai_model
    if selected is ModelProviderId.OPENAI_COMPATIBLE:
        requested_openai_model = openai_model if openai_model is not None else model
        if requested_openai_model is not None:
            selected_openai_model = requested_openai_model.strip() or _OPENAI_DEFAULT_MODEL
    selected_ollama_url = (ollama_url or current.ollama_url).strip()
    if not _PROVIDER_MODEL_RE.fullmatch(selected_ollama_model):
        raise ValueError("Local model name is invalid")
    if not _PROVIDER_MODEL_RE.fullmatch(selected_openai_model):
        raise ValueError("OpenAI model name is invalid")
    if not _valid_ollama_url(selected_ollama_url):
        raise ValueError("Ollama URL must use the local loopback address")
    path = _ai_provider_preference_path(data_root)
    _reject_symlink_ancestry(path.parent)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    preference = AIProviderPreference(
        default_provider=selected if set_default else current.default_provider,
        ollama_model=selected_ollama_model,
        ollama_url=selected_ollama_url,
        openai_model=selected_openai_model,
    )
    _atomic_private_write(
        path,
        json.dumps(
            {
                "schema_version": "1.0",
                "default_ai_provider": preference.default_provider.value,
                "ollama_model": preference.ollama_model,
                "ollama_url": preference.ollama_url,
                "openai_model": preference.openai_model,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    return preference


def _apply_ai_provider_preference(form: dict[str, str], data_root: Path) -> None:
    preference = _load_ai_provider_preference(data_root)
    form["generation_mode"] = preference.provider.value
    form["provider_model"] = preference.model


def _valid_ollama_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost"}
        and parsed.username is None
        and parsed.password is None
        and parsed.query == ""
        and parsed.fragment == ""
        and parsed.path in {"", "/"}
        and parsed.port is not None
    )


def _ollama_readiness(
    preference: AIProviderPreference,
    *,
    opener: object | None = None,
) -> OllamaReadiness:
    installed = shutil.which("ollama") is not None or Path("/Applications/Ollama.app").is_dir()
    direct_opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
    ).open
    selected_opener = direct_opener if opener is None else opener
    request = urllib.request.Request(
        f"{preference.ollama_url.rstrip('/')}/api/tags",
        headers={"Accept": "application/json"},
    )
    try:
        with selected_opener(request, timeout=0.8) as response:  # type: ignore[operator]
            raw = response.read(512 * 1024 + 1)
        if len(raw) > 512 * 1024:
            raise ValueError
        payload = json.loads(raw.decode("utf-8"))
        raw_models = payload.get("models", [])
        if not isinstance(raw_models, list):
            raise ValueError
        models = tuple(
            sorted(
                {
                    str(item.get("name", item.get("model", ""))).strip()
                    for item in raw_models
                    if isinstance(item, dict)
                    and str(item.get("name", item.get("model", ""))).strip()
                }
            )
        )
    except (
        OSError,
        TimeoutError,
        TypeError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
        urllib.error.URLError,
    ):
        return OllamaReadiness(installed=installed, reachable=False, model_available=False)
    wanted = preference.ollama_model
    model_available = wanted in models or any(
        value.split(":", maxsplit=1)[0] == wanted.split(":", maxsplit=1)[0]
        for value in models
    )
    return OllamaReadiness(
        installed=installed,
        reachable=True,
        model_available=model_available,
        models=models,
    )


def _gateway_from_preference(preference: AIProviderPreference) -> ModelGateway:
    if preference.provider is ModelProviderId.OLLAMA:
        return model_gateway_for(
            preference.provider,
            model=preference.ollama_model,
            ollama_endpoint=preference.ollama_url,
        )
    if preference.provider is ModelProviderId.OPENAI_COMPATIBLE:
        return model_gateway_for(
            preference.provider,
            model=preference.openai_model,
            openai_endpoint=_OPENAI_CHAT_COMPLETIONS_URL,
            openai_api_key=openai_api_key(),
        )
    return model_gateway_for(preference.provider)


def _preference_provider_configured(preference: AIProviderPreference) -> bool:
    if preference.provider is ModelProviderId.OLLAMA:
        return _ollama_readiness(preference).ready
    if preference.provider is ModelProviderId.OPENAI_COMPATIBLE:
        return openai_api_key_is_configured()
    return (
        model_gateway_for(ModelProviderId.SOLOSCALE_HOSTED).descriptor.configuration_state
        is GatewayConfigurationState.CONFIGURED
    )


def _creator_generation_mode(
    preference: AIProviderPreference, form: dict[str, str], source_kind: str
) -> str:
    """Resolve one truthful generation mode for a Creator production request.

    A Story/Canon action has no inline mode selector, so when the saved provider is
    not configured we fall back to the deterministic template instead of showing a
    confusing AI_NOT_EXECUTED with no durable package.
    """

    explicit = form.get("generation_mode", "").strip()
    if explicit:
        return explicit
    if source_kind == "STORY" and not _preference_provider_configured(preference):
        return "template"
    return preference.provider.value


def _creator_job_provider_metadata(
    preference: AIProviderPreference, generation_mode: str
) -> tuple[str | None, str | None]:
    if generation_mode == "template":
        return "template", None
    if generation_mode == ModelProviderId.OLLAMA.value:
        return ModelProviderId.OLLAMA.value, preference.ollama_model
    if generation_mode == ModelProviderId.OPENAI_COMPATIBLE.value:
        return ModelProviderId.OPENAI_COMPATIBLE.value, preference.openai_model
    hosted_model = model_gateway_for(ModelProviderId.SOLOSCALE_HOSTED).descriptor.model
    return ModelProviderId.SOLOSCALE_HOSTED.value, hosted_model


def _ollama_cli_path() -> str | None:
    discovered = shutil.which("ollama")
    if discovered:
        return discovered
    bundled = Path("/Applications/Ollama.app/Contents/Resources/ollama")
    return str(bundled) if bundled.is_file() else None


def _openai_connection_status(
    preference: AIProviderPreference,
    *,
    opener: object | None = None,
) -> str:
    credential = openai_api_key()
    if credential is None:
        return "not-configured"
    direct_opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
    ).open
    selected_opener = direct_opener if opener is None else opener
    model_path = urllib.parse.quote(preference.openai_model, safe="")
    request = urllib.request.Request(
        f"https://api.openai.com/v1/models/{model_path}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {credential}",
        },
    )
    try:
        with selected_opener(request, timeout=8) as response:  # type: ignore[operator]
            status = int(getattr(response, "status", 200))
            response.read(1)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            return "unauthorized"
        if exc.code == 404:
            return "model-unavailable"
        return "test-failed"
    except (OSError, TimeoutError, TypeError, urllib.error.URLError):
        return "test-failed"
    return "ready" if 200 <= status < 300 else "test-failed"


@dataclass(frozen=True)
class FormSubmission:
    fields: dict[str, str]
    files: dict[str, UploadedFile]


def _local_video_panel(
    snapshot: LocalVideoJobSnapshot | None,
    *,
    available: bool,
    locale: UILocale,
) -> str:
    if snapshot is None:
        availability = (
            ""
            if available
            else f'<p class="notice warning">{_escape(ui_text(locale, "本地 Remotion 运行环境尚未准备好。", "The local Remotion runtime is not ready."))}</p>'
        )
        disabled = "" if available else " disabled"
        return f'''<section class="card local-story-card">
  <div class="provider-row"><span>{_escape(ui_text(locale, "本地证据故事 · v0.1", "Local evidence story · v0.1"))}</span><span class="status-badge">LOCAL</span></div>
  <h2>{_escape(ui_text(locale, "把一次真实工程教训做成成片", "Turn a measured engineering lesson into a finished video"))}</h2>
  <p>{_escape(ui_text(locale, "首个故事已从本机 Git checkpoint 与两次真实 Resume timing 回执中核验。渲染不会调用模型、不会联网、不会发布。", "The first story is verified from a local Git checkpoint and two real Resume timing receipts. Rendering uses no model, network, or publication."))}</p>
  <div class="story-preview">
    <strong>{_escape(ui_text(locale, "一个 AI 简历 App 卡住两分钟之后，我学到的系统设计", "What a two-minute AI resume freeze taught me about system design"))}</strong>
    <span>9:16 · 1080 × 1920 · 84s · 7 scenes</span>
  </div>
  <ol class="story-layers">
    <li>{_escape(ui_text(locale, "事实：两次 qwen3:8b 真实运行约 122.6s / 128.3s", "Fact: two real qwen3:8b runs took about 122.6s / 128.3s"))}</li>
    <li>{_escape(ui_text(locale, "决定：分离请求、后台任务、UI 与模型推理", "Decision: separate the request, job, UI, and inference lifecycles"))}</li>
    <li>{_escape(ui_text(locale, "意外：模型生成占总耗时超过 99%", "Surprise: model generation consumed more than 99% of total time"))}</li>
  </ol>
  {availability}
  <form method="post" action="/video/local/render">
    <input type="hidden" name="ui_locale" value="{locale}" />
    <label>{_escape(ui_text(locale, '内容包 run_id（可选）', 'Content run ID (optional)'))}
      <input name="content_run_id" placeholder="content-2026…" />
    </label>
    <p class="muted">{_escape(ui_text(locale, '填写后直接从该内容包渲染视频脚本与分镜，不再重新录入事实；留空则使用本地工程故事。', 'When filled, renders that content package video script and storyboard downstream without re-entering facts; empty keeps the local engineering story.'))}</p>
    <button class="primary"{disabled}>{_escape(ui_text(locale, "生成本地视频", "Render local video"))}</button>
  </form>
</section>'''

    progress = {
        "QUEUED": 8,
        "PREPARING_STORY": 22,
        "PREPARING_ASSETS": 38,
        "RENDERING": 68,
        "COMPLETE": 100,
        "FAILED": 100,
    }[snapshot.phase]
    labels = {
        "QUEUED": ui_text(locale, "等待渲染", "Queued"),
        "PREPARING_STORY": ui_text(locale, "核验并准备故事", "Preparing verified story"),
        "PREPARING_ASSETS": ui_text(locale, "生成旁白与字幕", "Preparing narration and captions"),
        "RENDERING": ui_text(locale, "正在渲染成片", "Rendering video"),
        "COMPLETE": ui_text(locale, "视频已完成", "Video complete"),
        "FAILED": ui_text(locale, "渲染未完成", "Render failed"),
    }
    elapsed = snapshot.total_elapsed_ms / 1000
    if snapshot.phase == "COMPLETE":
        base = f"/video/local/downloads/{snapshot.job_id}"
        result = f'''<video controls preload="metadata" src="{base}/video"></video>
<div class="video-actions">
  <a class="primary-button" href="{base}/video" download>{_escape(ui_text(locale, "下载 MP4", "Download MP4"))}</a>
  <a class="secondary-button" href="{base}/subtitles" download>{_escape(ui_text(locale, "下载字幕", "Download subtitles"))}</a>
  <a class="secondary-button" href="{base}/thumbnail" target="_blank">{_escape(ui_text(locale, "查看封面", "View thumbnail"))}</a>
</div>
<details><summary>{_escape(ui_text(locale, "完整内容包", "Complete output package"))}</summary>
  <ul class="artifact-list">
    <li><a href="{base}/story" download>Canonical story</a></li>
    <li><a href="{base}/narration" download>Narration script</a></li>
    <li><a href="{base}/manifest" download>Scene manifest</a></li>
    <li><a href="{base}/receipt" download>Render receipt</a></li>
  </ul>
</details>
<p class="privacy-note">{_escape(ui_text(locale, "本地渲染完成；没有联网，也没有发布。", "Rendered locally with no network call and no publication."))}</p>'''
    elif snapshot.phase == "FAILED":
        result = f'<p class="error">{_escape(snapshot.error_message or ui_text(locale, "本地视频渲染安全停止。", "Local video rendering stopped safely."))}</p>'
    else:
        result = f'''<p>{_escape(ui_text(locale, "渲染在后台继续。你可以打开 Resume、Learning 或 Content，任务不会被中断。", "Rendering continues in the background. You can use Resume, Learning, or Content without interrupting it."))}</p>
<script>window.setTimeout(() => window.location.reload(), 1500);</script>'''
    audio = ui_text(locale, "含中文旁白", "Chinese narration included") if snapshot.audio_included else ui_text(locale, "旁白将在资产准备完成后显示", "Narration status will appear after asset preparation")
    return f'''<section class="card local-story-card" data-phase="{snapshot.phase}">
  <div class="provider-row"><span>{_escape(ui_text(locale, "本地证据故事 · v0.1", "Local evidence story · v0.1"))}</span><span class="status-badge">{_escape(labels[snapshot.phase])}</span></div>
  <h2>{_escape(ui_text(locale, "一个 AI 简历 App 卡住两分钟之后，我学到的系统设计", "What a two-minute AI resume freeze taught me about system design"))}</h2>
  <progress value="{progress}" max="100"></progress>
  <p>{_escape(ui_text(locale, "已用时", "Elapsed"))}: {elapsed:.1f}s · {_escape(audio)}</p>
  {result}
</section>'''


def _video_page(
    data_root: Path,
    job_id: str | None = None,
    error: str | None = None,
    locale: UILocale = DEFAULT_UI_LOCALE,
    *,
    local_job: LocalVideoJobSnapshot | None = None,
    local_video_available: bool = False,
    content_run_id: str | None = None,
    content_data_root: Path | None = None,
) -> str:
    job = load_job(data_root, job_id) if job_id else None
    configuration = provider_status()
    downstream_run = None
    downstream_notice = ""
    if content_run_id and content_data_root is not None:
        try:
            downstream_run = load_content_run(content_data_root, content_run_id)
        except ContentWorkspaceError:
            downstream_run = None
    topic_value = ""
    script_value = ""
    if downstream_run is not None:
        topic_value = downstream_run.brief.topic
        script_value = downstream_run.drafts.video_script
        downstream_notice = f'''<p class="notice" role="status">{_escape(ui_text(locale, '这是下游视频生产页；主题与分镜已从上游内容包带入，无需重新录入。', 'This is the downstream video production page; topic and storyboard are carried over from the upstream content package and do not need to be re-entered.'))} <a href="{ui_url('/creator/create', locale, run_id=content_run_id or '')}">{_escape(ui_text(locale, '打开上游内容包', 'Open upstream package'))} →</a></p>'''
    detail = ""
    if job:
        outgoing = html.escape(json.dumps(job.request.external_payload(), indent=2), quote=True)
        detail = f"""<section class="card job-panel">
<span class="status-badge">{_escape(ui_text(locale, '等待你的确认', 'Awaiting your review'))}</span>
<h2>{_escape(ui_text(locale, '发送前预览', 'External submission preview'))}</h2>
<p>{_escape(ui_text(locale, '只有这份精炼的视频说明和你明确选择的证据摘录会离开这台 Mac；原始 ChatGPT 与 Codex 对话不会上传。', 'Only this distilled video brief and the evidence excerpts you explicitly selected will leave this Mac. Raw ChatGPT and Codex histories are excluded.'))}</p>
<pre>{outgoing}</pre>
<p>{_escape(ui_text(locale, '状态', 'Status'))}: <strong>{job.status}</strong> · {_escape(ui_text(locale, '预估费用', 'Estimated cost'))}: ${job.estimated_cost_usd:.2f}</p>"""
        if job.status == "AWAITING_APPROVAL" and configuration == "READY":
            detail += f"""<form method="post" action="/video/submit/{job.job_id}">
<input type="hidden" name="ui_locale" value="{locale}" />
<label>{_escape(ui_text(locale, '输入 PUBLISH，批准这一次 Vertex AI 生成', 'Type PUBLISH to authorize this one Vertex AI generation'))}
<input name="confirmation" required></label>
<button class="primary warning-action">{_escape(ui_text(locale, '提交到 Google Vertex AI', 'Submit to Google Vertex AI'))}</button></form>"""
        elif job.status == "AWAITING_APPROVAL":
            detail += (
                f'<p class="notice warning"><strong>{_escape(ui_text(locale, "Google Cloud 尚未配置", "Google Cloud is not configured"))}</strong> · '
                f'{_escape(ui_text(locale, "草稿已私有保存，配置完成后再提交即可。", "This private draft is saved and can be submitted later."))}</p>'
            )
        elif job.status in {"SUBMITTED", "RUNNING"}:
            detail += f"""<form method="post" action="/video/poll/{job.job_id}">
<input type="hidden" name="ui_locale" value="{locale}" />
<button class="secondary">{_escape(ui_text(locale, '刷新进度', 'Refresh progress'))}</button></form>"""
        elif job.status == "SUCCEEDED" and job.output_path:
            local_video = f"/video/downloads/{job.job_id}/output.mp4"
            detail += f'''<video controls src="{local_video}"></video>
<p><a href="{local_video}" download>{_escape(ui_text(locale, '下载生成的视频', 'Download generated video'))}</a></p>'''
        detail += "</section>"
    message = f'<p class="error">{html.escape(error)}</p>' if error else ""
    local_panel = _local_video_panel(
        local_job,
        available=local_video_available,
        locale=locale,
    )
    body = f"""{local_panel}<div class="video-grid cloud-video-grid">
<section class="card">
  <div class="provider-row"><span>Google Vertex AI · Veo</span><span class="status-badge">{_escape(configuration)}</span></div>
  {message}
  {downstream_notice}
  <form method="post" action="/video/prepare">
    <input type="hidden" name="ui_locale" value="{locale}" />
    <input type="hidden" name="content_run_id" value="{_escape(content_run_id or '')}" />
    <label>{_escape(ui_text(locale, '主题', 'Topic'))}<input name="topic" value="{_escape(topic_value)}" required></label>
    <label>{_escape(ui_text(locale, '脚本或视频设计文档', 'Script or video design document'))}<textarea name="script" required>{_escape(script_value)}</textarea></label>
    <details>
      <summary>{_escape(ui_text(locale, '可选：证据与来源', 'Optional: evidence and source'))}</summary>
      <label>{_escape(ui_text(locale, '证据 ID（每行一个）', 'Evidence IDs (one per line)'))}<textarea name="evidence_ids"></textarea></label>
      <label>{_escape(ui_text(locale, '明确选择的证据摘录（每行一个）', 'Explicitly selected evidence excerpts (one per line)'))}<textarea name="evidence_excerpts"></textarea></label>
      <label>SoloScale content run ID<input name="content_run_id" value="{_escape(content_run_id or '')}"></label>
    </details>
    <div class="video-settings">
      <label>{_escape(ui_text(locale, '平台', 'Platform'))}<input name="platform" value="Short video"></label>
      <label>{_escape(ui_text(locale, '成片语言', 'Video language'))}<input name="language" value="{_escape(ui_text(locale, '中文', 'English'))}"></label>
      <label>{_escape(ui_text(locale, '风格', 'Style'))}<input name="style" value="Cinematic product demo"></label>
    </div>
    <div class="privacy-note">{_escape(ui_text(locale, '先保存本地 brief，再展示完整外发内容和预估费用；你确认之前不会调用云端生成。', 'The brief is saved locally first. You will review the exact outbound data and estimated cost before any cloud generation call.'))}</div>
    <button class="primary">{_escape(ui_text(locale, '保存 brief 并预览外发内容', 'Save brief and preview outbound data'))}</button>
  </form>
</section>
{detail or f'''<section class="empty-state card"><span class="kicker">{_escape(ui_text(locale, '从这里开始', 'Start here'))}</span><h2>{_escape(ui_text(locale, '先准备，再确认，最后生成', 'Prepare, review, then generate'))}</h2><p>{_escape(ui_text(locale, '整个过程分成三个清晰步骤；云端费用和隐私边界不会藏在按钮后面。', 'The flow has three clear steps. Cloud cost and privacy boundaries are never hidden behind a button.'))}</p><ol class="empty-steps"><li><span class="step-number">1</span>{_escape(ui_text(locale, '准备视频 brief', 'Prepare the video brief'))}</li><li><span class="step-number">2</span>{_escape(ui_text(locale, '核对外发内容与费用', 'Review outbound data and cost'))}</li><li><span class="step-number">3</span>{_escape(ui_text(locale, '明确批准一次生成', 'Explicitly approve one generation'))}</li></ol></section>'''}
</div>"""
    return render_app_shell(
        active="video",
        locale=locale,
        current_url="/video",
        title=f"SoloScale · {ui_text(locale, '视频工作台', 'Creator Video')}",
        eyebrow=ui_text(locale, "视频工作台", "Creator video"),
        heading=ui_text(locale, "先看清会发送什么，再决定是否生成。", "Preview what leaves your Mac before you generate."),
        description=ui_text(locale, "用可信内容准备视频，并把云端隐私与费用决定留在你手里。", "Turn trusted content into video while keeping cloud privacy and cost decisions in your hands."),
        body=body,
        extra_css="""
.video-grid{display:grid;grid-template-columns:minmax(340px,.9fr) minmax(0,1.1fr);gap:22px;align-items:start}
.local-story-card{margin-bottom:22px}.local-story-card video{width:100%;max-height:660px;border-radius:18px;background:#081124;margin-top:18px}
.story-preview{display:flex;flex-direction:column;gap:8px;background:var(--surface-soft);border:1px solid var(--border);border-radius:16px;padding:16px;margin:18px 0}.story-preview span{color:var(--text-muted);font-size:13px}
.story-layers{display:grid;gap:8px;color:var(--text-muted);padding-left:22px}.local-story-card progress{width:100%;height:14px;accent-color:var(--accent)}
.video-actions{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0}.artifact-list{display:grid;gap:8px}.cloud-video-grid{margin-top:22px}
.provider-row{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:18px;color:var(--text-muted);font-size:13px}
.video-settings{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.job-panel video{width:100%;border-radius:16px;background:#101827}
.job-panel pre{max-height:420px;overflow:auto}
.warning-action{background:var(--warning);color:#fff}
details{border:1px solid var(--border);border-radius:14px;padding:13px}details summary{cursor:pointer;font-weight:800}
@media(max-width:900px){.video-grid,.video-settings{grid-template-columns:1fr}}
""",
    )


def _repo_root() -> Path:
    return resolve_runtime_paths().repository_root


def _write_readiness_file(path: Path, payload: dict[str, object]) -> None:
    """Atomically publish a private sidecar readiness record."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _desktop_readiness_proof(*, token: str, url: str, pid: int) -> str:
    """Bind a desktop readiness receipt to the private launch secret."""
    payload = f"1.0\n{url}\n{pid}".encode()
    return hmac.new(token.encode(), payload, hashlib.sha256).hexdigest()


def _desktop_bootstrap_request_proof(
    *, token: str, url: str, pid: int, nonce: str
) -> str:
    """Authenticate one exact desktop bootstrap request."""
    payload = (
        "soloscale.desktop.bootstrap.request.v1\n"
        f"POST\n{_DESKTOP_BOOTSTRAP_PATH}\n{url}\n{pid}\n{nonce}"
    ).encode()
    return hmac.new(token.encode(), payload, hashlib.sha256).hexdigest()


def _desktop_session_cookie(*, token: str, url: str, pid: int, nonce: str) -> str:
    """Derive a session bearer distinct from the private launch token."""
    payload = f"soloscale.desktop.session-cookie.v1\n{url}\n{pid}\n{nonce}".encode()
    digest = hmac.new(token.encode(), payload, hashlib.sha256).hexdigest()
    return f"v1_{digest}"


def _desktop_bootstrap_response_proof(
    *, token: str, url: str, pid: int, nonce: str, cookie: str
) -> str:
    """Authenticate the bootstrap response before the desktop shell trusts it."""
    cookie_hash = hashlib.sha256(cookie.encode()).hexdigest()
    payload = (
        "soloscale.desktop.bootstrap.response.v1\n"
        f"POST\n{_DESKTOP_BOOTSTRAP_PATH}\n200\n{url}\n{pid}\n{nonce}\n{cookie_hash}"
    ).encode()
    return hmac.new(token.encode(), payload, hashlib.sha256).hexdigest()


def _resolve_soloscale_command() -> tuple[list[str], dict[str, str]]:
    env = os.environ.copy()
    cli = shutil.which("soloscale")
    if cli is not None:
        return [cli], env

    env["PYTHONPATH"] = os.pathsep.join(
        [str(_repo_root() / "src"), env.get("PYTHONPATH", "")]
    ).strip(os.pathsep)
    return [sys.executable, "-m", "soloscale.cli"], env


def _run_command(command: list[str], cwd: Path) -> UIActionResult:
    start = time.perf_counter()
    cli_command, cli_env = _resolve_soloscale_command()
    full_command = cli_command + command
    try:
        completed = subprocess.run(
            full_command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            env=cli_env,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except OSError as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return UIActionResult(
            name=command[0],
            command=" ".join(full_command),
            return_code=1,
            stdout="",
            stderr=str(exc),
            elapsed_ms=elapsed_ms,
        )
    except subprocess.TimeoutExpired:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return UIActionResult(
            name=command[0],
            command=" ".join(full_command),
            return_code=124,
            stdout="",
            stderr=f"command timed out after {COMMAND_TIMEOUT_SECONDS}s",
            elapsed_ms=elapsed_ms,
        )

    elapsed_ms = int((time.perf_counter() - start) * 1000)
    return UIActionResult(
        name=command[0],
        command=" ".join(full_command),
        return_code=completed.returncode,
        stdout=completed.stdout.strip(),
        stderr=completed.stderr.strip(),
        elapsed_ms=elapsed_ms,
    )


def _split_path_list(raw: str) -> list[str]:
    return [item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()]


def _normalize_for_resume(value: str, *, limit: int = 260) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1].rstrip()}…"


def _extract_private_result_path(raw_stdout: str) -> Path | None:
    prefix = "Private result: "
    for line in raw_stdout.splitlines():
        if line.startswith(prefix):
            candidate = line[len(prefix) :].strip()
            if candidate:
                return Path(candidate).expanduser()
    return None


def _load_json_file(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _run_jd_resume_draft(
    form: dict[str, str], data_root: Path, repo_root: Path
) -> UIActionResult | None:
    del form, data_root, repo_root
    return UIActionResult(
        name="jd-resume-draft",
        command="jd-resume-draft",
        return_code=2,
        stdout="",
        stderr=(
            "该旧入口已停用：Evidence Agent 结果只能用于证据发现，不能直接生成简历事实。"
            "请使用 Resume Intelligence Workspace，并显式提供 Candidate Profile。"
        ),
        elapsed_ms=0,
    )


def _candidate_profile_from_form(form: dict[str, str]) -> CandidateProfile:
    """Keep supplied resume text as operator claims; never infer new personal facts."""
    base_lines = [
        line.strip(" -•\t") for line in form.get("candidate_base_resume", "").splitlines()
    ]
    return CandidateProfile(
        full_name=form.get("candidate_name", "").strip() or None,
        headline=form.get("candidate_headline", "").strip() or None,
        summary=form.get("candidate_summary", "").strip() or None,
        skills=_split_path_list(form.get("candidate_skills", "")),
        experience_bullets=[line for line in base_lines if line],
    )


def _run_resume_workspace(form: dict[str, str], data_root: Path, repo_root: Path) -> UIActionResult:
    job_description = form.get("job_description", "").strip()
    if not job_description:
        return UIActionResult("resume-workspace", "resume-workspace", 2, "", "JD 不能为空。", 0)
    try:
        mode = ResumeMode(form.get("resume_mode", ResumeMode.LOCAL_ONLY.value))
    except ValueError:
        return UIActionResult(
            "resume-workspace",
            "resume-workspace",
            2,
            "",
            "Resume mode 无效。请选择 Local-only 或 Hybrid research。",
            0,
        )
    if mode is ResumeMode.HYBRID:
        return UIActionResult(
            "resume-workspace",
            "resume-workspace",
            2,
            "",
            "Hybrid research provider 尚未配置；v0.1 不会执行网络调用。请选择 Local-only。",
            0,
        )
    try:
        _reject_symlink_ancestry(data_root)
        library_value = form.get("resume_library_root", "").strip()
        library_root = (
            Path(library_value or Path.home() / "Documents" / "Resume Applications")
            .expanduser()
            .absolute()
        )
        store = KnowledgeStore(data_root)
        requirements = form.get("job_description", "").splitlines()
        hits = []
        for requirement in requirements[:24]:
            if requirement.strip():
                hits.extend(store.search(requirement.strip(), limit=3))
        unique_hits = {hit.chunk_id: hit for hit in hits}
        run = execute_resume_workspace(
            data_root=data_root,
            job_description=job_description,
            candidate_profile=_candidate_profile_from_form(form),
            evidence_hits=list(unique_hits.values()),
            company_name=form.get("company_name", "").strip() or None,
            company_url=form.get("company_url", "").strip() or None,
            job_title=form.get("job_title", "").strip() or None,
            job_id=form.get("job_id", "").strip() or None,
            application_library_root=library_root,
            repository_root=repo_root,
            mode=mode,
        )
    except (KnowledgeStoreError, OSError, ValueError) as exc:
        return UIActionResult("resume-workspace", "local KnowledgeStore search", 1, "", str(exc), 0)
    run_path = data_root / "resume-runs" / run.run_id
    return UIActionResult(
        "resume-workspace", "local KnowledgeStore search", 0, f"Resume workspace: {run_path}", "", 0
    )


def _write_private_json(path: Path, payload: object) -> None:
    _atomic_private_write(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
    )


def _write_private_bytes(path: Path, content: bytes) -> None:
    _atomic_private_write_bytes(path, content)


def _new_resume_candidate_recorder(
    data_root: Path,
) -> tuple[Path, Callable[[dict[str, object]], None]]:
    """Create a private sink for one raw, explicitly non-submittable candidate."""

    _reject_symlink_ancestry(data_root)
    data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(data_root, 0o700)
    candidates_root = data_root / "resume-candidates"
    candidates_root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(candidates_root, 0o700)
    candidate_id = (
        f"resume-candidate-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:10]}"
    )
    path = candidates_root / f"{candidate_id}.json"
    created_at = datetime.now(UTC).isoformat()

    def record(payload: dict[str, object]) -> None:
        _write_private_json(
            path,
            {
                **payload,
                "candidate_id": candidate_id,
                "created_at": created_at,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )

    return path, record


def _expert_review_candidate_artifact(
    review: ResumeExpertReviewResult | None,
    *,
    status: Literal["PENDING_VALIDATION", "VALIDATED", "REJECTED"],
    failure_code: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "candidate_kind": "RESUME_EXPERT_REVIEW_PATCH",
        "status": status,
        "submission_status": "NOT_FOR_SUBMISSION",
        "label": (
            "REJECTED / NOT FOR SUBMISSION"
            if status == "REJECTED"
            else "EXPERT PATCH / NOT FOR SUBMISSION"
        ),
        "structured_candidate": (
            review.model_dump(mode="json") if review is not None else None
        ),
        "failure_code": failure_code,
    }


def _expert_review_failure_code(error: Exception) -> str:
    if isinstance(error, ModelGatewayNotConfigured):
        return "EXPERT_REVIEW_UNAVAILABLE"
    if isinstance(error, ModelGatewayTransportError):
        return "EXPERT_REVIEW_TRANSPORT_FAILED"
    if isinstance(error, ModelGatewayInvalidResponse):
        return "EXPERT_REVIEW_INVALID_RESPONSE"
    message = str(error)
    if "does not match the approved draft" in message:
        return "EXPERT_PATCH_SOURCE_MISMATCH"
    if "unsupported factual claims" in message:
        return "EXPERT_PATCH_UNSUPPORTED_FACT"
    return "EXPERT_PATCH_REVALIDATION_FAILED"


def _find_soffice() -> str | None:
    candidates = [
        os.environ.get("SOLOSCALE_SOFFICE"),
        shutil.which("soffice"),
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "/Applications/LibreOfficeDev.app/Contents/MacOS/soffice",
    ]
    runtime_root = Path.home() / ".cache" / "codex-runtimes"
    try:
        candidates.extend(
            str(path)
            for path in sorted(
                runtime_root.glob("*/dependencies/bin/override/soffice"), reverse=True
            )
        )
    except OSError:
        pass
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def _create_resume_pdf_preview(source: Path, target: Path) -> bool:
    """Render a private PDF preview when a local LibreOffice binary is available."""
    soffice = _find_soffice()
    if soffice is None:
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="soloscale-resume-preview-") as temp_dir:
            temp_root = Path(temp_dir)
            profile_dir = temp_root / "profile"
            output_dir = temp_root / "output"
            profile_dir.mkdir(mode=0o700)
            output_dir.mkdir(mode=0o700)
            completed = subprocess.run(
                [
                    soffice,
                    f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(output_dir),
                    str(source),
                ],
                capture_output=True,
                check=False,
                timeout=30,
            )
            generated = output_dir / f"{source.stem}.pdf"
            if completed.returncode != 0 or not generated.is_file():
                return False
            content = generated.read_bytes()
            if not content.startswith(b"%PDF-"):
                return False
            _write_private_bytes(target, content)
            return True
    except (OSError, subprocess.SubprocessError):
        return False


def _safe_filename_component(value: str | None, fallback: str) -> str:
    cleaned = re.sub(r"[^\w.-]+", "-", (value or "").strip(), flags=re.UNICODE)
    return cleaned.strip("-._")[:80] or fallback


def _user_resume_filename(
    profile: CandidateProfile,
    company_name: str | None,
    job_title: str | None,
    job_description: str,
) -> str:
    resolved_title = job_title or next(
        (line.strip() for line in job_description.splitlines() if line.strip()), "Role"
    )
    return "Resume_{}_{}_{}.docx".format(
        _safe_filename_component(profile.full_name, "Candidate"),
        _safe_filename_component(company_name, "Company"),
        _safe_filename_component(resolved_title, "Role"),
    )


def _resume_variant_filename(
    filename: str,
    output_locale: Literal["en-US", "zh-CN"],
    *,
    include_locale: bool,
) -> str:
    if not include_locale:
        return filename
    path = Path(filename)
    return f"{path.stem}_{output_locale}{path.suffix}"


_JOB_EVIDENCE_PRIORITY_TERMS = (
    "python",
    "rag",
    "retrieval",
    "reranking",
    "embeddings",
    "agentic",
    "structured",
    "evaluation",
    "fastapi",
    "flask",
    "django",
    "vertex",
    "gcp",
    "aws",
    "postgresql",
    "observability",
    "monitoring",
    "cicd",
)
_JOB_EVIDENCE_STOPWORDS = {
    "and",
    "the",
    "with",
    "using",
    "experience",
    "knowledge",
    "understanding",
    "applications",
    "development",
    "related",
    "quality",
}


def _job_evidence_query(job_description: str) -> str:
    """Build one bounded discovery query instead of scanning once per JD line."""

    observed = list(
        dict.fromkeys(
            token.casefold()
            for token in re.findall(r"[A-Za-z][A-Za-z0-9+#./-]{2,}", job_description)
        )
    )
    selected = [term for term in _JOB_EVIDENCE_PRIORITY_TERMS if term in observed]
    selected.extend(
        token
        for token in observed
        if token not in selected and token not in _JOB_EVIDENCE_STOPWORDS
    )
    return " ".join(selected[:8])


def _search_job_evidence(job_description: str, data_root: Path) -> list[RetrievalHit]:
    store = KnowledgeStore(data_root)
    query = _job_evidence_query(job_description)
    return store.search(query, limit=8) if query else []


def _record_resume_event(
    data_root: Path,
    event_type: ResumeFunnelEventType,
    *,
    run_id: str | None = None,
) -> None:
    """Keep anonymous funnel telemetry from blocking the resume result path."""

    try:
        record_resume_funnel_event(data_root, event_type, run_id=run_id)
    except OSError:
        pass


def _request_scoped_coverage(
    job_description: str,
    profile: CandidateProfile,
) -> tuple[dict[str, int], list[dict[str, str]]]:
    requirements = [line.strip(" -\t") for line in job_description.splitlines() if line.strip()]
    claims = [
        *profile.skills,
        *profile.experience_bullets,
        *profile.project_bullets,
    ]
    claim_terms = {
        token.casefold()
        for claim in claims
        for token in re.findall(r"[A-Za-z][A-Za-z0-9+#.-]{3,}", claim)
    }
    strong = 0
    partial = 0
    gaps: list[dict[str, str]] = []
    for index, requirement in enumerate(requirements, start=1):
        terms = {
            token.casefold()
            for token in re.findall(r"[A-Za-z][A-Za-z0-9+#.-]{3,}", requirement)
        }
        overlap = terms & claim_terms
        if len(overlap) >= 2:
            strong += 1
        elif overlap:
            partial += 1
        else:
            gaps.append(
                {
                    "requirement_id": f"REQ-{index:02d}",
                    "requirement_sha256": hashlib.sha256(
                        requirement.encode("utf-8")
                    ).hexdigest(),
                    "reason": "No supported lexical candidate in the uploaded resume.",
                }
            )
    coverage = {
        "total": len(requirements),
        "lexical_candidate_strong": strong,
        "lexical_candidate_partial": partial,
        "no_lexical_candidate": len(requirements) - strong - partial,
        "critical_with_candidate": 0,
        "critical_total": 0,
    }
    return coverage, gaps


def _resume_provenance_terms(value: str) -> set[str]:
    return {
        token.casefold()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9+#.-]{2,}", value)
    }


def _build_resume_provenance_receipt(
    *,
    run_id: str,
    job_description: str,
    profile: CandidateProfile,
    tailored: TailoredDocx,
) -> ResumeProvenanceReceipt:
    """Bind every rendered Summary/bullet to all approved sources or fail closed."""

    profile_entries = {
        f"PROFILE-{index:02d}": text
        for index, text in enumerate(
            profile.experience_bullets + profile.project_bullets,
            start=1,
        )
    }
    if not profile_entries:
        raise ResumeTemplateError("Resume provenance requires approved profile bullets")
    provenance_facts = (
        tailored.candidate_evidence_pack.atomic_facts
        if tailored.candidate_evidence_pack is not None
        else build_resume_atomic_facts(profile)
    )
    atomic_fact_by_id = {item.fact_id: item for item in provenance_facts}
    output_profile = extract_candidate_profile(tailored.content)
    output_bullets = output_profile.experience_bullets + output_profile.project_bullets
    strategy = tailored.role_strategy
    signal_text_by_id: dict[str, str] = {}
    signal_receipts: list[ResumeHiringSignalReceipt] = []
    rewrite_by_id = {}
    unsupported_requirement_sha256s: list[str] = []
    if strategy is not None:
        rewrite_by_id = {
            rewrite.profile_entry_id: rewrite for rewrite in strategy.bullet_rewrites
        }
        if set(rewrite_by_id) != set(profile_entries):
            raise ResumeTemplateError(
                "Resume provenance cannot map every exported rewrite to an approved fact"
            )
        for index, signal in enumerate(strategy.top_hiring_signals, start=1):
            signal_id = f"SIGNAL-{index:02d}"
            signal_text_by_id[signal_id] = signal
            signal_receipts.append(
                ResumeHiringSignalReceipt(
                    signal_id=signal_id,
                    signal_sha256=hashlib.sha256(signal.encode("utf-8")).hexdigest(),
                )
            )
        unsupported_requirement_sha256s = [
            hashlib.sha256(item.encode("utf-8")).hexdigest()
            for item in strategy.unsupported_requirements
        ]

    claims: list[ResumeClaimProvenance] = []

    def evidence_sources_for(fact_ids: list[str]) -> tuple[list[str], list[str]]:
        ordered_ids: list[str] = []
        hashes_by_id: dict[str, str] = {}
        for fact_id in fact_ids:
            fact = atomic_fact_by_id[fact_id]
            if fact.evidence_id not in hashes_by_id:
                ordered_ids.append(fact.evidence_id)
                hashes_by_id[fact.evidence_id] = fact.source_sha256
        return ordered_ids, [hashes_by_id[evidence_id] for evidence_id in ordered_ids]

    def signal_ids_for(value: str) -> list[str]:
        claim_terms = _resume_provenance_terms(value)
        return [
            signal_id
            for signal_id, signal in signal_text_by_id.items()
            if claim_terms & _resume_provenance_terms(signal)
        ]

    if profile.summary is not None:
        summary_rewrite = strategy.summary_rewrite if strategy is not None else None
        summary_text = summary_rewrite.text if summary_rewrite is not None else profile.summary
        if output_profile.summary != summary_text:
            raise ResumeTemplateError(
                "Resume provenance does not match the exported Summary"
            )
        if summary_rewrite is None:
            summary_evidence_ids = ["SUMMARY"]
            summary_evidence_hashes = [
                hashlib.sha256(profile.summary.encode("utf-8")).hexdigest()
            ]
            summary_fact_hashes: list[str] = []
            summary_status = ResumeClaimVerificationStatus.VERIFIED
            summary_basis: Literal[
                "EXACT_OPERATOR_APPROVED_PROFILE_ENTRY",
                "DETERMINISTIC_EVIDENCE_PRESERVING_REWRITE",
                "DETERMINISTIC_MULTI_SOURCE_SYNTHESIS",
            ] = "EXACT_OPERATOR_APPROVED_PROFILE_ENTRY"
        else:
            summary_evidence_ids, summary_evidence_hashes = evidence_sources_for(
                summary_rewrite.source_fact_ids
            )
            summary_fact_hashes = [
                atomic_fact_by_id[fact_id].fact_sha256
                for fact_id in summary_rewrite.source_fact_ids
            ]
            summary_fact_ids = summary_rewrite.source_fact_ids
            summary_status = ResumeClaimVerificationStatus.SUPPORTED
            summary_basis = "DETERMINISTIC_MULTI_SOURCE_SYNTHESIS"
        if summary_rewrite is None:
            summary_fact_ids = []
        claims.append(
            ResumeClaimProvenance(
                claim_id="CLAIM-01",
                render_location="SUMMARY",
                final_text=summary_text,
                final_text_sha256=hashlib.sha256(
                    summary_text.encode("utf-8")
                ).hexdigest(),
                profile_entry_id="SUMMARY",
                approved_source_sha256=hashlib.sha256(
                    profile.summary.encode("utf-8")
                ).hexdigest(),
                evidence_ids=summary_evidence_ids,
                approved_evidence_sha256s=summary_evidence_hashes,
                fact_ids=summary_fact_ids,
                source_fact_sha256s=summary_fact_hashes,
                hiring_signal_ids=signal_ids_for(summary_text),
                status=summary_status,
                verification_basis=summary_basis,
            )
        )

    expected_output_bullets: list[str] = []
    for profile_entry_id, source_text in profile_entries.items():
        verification_basis: Literal[
            "EXACT_OPERATOR_APPROVED_PROFILE_ENTRY",
            "DETERMINISTIC_EVIDENCE_PRESERVING_REWRITE",
            "DETERMINISTIC_MULTI_SOURCE_SYNTHESIS",
        ]
        if strategy is None:
            final_text = source_text
            evidence_ids = [profile_entry_id]
            approved_evidence_sha256s = [
                hashlib.sha256(source_text.encode("utf-8")).hexdigest()
            ]
            fact_ids: list[str] = []
            source_fact_sha256s: list[str] = []
            status = ResumeClaimVerificationStatus.VERIFIED
            verification_basis = "EXACT_OPERATOR_APPROVED_PROFILE_ENTRY"
        else:
            rewrite = rewrite_by_id[profile_entry_id]
            final_text = rewrite.text
            if final_text == source_text:
                evidence_ids = [profile_entry_id]
                approved_evidence_sha256s = [
                    hashlib.sha256(source_text.encode("utf-8")).hexdigest()
                ]
                fact_ids = []
                source_fact_sha256s = []
                status = ResumeClaimVerificationStatus.VERIFIED
                verification_basis = "EXACT_OPERATOR_APPROVED_PROFILE_ENTRY"
            else:
                fact_ids = rewrite.source_fact_ids
                evidence_ids, approved_evidence_sha256s = evidence_sources_for(
                    fact_ids
                )
                source_fact_sha256s = [
                    atomic_fact_by_id[fact_id].fact_sha256
                    for fact_id in fact_ids
                ]
                status = ResumeClaimVerificationStatus.SUPPORTED
                verification_basis = (
                    "DETERMINISTIC_MULTI_SOURCE_SYNTHESIS"
                    if rewrite.kind == "SYNTHESIS"
                    else "DETERMINISTIC_EVIDENCE_PRESERVING_REWRITE"
                )
        expected_output_bullets.append(final_text)
        claims.append(
            ResumeClaimProvenance(
                claim_id=f"CLAIM-{len(claims) + 1:02d}",
                render_location="BULLET",
                final_text=final_text,
                final_text_sha256=hashlib.sha256(final_text.encode("utf-8")).hexdigest(),
                profile_entry_id=profile_entry_id,
                approved_source_sha256=hashlib.sha256(
                    source_text.encode("utf-8")
                ).hexdigest(),
                evidence_ids=evidence_ids,
                approved_evidence_sha256s=approved_evidence_sha256s,
                fact_ids=fact_ids,
                source_fact_sha256s=source_fact_sha256s,
                hiring_signal_ids=signal_ids_for(f"{source_text} {final_text}"),
                status=status,
                verification_basis=verification_basis,
            )
        )
    if sorted(output_bullets) != sorted(expected_output_bullets):
        raise ResumeTemplateError(
            "Resume provenance does not match the exported DOCX bullets"
        )
    return ResumeProvenanceReceipt(
        run_id=run_id,
        resume_sha256=tailored.output_sha256,
        job_description_sha256=hashlib.sha256(
            job_description.encode("utf-8")
        ).hexdigest(),
        generation_mode=tailored.generation_mode,
        hiring_signals=signal_receipts,
        claims=claims,
        unsupported_requirement_sha256s=unsupported_requirement_sha256s,
    )


def _resume_provenance_summary(
    receipt: ResumeProvenanceReceipt,
) -> dict[str, object]:
    verified = sum(
        claim.status == ResumeClaimVerificationStatus.VERIFIED
        for claim in receipt.claims
    )
    supported = sum(
        claim.status == ResumeClaimVerificationStatus.SUPPORTED
        for claim in receipt.claims
    )
    return {
        "artifact": "12_resume_provenance.json",
        "claim_count": len(receipt.claims),
        "verified_claim_count": verified,
        "supported_claim_count": supported,
        "unverified_claim_count": 0,
        "contradicted_claim_count": 0,
        "all_exported_claims_supported": True,
    }


def _write_resume_expert_review_receipt(
    run_dir: Path,
    tailored: TailoredDocx,
) -> str | None:
    if not tailored.expert_review_attempted and tailored.expert_review is None:
        return None
    path = run_dir / "13_expert_review.json"
    skipped_status = {
        "EXPERT_PATCH_SOURCE_MISMATCH": "SKIPPED_DRAFT_MISMATCH",
        "EXPERT_PATCH_UNSUPPORTED_FACT": "SKIPPED_UNSUPPORTED_FACT",
        "EXPERT_PATCH_REVALIDATION_FAILED": "SKIPPED_REVALIDATION",
        "EXPERT_REVIEW_UNAVAILABLE": "SKIPPED_UNAVAILABLE",
        "EXPERT_REVIEW_TRANSPORT_FAILED": "SKIPPED_TRANSPORT",
        "EXPERT_REVIEW_INVALID_RESPONSE": "SKIPPED_INVALID_RESPONSE",
    }
    _write_private_json(
        path,
        {
            "schema_version": "1.0",
            "status": (
                "PATCHES_REVERIFIED"
                if tailored.expert_review is not None
                else skipped_status.get(
                    tailored.expert_review_skipped_code or "",
                    "SKIPPED_REVALIDATION",
                )
            ),
            "provider": tailored.expert_provider,
            "model": tailored.expert_model,
            "patch_count": tailored.expert_rewrites,
            "review": (
                tailored.expert_review.model_dump(mode="json")
                if tailored.expert_review is not None
                else None
            ),
            "failure_code": tailored.expert_review_skipped_code,
            "final_resume_sha256": tailored.output_sha256,
            "new_factual_claims_accepted": 0,
            "final_human_review_required": True,
        },
    )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_resume_evidence_trace(run_dir: Path, tailored: TailoredDocx) -> str | None:
    trace = tailored.evidence_retrieval_trace
    if trace is None:
        return None
    path = run_dir / "14_resume_evidence_trace.json"
    _write_private_json(
        path,
        {
            "schema_version": "1.0",
            "trace": trace.model_dump(mode="json"),
            "raw_discovery_bodies_retained": False,
            "raw_discovery_bodies_sent_to_model": False,
        },
    )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _save_request_scoped_resume_run(
    *,
    data_root: Path,
    job_description: str,
    profile: CandidateProfile,
    tailored: TailoredDocx,
    output_name: str,
    source_format: str,
    tailoring_instructions: str,
    approved_claims: list[dict[str, str]],
    create_preview: bool = True,
    output_locale: Literal["en-US", "zh-CN"] = "en-US",
    source_resume_locale: Literal["en-US", "zh-CN", "mixed", "unknown"] = "unknown",
    resume_project_id: str | None = None,
    template_receipt: ResumeTemplateReceipt | None = None,
) -> Path:
    """Persist only the generated result and body-free receipts for upload-first use."""

    _reject_symlink_ancestry(data_root)
    data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(data_root, 0o700)
    runs_root = data_root / "resume-runs"
    runs_root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(runs_root, 0o700)
    run_id = f"resume-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:10]}"
    run_dir = runs_root / run_id
    run_dir.mkdir(mode=0o700)
    os.chmod(run_dir, 0o700)
    internal_docx = run_dir / "08_resume.docx"
    docx_started = time.perf_counter()
    _write_private_bytes(internal_docx, tailored.content)
    docx_render_ms = int((time.perf_counter() - docx_started) * 1000)
    provenance = _build_resume_provenance_receipt(
        run_id=run_id,
        job_description=job_description,
        profile=profile,
        tailored=tailored,
    )
    provenance_path = run_dir / "12_resume_provenance.json"
    _write_private_json(provenance_path, provenance.model_dump(mode="json"))
    provenance_sha256 = hashlib.sha256(provenance_path.read_bytes()).hexdigest()
    provenance_summary = _resume_provenance_summary(provenance)
    expert_review_sha256 = _write_resume_expert_review_receipt(run_dir, tailored)
    evidence_trace_sha256 = _write_resume_evidence_trace(run_dir, tailored)
    resume_text = "\n".join(
        paragraph.text
        for paragraph in read_template_paragraphs(tailored.content)
        if paragraph.text
    )
    _atomic_private_write(run_dir / "04_resume.md", resume_text + "\n")
    coverage, gaps = _request_scoped_coverage(job_description, profile)
    pack = tailored.candidate_evidence_pack
    positioning = tailored.positioning_brief
    retrieval_trace = tailored.evidence_retrieval_trace
    if pack is not None:
        _write_private_json(
            run_dir / "03_candidate_evidence_pack.json",
            {
                "schema_version": "1.0",
                "pack_sha256": pack.pack_sha256,
                "fact_count": len(pack.atomic_facts),
                "profile_fact_count": sum(
                    fact.source_kind == "PROFILE_ENTRY" for fact in pack.atomic_facts
                ),
                "candidate_evidence_fact_count": sum(
                    fact.source_kind == "CANDIDATE_EVIDENCE"
                    for fact in pack.atomic_facts
                ),
                "fact_admission": (
                    pack.fact_admission.model_dump(mode="json")
                    if pack.fact_admission is not None
                    else None
                ),
                "sources": [
                    source.model_dump(mode="json") for source in pack.sources
                ],
                "fact_ids": [fact.fact_id for fact in pack.atomic_facts],
                "fact_bodies_retained": False,
            },
        )
    if positioning is not None:
        _write_private_json(
            run_dir / "02_positioning_brief.json",
            {
                "schema_version": "1.0",
                "job_description_sha256": positioning.job_description_sha256,
                "role_title_sha256": hashlib.sha256(
                    positioning.role_title.encode("utf-8")
                ).hexdigest(),
                "role_title_retained": False,
                "hiring_signal_sha256s": [
                    hashlib.sha256(signal.encode("utf-8")).hexdigest()
                    for signal in positioning.top_hiring_signals
                ],
                "technical_themes": positioning.technical_themes,
                "priority_fact_ids": positioning.priority_fact_ids,
                "first_resume_focus": positioning.first_resume_focus,
                "jd_source_spans_retained": False,
            },
        )
    _write_private_json(run_dir / "05_gaps.json", {"gaps": gaps, "learning_tasks": []})
    _write_private_json(
        run_dir / "07_verification.json",
        {
            "candidate_lineage_replayable": True,
            "resume_claim_source": (
                "operator_resume_plus_verified_candidate_evidence_pack"
                if pack is not None and pack.sources
                else "operator_selected_resume_only"
            ),
            "semantic_requirement_coverage_verified": False,
            "coverage": coverage,
            "claim_provenance": provenance_summary,
        },
    )
    preview_pdf = run_dir / "10_resume_preview.pdf"
    pdf_started = time.perf_counter()
    preview_created = (
        _create_resume_pdf_preview(internal_docx, preview_pdf)
        if create_preview
        else False
    )
    pdf_render_ms = (
        int((time.perf_counter() - pdf_started) * 1000) if create_preview else 0
    )
    pdf_status = (
        "PDF_READY"
        if preview_created
        else "NEEDS_ATTENTION"
        if create_preview
        else "PENDING"
    )
    user_metadata: dict[str, object] = {
        "schema_version": "1.0",
        "resume_project_id": resume_project_id,
        "source_resume_locale": source_resume_locale,
        "target_locale": output_locale,
        "output_locale": output_locale,
        "composition_status": "CONTENT_READY",
        "truth_status": "VERIFIED",
        "docx_status": "DOCX_READY",
        "docx_render_ms": docx_render_ms,
        "pdf_status": pdf_status,
        "pdf_render_ms": pdf_render_ms,
        "pdf_layout_attempts": 1 if create_preview else 0,
        "pdf_failure_reason": (
            "LOCAL_PDF_RENDER_UNAVAILABLE_OR_FAILED"
            if create_preview and not preview_created
            else None
        ),
        "failure_classification": (
            "DEGRADED"
            if create_preview and not preview_created
            else "RECOVERABLE"
            if not create_preview
            else None
        ),
        "fallback_used": bool(
            tailored.role_strategy_fallback_applied
            or tailored.rejected_rewrites
            or tailored.expert_review_skipped_code
        ),
        "retention": "request_scoped_sources_not_persisted",
        "template_source_format": source_format,
        "template_sha256": tailored.template_sha256,
        "output_filename": output_name,
        "output_sha256": tailored.output_sha256,
        "claims_preserved": tailored.claims_preserved,
        "source_paragraph_count": tailored.source_paragraph_count,
        "project_blocks_reordered": tailored.project_blocks_reordered,
        "skill_bullets_reordered": tailored.skill_bullets_reordered,
        "grounded_rewrites": tailored.grounded_rewrites,
        "synthesized_rewrites": tailored.synthesized_rewrites,
        "summary_rewritten": tailored.summary_rewritten,
        "rejected_rewrites": tailored.rejected_rewrites,
        "role_strategy_fallback_applied": tailored.role_strategy_fallback_applied,
        "role_strategy_fallback_code": tailored.role_strategy_fallback_code,
        "generation_mode": tailored.generation_mode,
        "provider": tailored.provider or "template",
        "model": tailored.model,
        "model_call_performed": tailored.role_strategy is not None,
        "model_call_profile": tailored.model_call_profile,
        "candidate_evidence_pack_sha256": pack.pack_sha256 if pack else None,
        "evidence_retrieval_trace_sha256": evidence_trace_sha256,
        "candidate_evidence_fact_count": len(pack.atomic_facts) if pack else 0,
        "evidence_retrieved_count": (
            retrieval_trace.retrieved_count if retrieval_trace is not None else 0
        ),
        "evidence_admitted_count": (
            retrieval_trace.admitted_count
            if retrieval_trace is not None
            else len(pack.atomic_facts) if pack else 0
        ),
        "evidence_sent_count": (
            retrieval_trace.sent_count
            if retrieval_trace is not None
            else len(pack.atomic_facts) if pack else 0
        ),
        "evidence_requirements": (
            [item.model_dump(mode="json") for item in retrieval_trace.requirements]
            if retrieval_trace is not None
            else []
        ),
        "evidence_source_summary": (
            [item.model_dump(mode="json") for item in retrieval_trace.sources]
            if retrieval_trace is not None
            else []
        ),
        "evidence_adoption": (
            [item.model_dump(mode="json") for item in retrieval_trace.adoption]
            if retrieval_trace is not None
            else []
        ),
        "candidate_evidence_projects": (
            list(dict.fromkeys(source.project for source in pack.sources))
            if pack
            else []
        ),
        "positioning_role_title": None,
        "unsupported_requirement_count": (
            len(tailored.role_strategy.unsupported_requirements)
            if tailored.role_strategy is not None
            else len(gaps)
        ),
        "internal_docx": str(internal_docx),
        "external_docx": "",
        "download_url": f"/downloads/{run_id}/resume.docx",
        "preview_url": f"/previews/{run_id}/resume.pdf" if preview_created else "",
        "preview_generated": preview_created,
        "network_used": tailored.provider
        in {
            ModelProviderId.SOLOSCALE_HOSTED.value,
            ModelProviderId.OPENAI_COMPATIBLE.value,
        }
        or tailored.expert_provider == ModelProviderId.OPENAI_COMPATIBLE.value,
        "mock_only_hosted_boundary": False,
        "tailoring_instructions_sha256": hashlib.sha256(
            tailoring_instructions.encode("utf-8")
        ).hexdigest(),
        "operator_approved_profile_claims": approved_claims,
        "resume_provenance_sha256": provenance_sha256,
        "claim_provenance": provenance_summary,
        "expert_review_performed": tailored.expert_review is not None,
        "expert_review_attempted": tailored.expert_review_attempted,
        "expert_review_skipped_code": tailored.expert_review_skipped_code,
        "expert_review_provider": tailored.expert_provider,
        "expert_review_model": tailored.expert_model,
        "expert_rewrites": tailored.expert_rewrites,
        "expert_review_sha256": expert_review_sha256,
        "template_receipt": (
            template_receipt.model_dump(mode="json")
            if template_receipt is not None
            else None
        ),
    }
    _write_private_json(run_dir / "09_user_ui.json", user_metadata)
    job_sha256 = hashlib.sha256(job_description.encode("utf-8")).hexdigest()
    candidate_sha256 = hashlib.sha256(
        profile.model_dump_json().encode("utf-8")
    ).hexdigest()
    _write_private_json(
        run_dir / "application_receipt.json",
        {
            "schema_version": "1.0",
            "status": "REQUEST_SCOPED_DRAFT_READY",
            "run_id": run_id,
            "resume_project_id": resume_project_id,
            "source_resume_locale": source_resume_locale,
            "target_locale": output_locale,
            "output_locale": output_locale,
            "source_inputs_retained": False,
            "source_input_lifetime": "request_only",
            "job_description_sha256": job_sha256,
            "candidate_profile_sha256": candidate_sha256,
            "resume_sha256": tailored.output_sha256,
            "resume_provenance_sha256": provenance_sha256,
            "all_exported_claims_supported": True,
            "expert_review_performed": tailored.expert_review is not None,
            "expert_review_attempted": tailored.expert_review_attempted,
            "expert_review_skipped_code": tailored.expert_review_skipped_code,
            "expert_review_provider": tailored.expert_provider,
            "expert_review_model": tailored.expert_model,
            "expert_review_sha256": expert_review_sha256,
            "generation_mode": tailored.generation_mode,
            "provider": tailored.provider or "template",
            "model": tailored.model,
            "model_call_profile": tailored.model_call_profile,
            "final_human_review_required": True,
            "job_application_submitted": False,
            "template_receipt_sha256": (
                hashlib.sha256(
                    template_receipt.model_dump_json().encode("utf-8")
                ).hexdigest()
                if template_receipt is not None
                else None
            ),
        },
    )
    artifacts = [
        "04_resume.md",
        "05_gaps.json",
        "07_verification.json",
        "08_resume.docx",
        "09_user_ui.json",
        "12_resume_provenance.json",
        "application_receipt.json",
    ]
    if positioning is not None:
        artifacts.append("02_positioning_brief.json")
    if pack is not None:
        artifacts.append("03_candidate_evidence_pack.json")
    if preview_created:
        artifacts.append("10_resume_preview.pdf")
    if expert_review_sha256 is not None:
        artifacts.append("13_expert_review.json")
    if evidence_trace_sha256 is not None:
        artifacts.append("14_resume_evidence_trace.json")
    _write_private_json(
        run_dir / "run.json",
        {
            "schema_version": "1.0",
            "run_id": run_id,
            "status": "DRAFT_REQUIRES_HUMAN_REVIEW",
            "resume_project_id": resume_project_id,
            "source_resume_locale": source_resume_locale,
            "target_locale": output_locale,
            "output_locale": output_locale,
            "composition_status": "CONTENT_READY",
            "truth_status": "VERIFIED",
            "docx_status": "DOCX_READY",
            "docx_render_ms": docx_render_ms,
            "pdf_status": pdf_status,
            "retention": "request_scoped_sources_not_persisted",
            "artifact_paths": artifacts,
            "job_description_sha256": job_sha256,
            "candidate_profile_sha256": candidate_sha256,
            "claim_provenance": provenance_summary,
            "expert_review_performed": tailored.expert_review is not None,
            "expert_review_attempted": tailored.expert_review_attempted,
            "expert_review_skipped_code": tailored.expert_review_skipped_code,
            "model_call_profile": tailored.model_call_profile,
            "candidate_evidence_pack_sha256": pack.pack_sha256 if pack else None,
            "candidate_evidence_fact_count": len(pack.atomic_facts) if pack else 0,
            "positioning_role_title": None,
            "template_receipt": (
                template_receipt.model_dump(mode="json")
                if template_receipt is not None
                else None
            ),
        },
    )
    return run_dir


def _run_user_resume(
    form: dict[str, str],
    files: dict[str, UploadedFile],
    data_root: Path,
    repo_root: Path,
    *,
    evidence_repository_root: Path | None = None,
    gateway: ModelGateway | None = None,
    expert_gateway: ModelGateway | None = None,
    template_receipt: ResumeTemplateReceipt | None = None,
    allow_persistent_storage: bool = False,
    create_preview: bool = True,
    progress: Callable[[str], None] | None = None,
    timing: Callable[[str, int], None] | None = None,
) -> UIActionResult:
    """Run the local upload → evidence → workspace → DOCX flow without a subprocess."""
    started = time.perf_counter()
    candidate_artifact_path: Path | None = None
    discovery_hits: list[RetrievalHit] = []
    upload = files.get("resume_template")
    if progress is not None:
        progress("PREPARING")
    generation_mode = form.get(
        "generation_mode", ModelProviderId.SOLOSCALE_HOSTED.value
    ).strip()
    legacy_approval = (
        generation_mode == "template"
        and form.get("approve_candidate_claims") == "yes"
    )
    if form.get("approve_resume_processing") != "yes" and not legacy_approval:
        return UIActionResult(
            "tailored-resume",
            "local resume generation",
            2,
            "",
            "请确认本次只处理你主动选择的简历、JD 和可选支持文件。",
            0,
        )
    tailoring_instructions = form.get("tailoring_instructions", "").strip()
    if len(tailoring_instructions) > 1200:
        return UIActionResult(
            "tailored-resume",
            "local resume generation",
            2,
            "",
            "针对性说明不能超过 1200 个字符。",
            0,
        )
    if generation_mode not in {
        ModelProviderId.SOLOSCALE_HOSTED.value,
        ModelProviderId.OLLAMA.value,
        ModelProviderId.OPENAI_COMPATIBLE.value,
        "template",
    }:
        return UIActionResult(
            "tailored-resume",
            "resume generation",
            2,
            "",
            "AI generation mode is invalid.",
            0,
        )
    output_language = form.get("resume_output_language", "en-US").strip()
    if output_language not in {"en-US", "zh-CN", "both"}:
        return UIActionResult(
            "tailored-resume",
            "resume generation",
            2,
            "",
            "Resume output language is invalid.",
            0,
        )
    output_locales: tuple[Literal["en-US", "zh-CN"], ...] = (
        ("en-US", "zh-CN")
        if output_language == "both"
        else (cast(Literal["en-US", "zh-CN"], output_language),)
    )
    # Safe offline mode preserves source claim wording by design. Locale-specific
    # editorial composition is an AI capability; this is not a source/target
    # language-matching rule.
    if generation_mode == "template" and output_language != "en-US":
        return UIActionResult(
            "tailored-resume",
            "resume generation",
            2,
            "",
            "Natural Chinese Resume output requires an AI provider; safe offline mode does not mechanically translate Resume claims.",
            0,
        )
    expert_review_mode = form.get("expert_review_mode", "local").strip()
    if generation_mode == "template":
        expert_review_mode = "local"
    if expert_review_mode not in {"local", "openai_sol"}:
        return UIActionResult(
            "tailored-resume",
            "resume expert review",
            2,
            "",
            "Expert review mode is invalid.",
            0,
        )
    if output_language == "both" and expert_review_mode == "openai_sol":
        return UIActionResult(
            "tailored-resume",
            "resume expert review",
            2,
            "",
            "Generate bilingual base resumes first; optional expert review is available one locale at a time.",
            0,
        )
    if expert_review_mode == "openai_sol":
        if form.get("approve_expert_review") != "yes":
            return UIActionResult(
                "tailored-resume",
                "resume expert review",
                2,
                "",
                "请明确确认本次 GPT-5.6 Sol 专家审阅会使用你的 OpenAI API 账户。",
                0,
            )
        if (
            expert_gateway is None
            or expert_gateway.descriptor.configuration_state
            is not GatewayConfigurationState.CONFIGURED
        ):
            return UIActionResult(
                "tailored-resume",
                "resume expert review",
                1,
                "",
                "GPT-5.6 Sol 专家审阅尚未配置；没有发送审阅请求。",
                0,
            )
    if upload is None or not upload.content:
        return UIActionResult(
            "tailored-resume", "local resume generation", 2, "", "请上传一份简历。", 0
        )

    try:
        _reject_symlink_ancestry(data_root)
        _record_resume_event(data_root, ResumeFunnelEventType.RESUME_UPLOAD_STARTED)
        selected_files = [
            SelectedResumeFile(
                role=ResumeUploadRole.RESUME,
                filename=upload.filename,
                content_type=upload.content_type,
                content=upload.content,
            )
        ]
        jd_upload = files.get("job_description_file")
        if jd_upload is not None and jd_upload.content:
            selected_files.append(
                SelectedResumeFile(
                    role=ResumeUploadRole.JOB_DESCRIPTION,
                    filename=jd_upload.filename,
                    content_type=jd_upload.content_type,
                    content=jd_upload.content,
                )
            )
        support_file = files.get("support_document")
        if support_file is not None and support_file.content:
            selected_files.append(
                SelectedResumeFile(
                    role=ResumeUploadRole.SUPPORT,
                    filename=support_file.filename,
                    content_type=support_file.content_type,
                    content=support_file.content,
                )
            )
        extracted = extract_selected_resume_files(selected_files)
        resume_upload = extracted[ResumeUploadRole.RESUME]
        source_resume_locale = detect_resume_language([resume_upload.text])
        _record_resume_event(data_root, ResumeFunnelEventType.RESUME_UPLOAD_COMPLETED)
        pasted_jd = form.get("job_description", "").strip()
        uploaded_jd = extracted.get(ResumeUploadRole.JOB_DESCRIPTION)
        if pasted_jd and uploaded_jd is not None:
            raise ResumeUploadError("Paste a JD or upload one JD file, not both")
        job_description = pasted_jd or (uploaded_jd.text if uploaded_jd is not None else "")
        if not job_description:
            raise ResumeUploadError("Paste or upload the complete Job Description")
        _record_resume_event(data_root, ResumeFunnelEventType.JD_SUPPLIED)
        template_bytes = (
            upload.content
            if resume_upload.source_format == "docx"
            else normalize_text_resume_to_docx(resume_upload.text)
        )
        template_preview_id = form.get("resume_template_preview_id", "").strip()
        if template_preview_id:
            if form.get("approve_layout_template") != "yes":
                raise ResumeUploadError(
                    "Review and approve the detected template structure before generation"
                )
            if (
                template_receipt is None
                or template_receipt.preview_id != template_preview_id
            ):
                raise ResumeUploadError(
                    "The selected template preview is unavailable; read the template again"
                )
            template_bytes = apply_resume_template_structure(
                template_bytes, template_receipt.detected_sections
            )
        profile_started = time.perf_counter()
        profile = extract_candidate_profile(template_bytes)
        if timing is not None:
            timing(
                "profile_extract_ms",
                int((time.perf_counter() - profile_started) * 1000),
            )
        candidate_evidence_pack = None
        if generation_mode != "template":
            if progress is not None:
                progress("RETRIEVING")
            retrieval_started = time.perf_counter()
            if evidence_repository_root is not None:
                ensure_local_project_evidence(
                    data_root,
                    repository_root=evidence_repository_root,
                    timing=timing,
                )
            elif timing is not None:
                timing("freshness_check_ms", 0)
                timing("local_git_refresh_ms", 0)
            if timing is not None:
                timing("knowledge_sync_ms", 0)
            lexical_started = time.perf_counter()
            discovery_hits = _search_job_evidence(job_description, data_root)
            if timing is not None:
                timing(
                    "lexical_search_ms",
                    int((time.perf_counter() - lexical_started) * 1000),
                )
            candidate_evidence_pack = build_candidate_evidence_pack(
                profile,
                data_root=data_root,
                repository_root=evidence_repository_root,
                job_description=job_description,
                repository_snapshot_is_current=evidence_repository_root is not None,
                timing=timing,
            )
            if timing is not None:
                timing(
                    "retrieval_ms",
                    int((time.perf_counter() - retrieval_started) * 1000),
                )
        if progress is not None:
            progress("GENERATING")
        support_upload: ExtractedResumeUpload | None = extracted.get(ResumeUploadRole.SUPPORT)
        relevance_text = "\n".join(
            part
            for part in (
                job_description,
                tailoring_instructions,
                support_upload.text if support_upload is not None else "",
            )
            if part
        )
        _record_resume_event(data_root, ResumeFunnelEventType.GENERATION_STARTED)
        generation_started = time.perf_counter()
        tailored_variants: list[
            tuple[Literal["en-US", "zh-CN"], TailoredDocx]
        ] = []
        if generation_mode == "template":
            selected_gateway = None
            tailored_variants.append(
                ("en-US", tailor_resume_docx(template_bytes, relevance_text))
            )
        else:
            selected_gateway = gateway or model_gateway_for(
                generation_mode,
                model=form.get("provider_model", "qwen3:8b"),
            )
            assert candidate_evidence_pack is not None
            gateway_template_metadata = (
                template_metadata_from_receipt(template_receipt)
                if template_receipt is not None
                else resume_upload.template_metadata
            )
            for output_locale in output_locales:
                candidate_artifact_path, candidate_recorder = (
                    _new_resume_candidate_recorder(data_root)
                )
                localized = tailor_resume_docx_with_gateway(
                    template_bytes,
                    job_description,
                    gateway=selected_gateway,
                    tailoring_instructions=tailoring_instructions,
                    template_metadata=gateway_template_metadata,
                    support_upload=support_upload,
                    candidate_recorder=candidate_recorder,
                    candidate_evidence_pack=candidate_evidence_pack,
                    output_locale=output_locale,
                )
                # The gateway receives only verified atomic facts. Body-free local
                # discovery lineage is attached after that boundary completes.
                tailored_variants.append(
                    (
                        output_locale,
                        replace(
                            localized,
                            evidence_retrieval_trace=(
                                build_resume_evidence_retrieval_trace(
                                    job_description=job_description,
                                    facts=candidate_evidence_pack.atomic_facts,
                                    knowledge_hits=discovery_hits,
                                    adoption=list(localized.evidence_adoption),
                                )
                            ),
                        ),
                    )
                )
        tailored = tailored_variants[0][1]
        if timing is not None:
            timing(
                "model_generation_ms",
                int((time.perf_counter() - generation_started) * 1000),
            )
        if progress is not None:
            progress("VERIFYING")
        verification_started = time.perf_counter()
        profile_claims = profile.experience_bullets + profile.project_bullets
        approved_claims = [
            {
                "id": f"PROFILE-{index:02d}",
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
            for index, text in enumerate(profile_claims, start=1)
        ]
        if timing is not None:
            timing(
                "verification_ms",
                int((time.perf_counter() - verification_started) * 1000),
            )
        if expert_review_mode == "openai_sol":
            if progress is not None:
                progress("EXPERT_REVIEW")
            expert_started = time.perf_counter()
            selected_expert_gateway = cast(ModelGateway, expert_gateway)
            _expert_candidate_artifact_path, expert_candidate_recorder = (
                _new_resume_candidate_recorder(data_root)
            )
            expert_review: ResumeExpertReviewResult | None = None
            try:
                expert_review = request_resume_expert_review(
                    tailored,
                    profile=profile,
                    gateway=selected_expert_gateway,
                )
                expert_candidate_recorder(
                    _expert_review_candidate_artifact(
                        expert_review,
                        status="PENDING_VALIDATION",
                    )
                )
                if progress is not None:
                    progress("REVERIFYING")
                reverification_started = time.perf_counter()
                tailored = apply_resume_expert_review(
                    tailored,
                    profile=profile,
                    job_description=job_description,
                    review=expert_review,
                    expert_provider=selected_expert_gateway.descriptor.provider.value,
                    expert_model=selected_expert_gateway.descriptor.model,
                )
                expert_candidate_recorder(
                    _expert_review_candidate_artifact(
                        expert_review,
                        status="VALIDATED",
                    )
                )
                if timing is not None:
                    timing(
                        "reverification_ms",
                        int((time.perf_counter() - reverification_started) * 1000),
                    )
            except (
                ModelGatewayNotConfigured,
                ModelGatewayTransportError,
                ModelGatewayInvalidResponse,
                ResumeTemplateError,
            ) as error:
                failure_code = _expert_review_failure_code(error)
                expert_candidate_recorder(
                    _expert_review_candidate_artifact(
                        expert_review,
                        status="REJECTED",
                        failure_code=failure_code,
                    )
                )
                tailored = replace(
                    tailored,
                    expert_review_attempted=True,
                    expert_review_skipped_code=failure_code,
                    expert_provider=selected_expert_gateway.descriptor.provider.value,
                    expert_model=selected_expert_gateway.descriptor.model,
                )
            if timing is not None:
                timing(
                    "expert_review_ms",
                    int((time.perf_counter() - expert_started) * 1000),
                )
            tailored_variants[0] = (tailored_variants[0][0], tailored)
        output_name = _user_resume_filename(
            profile,
            form.get("company_name", "").strip() or None,
            form.get("job_title", "").strip() or None,
            job_description,
        )
        if not allow_persistent_storage:
            if timing is not None and generation_mode == "template":
                timing("retrieval_ms", 0)
            if progress is not None:
                progress("EXPORTING")
            docx_started = time.perf_counter()
            resume_project_id = f"resume-project-{uuid4().hex[:12]}"
            run_dirs: list[Path] = []
            for output_locale, variant in tailored_variants:
                variant_name = _resume_variant_filename(
                    output_name,
                    output_locale,
                    include_locale=len(tailored_variants) > 1,
                )
                run_dir = _save_request_scoped_resume_run(
                    data_root=data_root,
                    job_description=job_description,
                    profile=profile,
                    tailored=variant,
                    output_name=variant_name,
                    source_format=resume_upload.source_format,
                    tailoring_instructions=tailoring_instructions,
                    approved_claims=approved_claims,
                    create_preview=create_preview,
                    output_locale=output_locale,
                    source_resume_locale=source_resume_locale,
                    resume_project_id=resume_project_id,
                    template_receipt=template_receipt,
                )
                run_dirs.append(run_dir)
                _record_resume_event(
                    data_root,
                    ResumeFunnelEventType.GENERATION_COMPLETED,
                    run_id=run_dir.name,
                )
            if timing is not None:
                timing("docx_ms", int((time.perf_counter() - docx_started) * 1000))
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            workspace_lines = [f"Resume workspace: {run_dirs[0]}"]
            workspace_lines.extend(
                f"Resume variant workspace: {value}" for value in run_dirs[1:]
            )
            return UIActionResult(
                "tailored-resume",
                "request-scoped resume generation",
                0,
                "\n".join(workspace_lines),
                "",
                elapsed_ms,
            )
        if len(tailored_variants) > 1:
            raise ResumeUploadError(
                "Bilingual output is available in the private Desktop draft flow"
            )
        library_root = (
            Path(
                form.get("resume_library_root", "").strip()
                or Path.home() / "Documents" / "Resume Applications"
            )
            .expanduser()
            .absolute()
        )
        evidence_hits = discovery_hits
        if generation_mode == "template":
            if progress is not None:
                progress("RETRIEVING")
            retrieval_started = time.perf_counter()
            evidence_hits = _search_job_evidence(job_description, data_root)
            if timing is not None:
                timing(
                    "retrieval_ms",
                    int((time.perf_counter() - retrieval_started) * 1000),
                )
        if progress is not None:
            progress("EXPORTING")
        docx_started = time.perf_counter()
        run = execute_resume_workspace(
            data_root=data_root,
            job_description=job_description,
            candidate_profile=profile,
            evidence_hits=evidence_hits,
            company_name=form.get("company_name", "").strip() or None,
            company_url=form.get("company_url", "").strip() or None,
            job_title=form.get("job_title", "").strip() or None,
            job_id=form.get("job_id", "").strip() or None,
            application_library_root=library_root,
            repository_root=repo_root,
            application_resume_bytes=tailored.content,
            application_resume_filename=output_name,
            application_resume_metadata={
                "output_locale": tailored.output_locale,
                "template_source_format": resume_upload.source_format,
                "source_parse_trace": (
                    resume_upload.source_parse_trace.model_dump_json()
                    if resume_upload.source_parse_trace is not None
                    else ""
                ),
                "template_sha256": tailored.template_sha256,
                "claims_preserved": tailored.claims_preserved,
                "source_paragraph_count": tailored.source_paragraph_count,
                "project_blocks_reordered": tailored.project_blocks_reordered,
                "skill_bullets_reordered": tailored.skill_bullets_reordered,
                "grounded_rewrites": tailored.grounded_rewrites,
                "synthesized_rewrites": tailored.synthesized_rewrites,
                "summary_rewritten": tailored.summary_rewritten,
                "rejected_rewrites": tailored.rejected_rewrites,
                "role_strategy_fallback_applied": (
                    tailored.role_strategy_fallback_applied
                ),
                "role_strategy_fallback_code": (
                    tailored.role_strategy_fallback_code or ""
                ),
                "generation_mode": tailored.generation_mode,
                "provider": tailored.provider or "template",
                "model": tailored.model or "none",
                "operator_approved_profile_claims_sha256": hashlib.sha256(
                    json.dumps(approved_claims, sort_keys=True).encode("utf-8")
                ).hexdigest(),
                "tailoring_instructions_sha256": hashlib.sha256(
                    tailoring_instructions.encode("utf-8")
                ).hexdigest(),
                "template_receipt_preview_id": (
                    template_receipt.preview_id if template_receipt is not None else ""
                ),
                "template_receipt_source_type": (
                    template_receipt.source_type.value
                    if template_receipt is not None
                    else ""
                ),
                "template_receipt_source_sha256": (
                    template_receipt.source_sha256
                    if template_receipt is not None
                    else ""
                ),
                "template_receipt_source_url": (
                    template_receipt.source_url or ""
                    if template_receipt is not None
                    else ""
                ),
                "template_receipt_sections_json": (
                    json.dumps(template_receipt.detected_sections, ensure_ascii=False)
                    if template_receipt is not None
                    else ""
                ),
            },
            mode=ResumeMode.LOCAL_ONLY,
        )
        run_dir = data_root / "resume-runs" / run.run_id
        application_value = run.route.get("application_library_path")
        if not isinstance(application_value, str) or not application_value:
            raise OSError("Resume application directory was not created")
        application_dir = Path(application_value)
        internal_docx = run_dir / "08_resume.docx"
        external_docx = application_dir / output_name
        if (
            not internal_docx.is_file()
            or not external_docx.is_file()
            or internal_docx.read_bytes() != tailored.content
            or external_docx.read_bytes() != tailored.content
        ):
            raise OSError("Published resume DOCX failed byte-integrity verification")
        provenance = _build_resume_provenance_receipt(
            run_id=run.run_id,
            job_description=job_description,
            profile=profile,
            tailored=tailored,
        )
        provenance_path = run_dir / "12_resume_provenance.json"
        _write_private_json(provenance_path, provenance.model_dump(mode="json"))
        provenance_sha256 = hashlib.sha256(provenance_path.read_bytes()).hexdigest()
        provenance_summary = _resume_provenance_summary(provenance)
        expert_review_sha256 = _write_resume_expert_review_receipt(run_dir, tailored)
        evidence_trace_sha256 = _write_resume_evidence_trace(run_dir, tailored)
        verification_path = run_dir / "07_verification.json"
        verification_payload = _load_json_file(verification_path) or {}
        verification_payload["claim_provenance"] = provenance_summary
        _write_private_json(verification_path, verification_payload)
        preview_pdf = run_dir / "10_resume_preview.pdf"
        if create_preview and progress is not None:
            progress("PREVIEWING")
        pdf_started = time.perf_counter()
        preview_created = (
            _create_resume_pdf_preview(internal_docx, preview_pdf)
            if create_preview
            else False
        )
        pdf_render_ms = (
            int((time.perf_counter() - pdf_started) * 1000) if create_preview else 0
        )
        pdf_status = (
            "PDF_READY"
            if preview_created
            else "NEEDS_ATTENTION"
            if create_preview
            else "PENDING"
        )
        docx_render_ms = int((time.perf_counter() - docx_started) * 1000)
        user_metadata: dict[str, object] = {
            "schema_version": "1.0",
            "source_resume_locale": source_resume_locale,
            "target_locale": tailored.output_locale,
            "output_locale": tailored.output_locale,
            "composition_status": "CONTENT_READY",
            "truth_status": "VERIFIED",
            "docx_status": "DOCX_READY",
            "docx_render_ms": docx_render_ms,
            "pdf_status": pdf_status,
            "pdf_render_ms": pdf_render_ms,
            "pdf_layout_attempts": 1 if create_preview else 0,
            "pdf_failure_reason": (
                "LOCAL_PDF_RENDER_UNAVAILABLE_OR_FAILED"
                if create_preview and not preview_created
                else None
            ),
            "failure_classification": (
                "DEGRADED"
                if create_preview and not preview_created
                else "RECOVERABLE"
                if not create_preview
                else None
            ),
            "fallback_used": bool(
                tailored.role_strategy_fallback_applied
                or tailored.rejected_rewrites
                or tailored.expert_review_skipped_code
            ),
            "template_source_format": resume_upload.source_format,
            "source_parse_trace": (
                resume_upload.source_parse_trace.model_dump(mode="json")
                if resume_upload.source_parse_trace is not None
                else None
            ),
            "template_sha256": tailored.template_sha256,
            "output_filename": output_name,
            "output_sha256": tailored.output_sha256,
            "claims_preserved": tailored.claims_preserved,
            "source_paragraph_count": tailored.source_paragraph_count,
            "project_blocks_reordered": tailored.project_blocks_reordered,
            "skill_bullets_reordered": tailored.skill_bullets_reordered,
            "grounded_rewrites": tailored.grounded_rewrites,
            "synthesized_rewrites": tailored.synthesized_rewrites,
            "summary_rewritten": tailored.summary_rewritten,
            "rejected_rewrites": tailored.rejected_rewrites,
            "role_strategy_fallback_applied": tailored.role_strategy_fallback_applied,
            "role_strategy_fallback_code": tailored.role_strategy_fallback_code,
            "generation_mode": tailored.generation_mode,
            "provider": tailored.provider or "template",
            "model": tailored.model,
            "model_call_performed": tailored.role_strategy is not None,
            "model_call_profile": tailored.model_call_profile,
            "candidate_evidence_pack_sha256": (
                candidate_evidence_pack.pack_sha256
                if candidate_evidence_pack is not None
                else None
            ),
            "candidate_evidence_fact_count": (
                len(candidate_evidence_pack.atomic_facts)
                if candidate_evidence_pack is not None
                else 0
            ),
            "evidence_retrieval_trace_sha256": evidence_trace_sha256,
            "evidence_retrieved_count": (
                tailored.evidence_retrieval_trace.retrieved_count
                if tailored.evidence_retrieval_trace is not None
                else 0
            ),
            "evidence_admitted_count": (
                tailored.evidence_retrieval_trace.admitted_count
                if tailored.evidence_retrieval_trace is not None
                else len(candidate_evidence_pack.atomic_facts)
                if candidate_evidence_pack is not None
                else 0
            ),
            "evidence_sent_count": (
                tailored.evidence_retrieval_trace.sent_count
                if tailored.evidence_retrieval_trace is not None
                else len(candidate_evidence_pack.atomic_facts)
                if candidate_evidence_pack is not None
                else 0
            ),
            "evidence_requirements": (
                [
                    item.model_dump(mode="json")
                    for item in tailored.evidence_retrieval_trace.requirements
                ]
                if tailored.evidence_retrieval_trace is not None
                else []
            ),
            "evidence_source_summary": (
                [
                    item.model_dump(mode="json")
                    for item in tailored.evidence_retrieval_trace.sources
                ]
                if tailored.evidence_retrieval_trace is not None
                else []
            ),
            "evidence_adoption": (
                [
                    item.model_dump(mode="json")
                    for item in tailored.evidence_retrieval_trace.adoption
                ]
                if tailored.evidence_retrieval_trace is not None
                else []
            ),
            "model_gap_quotes": (
                tailored.role_strategy.unsupported_requirements
                if tailored.role_strategy is not None
                else []
            ),
            "internal_docx": str(internal_docx),
            "external_docx": str(external_docx),
            "download_url": f"/downloads/{run.run_id}/resume.docx",
            "preview_url": f"/previews/{run.run_id}/resume.pdf" if preview_created else "",
            "preview_generated": preview_created,
            "network_used": (
                selected_gateway is not None
                and selected_gateway.descriptor.provider
                in {
                    ModelProviderId.SOLOSCALE_HOSTED,
                    ModelProviderId.OPENAI_COMPATIBLE,
                }
            )
            or tailored.expert_provider == ModelProviderId.OPENAI_COMPATIBLE.value,
            "mock_only_hosted_boundary": False,
            "tailoring_instructions_sha256": hashlib.sha256(
                tailoring_instructions.encode("utf-8")
            ).hexdigest(),
            "operator_approved_profile_claims": approved_claims,
            "resume_provenance_sha256": provenance_sha256,
            "claim_provenance": provenance_summary,
            "expert_review_performed": tailored.expert_review is not None,
            "expert_review_attempted": tailored.expert_review_attempted,
            "expert_review_skipped_code": tailored.expert_review_skipped_code,
            "expert_review_provider": tailored.expert_provider,
            "expert_review_model": tailored.expert_model,
            "expert_rewrites": tailored.expert_rewrites,
            "expert_review_sha256": expert_review_sha256,
            "template_receipt": (
                template_receipt.model_dump(mode="json")
                if template_receipt is not None
                else None
            ),
        }
        _write_private_json(run_dir / "09_user_ui.json", user_metadata)
        if tailored.role_strategy is not None:
            _write_private_json(
                run_dir / "11_role_strategy.json",
                tailored.role_strategy.model_dump(mode="json"),
            )
        application_receipt = {
            "schema_version": "1.0",
            "status": "PRIVATE_APPLICATION_DRAFT_SAVED",
            "run_id": run.run_id,
            "operator_approved_profile_claims": approved_claims,
            "tailoring_instructions_sha256": hashlib.sha256(
                tailoring_instructions.encode("utf-8")
            ).hexdigest(),
            "resume_sha256": tailored.output_sha256,
            "resume_provenance_sha256": provenance_sha256,
            "all_exported_claims_supported": True,
            "expert_review_performed": tailored.expert_review is not None,
            "expert_review_attempted": tailored.expert_review_attempted,
            "expert_review_skipped_code": tailored.expert_review_skipped_code,
            "expert_review_provider": tailored.expert_provider,
            "expert_review_model": tailored.expert_model,
            "expert_review_sha256": expert_review_sha256,
            "generation_mode": tailored.generation_mode,
            "provider": tailored.provider or "template",
            "model": tailored.model,
            "model_call_performed": tailored.role_strategy is not None,
            "model_call_profile": tailored.model_call_profile,
            "model_gap_quotes": (
                tailored.role_strategy.unsupported_requirements
                if tailored.role_strategy is not None
                else []
            ),
            "application_directory": str(application_dir),
            "final_human_review_required": True,
            "job_application_submitted": False,
        }
        _write_private_json(run_dir / "application_receipt.json", application_receipt)

        run_path = run_dir / "run.json"
        run_payload = _load_json_file(run_path) or {}
        route = run_payload.get("route")
        if not isinstance(route, dict):
            route = {}
            run_payload["route"] = route
        route["user_ui"] = True
        route["docx_saved"] = True
        route["docx_sha256"] = tailored.output_sha256
        route["resume_provenance_sha256"] = provenance_sha256
        route["preview_generated"] = preview_created
        route["composition_status"] = "CONTENT_READY"
        route["truth_status"] = "VERIFIED"
        route["docx_status"] = "DOCX_READY"
        route["docx_render_ms"] = docx_render_ms
        route["pdf_status"] = pdf_status
        route["model_call_profile"] = tailored.model_call_profile
        artifact_paths = run_payload.get("artifact_paths")
        if not isinstance(artifact_paths, list):
            artifact_paths = []
            run_payload["artifact_paths"] = artifact_paths
        output_artifacts = [
            "09_user_ui.json",
            "12_resume_provenance.json",
            "application_receipt.json",
        ]
        if tailored.role_strategy is not None:
            output_artifacts.append("11_role_strategy.json")
        if expert_review_sha256 is not None:
            output_artifacts.append("13_expert_review.json")
        if evidence_trace_sha256 is not None:
            output_artifacts.append("14_resume_evidence_trace.json")
        if preview_created:
            output_artifacts.append("10_resume_preview.pdf")
        for name in output_artifacts:
            if name not in artifact_paths:
                artifact_paths.append(name)
        _write_private_json(run_path, run_payload)
        _record_resume_event(
            data_root,
            ResumeFunnelEventType.GENERATION_COMPLETED,
            run_id=run.run_id,
        )
        if timing is not None:
            timing("docx_ms", int((time.perf_counter() - docx_started) * 1000))
    except ModelGatewayNotConfigured:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return UIActionResult(
            "tailored-resume",
            "AI resume generation",
            1,
            "",
            "SoloScale 托管 AI 尚未连接到这个本地版本；没有生成通用简历，也没有保存新的申请包。请配置高级 AI 服务，或明确选择安全离线草稿。",
            elapsed_ms,
        )
    except ModelGatewayTransportError:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return UIActionResult(
            "tailored-resume",
            "AI resume generation",
            1,
            "",
            "所选 AI 服务当前无法连接；本次没有回退到通用简历，也没有保存新的申请包。",
            elapsed_ms,
        )
    except ModelGatewayInvalidResponse:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return UIActionResult(
            "tailored-resume",
            "AI resume generation",
            1,
            "",
            "所选 AI 服务没有返回可验证的简历结构；本次没有生成或保存简历。",
            elapsed_ms,
        )
    except ResumeTemplateError as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        diagnostics: dict[str, object] | None = (
            {"resume_validation": exc.validation_diagnostics.as_dict()}
            if exc.validation_diagnostics is not None
            else None
        )
        message = str(exc)
        if candidate_artifact_path is not None and candidate_artifact_path.is_file():
            diagnostics = dict(diagnostics or {})
            diagnostics["structured_candidate_path"] = str(candidate_artifact_path)
            message = (
                f"{message} Rejected candidate saved for offline inspection: "
                f"{candidate_artifact_path}"
            )
        return UIActionResult(
            "tailored-resume",
            "local resume generation",
            1,
            "",
            message,
            elapsed_ms,
            diagnostics=diagnostics,
        )
    except (
        KnowledgeStoreError,
        EvidenceHubError,
        OSError,
        ResumeUploadError,
        ValueError,
    ) as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return UIActionResult(
            "tailored-resume", "local resume generation", 1, "", str(exc), elapsed_ms
        )

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return UIActionResult(
        "tailored-resume",
        "local resume generation",
        0,
        f"Resume workspace: {run_dir}",
        "",
        elapsed_ms,
    )


def _resume_result_run_dir(data_root: Path, result: UIActionResult) -> Path | None:
    run_dirs = _resume_result_run_dirs(data_root, result)
    return run_dirs[0] if run_dirs else None


def _resume_result_run_dirs(data_root: Path, result: UIActionResult) -> list[Path]:
    valid: list[Path] = []
    runs_root = data_root.expanduser().absolute() / "resume-runs"
    for raw_run_dir in _workspace_paths(result.stdout):
        if _RUN_ID_RE.fullmatch(raw_run_dir.name) is None:
            continue
        expected = runs_root / raw_run_dir.name
        try:
            _reject_symlink_ancestry(expected)
            if (
                raw_run_dir.expanduser().resolve() != expected.resolve()
                or expected.parent != runs_root
                or not expected.is_dir()
            ):
                continue
        except OSError:
            continue
        valid.append(expected)
    return valid


def _finalize_resume_preview(data_root: Path, result: UIActionResult) -> bool:
    """Render and register a PDF after the downloadable DOCX is already ready."""

    run_dirs = _resume_result_run_dirs(data_root, result)
    if not run_dirs:
        return False
    all_created = True
    for run_dir in run_dirs:
        source = _resume_run_artifact(data_root, run_dir.name, "08_resume.docx")
        target = run_dir / "10_resume_preview.pdf"
        pdf_started = time.perf_counter()
        created = bool(
            source is not None
            and not target.is_symlink()
            and _create_resume_pdf_preview(source, target)
        )
        pdf_render_ms = int((time.perf_counter() - pdf_started) * 1000)

        metadata_path = run_dir / "09_user_ui.json"
        metadata = _load_json_file(metadata_path)
        if metadata is not None:
            metadata["preview_generated"] = created
            metadata["preview_url"] = (
                f"/previews/{run_dir.name}/resume.pdf" if created else ""
            )
            metadata["pdf_status"] = "PDF_READY" if created else "NEEDS_ATTENTION"
            metadata["pdf_render_ms"] = pdf_render_ms
            previous_attempts = metadata.get("pdf_layout_attempts", 0)
            metadata["pdf_layout_attempts"] = (
                previous_attempts + 1 if isinstance(previous_attempts, int) else 1
            )
            metadata["pdf_failure_reason"] = (
                None if created else "LOCAL_PDF_RENDER_UNAVAILABLE_OR_FAILED"
            )
            metadata["failure_classification"] = None if created else "DEGRADED"
            _write_private_json(metadata_path, metadata)

        run_path = run_dir / "run.json"
        run_payload = _load_json_file(run_path)
        if run_payload is not None:
            route = run_payload.get("route")
            if not isinstance(route, dict):
                route = {}
                run_payload["route"] = route
            route["preview_generated"] = created
            route["pdf_status"] = "PDF_READY" if created else "NEEDS_ATTENTION"
            route["pdf_render_ms"] = pdf_render_ms
            route["failure_classification"] = None if created else "DEGRADED"
            artifacts = run_payload.get("artifact_paths")
            if not isinstance(artifacts, list):
                artifacts = []
                run_payload["artifact_paths"] = artifacts
            if created and "10_resume_preview.pdf" not in artifacts:
                artifacts.append("10_resume_preview.pdf")
            _write_private_json(run_path, run_payload)
        all_created = all_created and created
    return all_created


def _persist_resume_job_timings(
    data_root: Path,
    result: UIActionResult,
    snapshot: ResumeJobSnapshot,
) -> None:
    run_dir = _resume_result_run_dir(data_root, result)
    if run_dir is None:
        return
    run_path = run_dir / "run.json"
    run_payload = _load_json_file(run_path)
    if run_payload is None:
        return
    timing_ms = dict(snapshot.stage_durations_ms)
    timing_ms["total_ms"] = snapshot.total_elapsed_ms
    run_payload["resume_job"] = {
        "job_id": snapshot.job_id,
        "phase": snapshot.phase,
        "timing_ms": timing_ms,
        "total_elapsed_ms": snapshot.total_elapsed_ms,
        "preview_state": snapshot.preview_state,
        "failed_phase": snapshot.failed_phase,
    }
    _write_private_json(run_path, run_payload)


def _run_learning_workspace(
    form: dict[str, str],
    data_root: Path,
    repo_root: Path,
) -> UIActionResult:
    start = time.perf_counter()
    requirement = form.get("target_requirement", "").strip()
    try:
        preflight = preflight_work_sources(data_root, workspace_root=repo_root)
        local_git = next(
            (source for source in preflight.sources if source.kind == "local_git"),
            None,
        )
        if local_git is not None and local_git.action_required:
            refresh_work_source(data_root, "local_git", workspace_root=repo_root)
        run = run_learning_traceability(
            data_root=data_root,
            repository_root=repo_root,
            target_requirement=requirement,
        )
    except LearningFixtureAnchorError:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return UIActionResult(
            "learning-traceability",
            "local deterministic traceability build",
            1,
            "",
            "这个项目没有找到此练习需要的代码锚点。你可以选择其他项目或创建新的学习案例。",
            elapsed_ms,
        )
    except (LearningTraceabilityError, EvidenceHubError, OSError, ValueError):
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return UIActionResult(
            "learning-traceability",
            "local deterministic traceability build",
            1,
            "",
            "当前学习案例无法建立。请检查已选择项目后重试。",
            elapsed_ms,
        )
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    return UIActionResult(
        "learning-traceability",
        "local deterministic traceability build",
        0,
        f"Learning workspace: {run.private_run_path}",
        "",
        elapsed_ms,
    )


def _save_learning_response(
    form: dict[str, str],
    data_root: Path,
) -> UIActionResult:
    start = time.perf_counter()
    try:
        receipt, receipt_path = save_learning_response(
            data_root=data_root,
            run_id=form.get("run_id", ""),
            stage=form.get("stage", ""),
            response=form.get("response", ""),
        )
    except ValueError as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return UIActionResult(
            "learning-response",
            "private local response save",
            2,
            "",
            str(exc),
            elapsed_ms,
        )
    except (LearningTraceabilityError, OSError) as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return UIActionResult(
            "learning-response",
            "private local response save",
            1,
            "",
            str(exc),
            elapsed_ms,
        )
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    return UIActionResult(
        "learning-response",
        "private local response save",
        0,
        (
            f"Learning response saved: {receipt_path}\n"
            f"Status: {receipt.status}\n"
            "Mastery advanced: False"
        ),
        "",
        elapsed_ms,
    )


def _learning_response_location(stage: str) -> str:
    stage_slug = {
        "Explain": "explain",
        "Trace": "trace",
    }.get(stage)
    if stage_slug is None:
        raise ValueError("learning response stage is invalid")
    return f"/learning?response_saved={stage_slug}#exercise-{stage_slug}"


def _escape(value: str) -> str:
    return html.escape(value or "", quote=True)


def _parse_form(raw: bytes) -> dict[str, str]:
    return {
        key: values[0] if values else ""
        for key, values in urllib.parse.parse_qs(raw.decode("utf-8")).items()
    }


def _parse_submission(
    raw: bytes,
    content_type: str,
    *,
    max_bytes: int = MAX_UPLOAD_BYTES,
) -> FormSubmission:
    if len(raw) > max_bytes:
        raise ValueError("上传内容超过限制。")
    if content_type.startswith("application/x-www-form-urlencoded"):
        return FormSubmission(fields=_parse_form(raw), files={})
    if not content_type.startswith("multipart/form-data"):
        raise ValueError("表单必须使用 multipart/form-data。")
    if "\r" in content_type or "\n" in content_type:
        raise ValueError("Invalid multipart content type")
    message = BytesParser(policy=policy.default).parsebytes(
        (f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n").encode("ascii") + raw
    )
    if not message.is_multipart():
        raise ValueError("无法解析上传表单。")
    fields: dict[str, str] = {}
    files: dict[str, UploadedFile] = {}
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() != "form-data":
            continue
        field_name = part.get_param("name", header="content-disposition")
        if not isinstance(field_name, str) or not field_name:
            continue
        payload = part.get_payload(decode=True)
        content = payload if isinstance(payload, bytes) else b""
        filename = part.get_filename()
        if filename is not None:
            files.setdefault(
                field_name,
                UploadedFile(
                    filename=filename,
                    content_type=part.get_content_type(),
                    content=content,
                ),
            )
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            value = content.decode(charset)
        except (LookupError, UnicodeDecodeError) as exc:
            raise ValueError("表单文字不是有效的 UTF-8。") from exc
        fields.setdefault(field_name, value)
    return FormSubmission(fields=fields, files=files)


def _prepare_resume_template_preview(
    submission: FormSubmission,
) -> ResumeTemplateReceipt:
    upload = submission.files.get("layout_template_file")
    pasted_html = submission.fields.get("layout_template_html", "").strip()
    source_url = submission.fields.get("layout_template_url", "").strip()
    selected = sum(
        (
            bool(upload is not None and upload.content),
            bool(pasted_html),
            bool(source_url),
        )
    )
    if selected != 1:
        raise ResumeUploadError("Choose exactly one template source: file, URL, or HTML")
    if upload is not None and upload.content:
        receipt = inspect_template_file(upload.filename, upload.content)
    elif pasted_html:
        receipt = inspect_template_html(pasted_html)
    else:
        receipt = inspect_template_url(source_url)
    return receipt


def _build_control_tower_path(data_root: Path) -> Path:
    return (data_root / "control-tower" / "index.html").resolve()


def _read_control_tower(data_root: Path) -> tuple[bool, str]:
    target = _build_control_tower_path(data_root)
    if not target.is_file():
        return False, ""
    return True, target.read_text(encoding="utf-8")


def _run_action(form: dict[str, str], data_root: Path, repo_root: Path) -> UIActionResult | None:
    action = form.get("action")
    if action == "knowledge-status":
        return _run_command(["knowledge-status", "--data-root", str(data_root)], repo_root)
    if action == "control-tower-build":
        return _run_command(["control-tower-build", "--data-root", str(data_root)], repo_root)
    if action == "knowledge-search":
        query = form.get("query", "").strip()
        if not query:
            return UIActionResult(
                name=action,
                command="knowledge-search",
                return_code=2,
                stdout="",
                stderr="Query 不能为空。",
                elapsed_ms=0,
            )
        source_kind = form.get("source_kind", "").strip()
        command = ["knowledge-search", query, "--data-root", str(data_root)]
        if source_kind:
            command += ["--source-kind", source_kind]
        return _run_command(command, repo_root)
    if action == "knowledge-sync":
        include_codex = form.get("include_codex") == "on"
        codex_home = form.get("codex_home", "").strip()
        chatgpt_exports = [
            Path(value).expanduser()
            for value in _split_path_list(form.get("chatgpt_exports", ""))
        ]
        buildlog_roots = [
            Path(value).expanduser()
            for value in _split_path_list(form.get("buildlog_roots", ""))
        ]
        started = time.perf_counter()
        try:
            refreshed = refresh_selected_knowledge_sources(
                data_root,
                include_codex=include_codex,
                codex_home=Path(codex_home).expanduser() if codex_home else None,
                chatgpt_exports=chatgpt_exports,
                buildlog_roots=buildlog_roots,
            )
        except (OSError, ValueError, WorkContextError) as exc:
            return UIActionResult(
                name="knowledge-sync",
                command="in-process knowledge refresh",
                return_code=1,
                stdout="",
                stderr=str(exc),
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
        return UIActionResult(
            name="knowledge-sync",
            command="in-process knowledge refresh",
            return_code=0 if refreshed.failed == 0 or refreshed.imported + refreshed.updated + refreshed.skipped else 1,
            stdout=(
                f"Discovered {refreshed.discovered}; imported {refreshed.imported}; "
                f"updated {refreshed.updated}; unchanged {refreshed.skipped}; "
                f"failed {refreshed.failed}."
            ),
            stderr="",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
    if action == "evidence-agent":
        question = form.get("question", "").strip()
        if not question:
            return UIActionResult(
                name=action,
                command="evidence-agent",
                return_code=2,
                stdout="",
                stderr="Question 不能为空。",
                elapsed_ms=0,
            )
        model = form.get("model", "qwen3:8b").strip() or "qwen3:8b"
        ollama_url = (
            form.get("ollama_url", "http://127.0.0.1:11434").strip() or "http://127.0.0.1:11434"
        )
        source_kind = form.get("agent_source_kind", "").strip()

        command = [
            "evidence-agent",
            question,
            "--data-root",
            str(data_root),
            "--model",
            model,
            "--ollama-url",
            ollama_url,
        ]
        if source_kind:
            command += ["--source-kind", source_kind]
        return _run_command(command, repo_root)
    if action == "jd-resume-draft":
        return _run_jd_resume_draft(form, data_root, repo_root)
    if action == "resume-workspace":
        return _run_resume_workspace(form, data_root, repo_root)

    return None


def _human_duration(locale: UILocale, elapsed_ms: int) -> str:
    """Render a primary duration without leaking raw adapter latency."""

    seconds = max(0, int(elapsed_ms)) / 1000
    if seconds < 1:
        return ui_text(locale, f"{max(0, int(elapsed_ms))} 毫秒", f"{max(0, int(elapsed_ms))} ms")
    return ui_text(locale, f"{seconds:.1f} 秒", f"{seconds:.1f}s")


def _human_ai_service_label(
    locale: UILocale, provider: str, model: str | None = None
) -> str:
    """Map internal provider vocabulary to human product truth."""

    provider_key = (provider or "").strip().lower()
    model_label = (model or "").strip()
    if provider_key in {"openai_compatible", "openai", "openai_sol"}:
        base = ui_text(locale, "OpenAI 兼容服务", "OpenAI-compatible service")
    elif provider_key == "ollama":
        base = ui_text(locale, "本地 Ollama", "Local Ollama")
    elif provider_key == "soloscale_hosted":
        base = ui_text(locale, "SoloScale 托管 AI", "SoloScale Hosted AI")
    elif provider_key == "template":
        return ui_text(locale, "安全离线模板", "Safe offline template")
    else:
        base = ui_text(locale, "已配置服务", "Configured service")
    return f"{base} · {model_label}" if model_label else base


def _result_card(
    result: UIActionResult | None, locale: UILocale = DEFAULT_UI_LOCALE
) -> str:
    if result is None:
        return ""
    if result.return_code == 0:
        status = ui_text(locale, "已完成", "Completed")
        banner = "success"
    else:
        status = ui_text(locale, "需要处理", "Needs attention")
        banner = "error"
    body = result.stdout if result.stdout else result.stderr
    if not body:
        body = ui_text(locale, "没有返回额外信息。", "No additional details were returned.")
    workspace = (
        _resume_workspace_result(result.stdout)
        if result.name == "resume-workspace" and result.return_code == 0
        else ""
    )
    return f"""
<section class="tool-result {banner}" role="status">
  <span class="status-badge">{_escape(status)}</span>
  <h3>{_escape(ui_text(locale, '工具运行结果', 'Tool result'))}</h3>
  <p>{_escape(body)}</p>
  <details class="technical-details">
    <summary>{_escape(ui_text(locale, '查看技术详情', 'View technical details'))}</summary>
    <p>{_escape(ui_text(locale, '耗时', 'Duration'))}: {_escape(_human_duration(locale, result.elapsed_ms))}</p>
    <p>{_escape(ui_text(locale, '命令', 'Command'))}: <code>{_escape(result.command)}</code></p>
  </details>
  {workspace}
</section>
"""


def _resume_workspace_result(raw_stdout: str) -> str:
    prefix = "Resume workspace: "
    path_text = next(
        (line[len(prefix) :] for line in raw_stdout.splitlines() if line.startswith(prefix)), ""
    )
    run_dir = Path(path_text) if path_text else None
    if run_dir is None:
        return ""
    resume = ""
    try:
        resume = (run_dir / "04_resume.md").read_text(encoding="utf-8")
    except OSError:
        return ""
    verification = _load_json_file(run_dir / "07_verification.json") or {}
    gaps = _load_json_file(run_dir / "05_gaps.json") or {}
    run = _load_json_file(run_dir / "run.json") or {}
    route = run.get("route", {})
    if not isinstance(route, dict):
        route = {}
    application_path = route.get("application_library_path", "")
    coverage = verification.get("coverage", {})
    if not isinstance(coverage, dict):
        coverage = {}
    gap_items = gaps.get("gaps", [])
    if not isinstance(gap_items, list):
        gap_items = []
    gap_text = (
        "\n".join(f"- {item.get('skill', '')}" for item in gap_items if isinstance(item, dict))
        or "- None"
    )
    summary = (
        f"Requirements: {coverage.get('total', 0)} · strong lexical candidates: "
        f"{coverage.get('lexical_candidate_strong', 0)} · partial lexical candidates: "
        f"{coverage.get('lexical_candidate_partial', 0)} · no lexical candidate: "
        f"{coverage.get('no_lexical_candidate', 0)} · critical with candidate: "
        f"{coverage.get('critical_with_candidate', 0)}/{coverage.get('critical_total', 0)}"
    )
    return f"""<section class=\"graph-card\"><h3>One-page resume preview</h3>
<pre class=\"success\">{_escape(resume)}</pre><h3>Coverage</h3><p>{_escape(summary)}</p>
<h3>Gaps</h3><pre>{_escape(gap_text)}</pre>
<p class=\"muted\">Private artifacts: <code>{_escape(str(run_dir))}</code></p>
<p class=\"muted\">Application library: <code>{_escape(str(application_path))}</code></p>
{_resume_graph(raw_stdout)}</section>"""


def _resume_graph(raw_stdout: str) -> str:
    """Small native renderer for the frozen graph contract; no frontend dependency."""
    prefix = "Resume workspace: "
    path_text = next(
        (line[len(prefix) :] for line in raw_stdout.splitlines() if line.startswith(prefix)), ""
    )
    graph = _load_json_file(Path(path_text) / "06_graph.json") if path_text else None
    if graph is None:
        return ""
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return ""
    # JSON is embedded as a JavaScript expression. Escaping a closing script tag avoids
    # breaking the page while preserving JSON.parse-compatible node details.
    node_json = json.dumps(nodes, ensure_ascii=True).replace("</", "<\\/")
    edge_json = json.dumps(edges, ensure_ascii=True).replace("</", "<\\/")
    return f"""<section class="graph-card"><h3>Skill–Evidence Graph</h3>
<p class="muted">点击节点查看详情；双击展开/收起相连节点。</p>
<div style="overflow:auto"><svg id="resume-graph" role="img"></svg></div>
<pre id="graph-detail" class="success">请选择节点。</pre>
<script>
(function(){{
  const nodes={node_json};
  const edges={edge_json};
  const svg=document.getElementById('resume-graph');
  const detail=document.getElementById('graph-detail');
  const hidden=new Set();
  const columns=6;
  const height=Math.max(360,110*Math.ceil(nodes.length/columns)+50);
  svg.setAttribute('viewBox',`0 0 900 ${{height}}`);
  svg.setAttribute('height',height);
  const byId=Object.fromEntries(nodes.map((n,i)=>[
    n.id,
    {{...n,x:80+(i%columns)*155,y:55+Math.floor(i/columns)*105}},
  ]));
  function draw(){{
    svg.innerHTML='';
    edges.forEach(e=>{{
      const a=byId[e.source];
      const b=byId[e.target];
      if(!a||!b||hidden.has(a.id)||hidden.has(b.id))return;
      const line=document.createElementNS('http://www.w3.org/2000/svg','line');
      line.setAttribute('x1',a.x);
      line.setAttribute('y1',a.y);
      line.setAttribute('x2',b.x);
      line.setAttribute('y2',b.y);
      line.setAttribute('stroke','#64748b');
      svg.append(line);
    }});
    Object.values(byId).forEach(n=>{{
      if(hidden.has(n.id))return;
      const group=document.createElementNS('http://www.w3.org/2000/svg','g');
      const circle=document.createElementNS('http://www.w3.org/2000/svg','circle');
      const label=document.createElementNS('http://www.w3.org/2000/svg','text');
      circle.setAttribute('cx',n.x);
      circle.setAttribute('cy',n.y);
      circle.setAttribute('r','31');
      circle.setAttribute('fill','#1d4ed8');
      label.setAttribute('x',n.x);
      label.setAttribute('y',n.y+5);
      label.setAttribute('text-anchor','middle');
      label.setAttribute('fill','white');
      label.setAttribute('font-size','10');
      label.textContent=n.kind;
      group.append(circle,label);
      group.style.cursor='pointer';
      group.onclick=()=>detail.textContent=JSON.stringify(n,null,2);
      group.ondblclick=()=>{{
        edges.filter(e=>e.source===n.id).forEach(e=>{{
          hidden.has(e.target)?hidden.delete(e.target):hidden.add(e.target);
        }});
        draw();
      }};
      svg.append(group);
    }});
  }}
  draw();
}})();
</script></section>"""


def _workspace_path(raw_stdout: str) -> Path | None:
    paths = _workspace_paths(raw_stdout)
    return paths[0] if paths else None


def _workspace_paths(raw_stdout: str) -> list[Path]:
    prefixes = ("Resume workspace: ", "Resume variant workspace: ")
    values: list[Path] = []
    for line in raw_stdout.splitlines():
        for prefix in prefixes:
            if line.startswith(prefix):
                values.append(Path(line[len(prefix) :]))
                break
    return values


def _resume_provenance_panel(
    run_dir: Path,
    locale: UILocale = DEFAULT_UI_LOCALE,
) -> str:
    payload = _load_json_file(run_dir / "12_resume_provenance.json")
    if payload is None:
        return ""
    try:
        receipt = ResumeProvenanceReceipt.model_validate(payload)
    except ValueError:
        return (
            '<section class="resume-provenance error-state">'
            f'<h3>{_escape(ui_text(locale, "事实映射不可用", "Claim mapping unavailable"))}</h3>'
            f'<p>{_escape(ui_text(locale, "本次回执没有通过本地验证；请勿仅凭该映射投递。", "The local receipt did not validate. Do not rely on this mapping alone before applying."))}</p>'
            "</section>"
        )
    rows: list[str] = []
    for claim in receipt.claims:
        status_label = ui_text(
            locale,
            "原文已核对"
            if claim.status == ResumeClaimVerificationStatus.VERIFIED
            else "改写有支持",
            "Exact source verified"
            if claim.status == ResumeClaimVerificationStatus.VERIFIED
            else "Rewrite supported",
        )
        signal_label = ui_text(
            locale,
            f"关联 {len(claim.hiring_signal_ids)} 个 JD 信号",
            f"{len(claim.hiring_signal_ids)} linked JD signals",
        )
        rows.append(
            '<article class="provenance-claim">'
            f'<div><strong>{_escape(claim.final_text)}</strong>'
            f'<p><code>{_escape(claim.profile_entry_id)}</code> · {_escape(signal_label)}</p></div>'
            f'<span class="status-badge">{_escape(status_label)}</span>'
            "</article>"
        )
    counts = _resume_provenance_summary(receipt)
    return f'''<details class="resume-provenance">
  <summary>{_escape(ui_text(locale, "为什么这些内容会出现在我的简历里？", "Why is this on my resume?"))}</summary>
  <p>{_escape(ui_text(locale, "每条最终 bullet 都绑定到一条你批准的简历事实。AI 改写必须保留已核对事实，无法支持的内容不会进入 DOCX。", "Every final bullet is bound to one approved resume fact. AI rewrites must preserve checked facts; unsupported content cannot enter the DOCX."))}</p>
  <div class="provenance-summary">
    <strong>{_escape(str(counts["claim_count"]))}</strong>
    <span>{_escape(ui_text(locale, "条可追溯 bullet", "traceable bullets"))}</span>
  </div>
  <div class="provenance-claims">{"".join(rows)}</div>
  <p class="hint">{_escape(ui_text(locale, "为保护隐私，本次回执只保存批准事实的 ID 和哈希，不复制原始简历或 JD 正文。连接本地工作资料后，可再查看项目、代码和 BuildLog 锚点。", "For privacy, this receipt keeps approved fact IDs and hashes without copying the source resume or JD bodies. After connecting local work, project, code, and BuildLog anchors can be shown."))}</p>
</details>'''


def _resume_phase_label(
    phase: str,
    locale: UILocale = DEFAULT_UI_LOCALE,
) -> str:
    labels = {
        "QUEUED": ("等待开始", "Waiting to start"),
        "PREPARING": ("读取简历与 JD", "Reading resume and JD"),
        "GENERATING": ("生成针对性内容", "Generating tailored content"),
        "VERIFYING": ("核对事实与结构", "Verifying facts and structure"),
        "EXPERT_REVIEW": ("专家审阅", "Expert review"),
        "REVERIFYING": ("再次核对事实", "Re-verifying facts"),
        "RETRIEVING": ("检索本地证据", "Retrieving local evidence"),
        "EXPORTING": ("保存 DOCX", "Saving DOCX"),
        "DOCX_READY": ("DOCX 已就绪", "DOCX ready"),
        "PREVIEWING": ("生成 PDF 预览", "Building PDF preview"),
        "COMPLETE": ("全部完成", "Complete"),
        "FAILED": ("本次未完成", "Run stopped"),
    }
    return ui_text(locale, *labels.get(phase, labels["QUEUED"]))


def _resume_job_panel(
    snapshot: ResumeJobSnapshot,
    locale: UILocale = DEFAULT_UI_LOCALE,
) -> str:
    progress_value = {
        "QUEUED": 5,
        "PREPARING": 15,
        "GENERATING": 45,
        "VERIFYING": 65,
        "EXPERT_REVIEW": 72,
        "REVERIFYING": 78,
        "RETRIEVING": 74,
        "EXPORTING": 84,
        "DOCX_READY": 90,
        "PREVIEWING": 95,
        "COMPLETE": 100,
        "FAILED": 100,
    }.get(snapshot.phase, 5)
    phase_label = _resume_phase_label(snapshot.phase, locale)
    if snapshot.phase == "PREVIEWING" and snapshot.result is not None:
        detail = ui_text(
            locale,
            "DOCX 已可下载；PDF 预览仍在后台生成。你可以先去其他页面，稍后返回。",
            "The DOCX is ready to download while the PDF preview finishes in the background. You can visit another page and return later.",
        )
    elif snapshot.phase == "COMPLETE":
        detail = ui_text(
            locale,
            "简历和可用预览已经准备好。",
            "The resume and available preview are ready.",
        )
    elif snapshot.phase == "FAILED":
        failed_label = _resume_phase_label(snapshot.failed_phase or "FAILED", locale)
        detail = ui_text(
            locale,
            f"流程停在“{failed_label}”；没有静默生成通用简历。",
            f'The run stopped at "{failed_label}". No generic fallback resume was created.',
        )
    elif snapshot.phase == "QUEUED":
        detail = ui_text(
            locale,
            "已有一个简历任务正在运行；本次任务会按顺序自动开始。",
            "Another resume job is running. This one will start automatically in order.",
        )
    else:
        detail = ui_text(
            locale,
            "任务在后台继续运行；页面会自动更新，你也可以先处理其他事情。",
            "The job continues in the background. This page updates automatically, and you can work elsewhere meanwhile.",
        )

    steps = ["PREPARING", "GENERATING", "VERIFYING"]
    if (
        snapshot.phase in {"EXPERT_REVIEW", "REVERIFYING"}
        or snapshot.failed_phase in {"EXPERT_REVIEW", "REVERIFYING"}
        or "expert_review_ms" in snapshot.stage_durations_ms
    ):
        steps.extend(["EXPERT_REVIEW", "REVERIFYING"])
    if (
        snapshot.phase == "RETRIEVING"
        or snapshot.failed_phase == "RETRIEVING"
        or "retrieving_ms" in snapshot.stage_durations_ms
    ):
        steps.append("RETRIEVING")
    steps.extend(["EXPORTING", "PREVIEWING", "COMPLETE"])
    current_step = "PREVIEWING" if snapshot.phase == "DOCX_READY" else snapshot.phase
    step_items = []
    for phase in steps:
        duration_key = f"{phase.casefold()}_ms"
        state = "pending"
        if snapshot.phase == "COMPLETE" or duration_key in snapshot.stage_durations_ms:
            state = "done"
        if phase == current_step and snapshot.phase not in {"COMPLETE", "FAILED"}:
            state = "current"
        if snapshot.phase == "FAILED" and phase == snapshot.failed_phase:
            state = "failed"
        step_items.append(
            f'<li class="{state}"><span></span>{_escape(_resume_phase_label(phase, locale))}</li>'
        )

    duration_items = []
    timing_labels = (
        ("post_response_ms", "提交后返回", "POST response"),
        ("profile_extract_ms", "解析简历", "Profile extract"),
        ("retrieval_ms", "本地检索", "Evidence retrieval"),
        ("freshness_check_ms", "证据新鲜度检查", "Freshness check"),
        ("local_git_refresh_ms", "本地 Git 刷新", "Local Git refresh"),
        ("knowledge_sync_ms", "资料同步", "Knowledge sync"),
        ("requirement_extraction_ms", "提取 JD 要求", "Requirement extraction"),
        ("lexical_search_ms", "关键词检索", "Lexical search"),
        ("fusion_ms", "证据融合排序", "Evidence fusion"),
        ("evidence_admission_ms", "事实准入", "Evidence admission"),
        ("candidate_pack_build_ms", "构建证据包", "Candidate pack build"),
        ("model_generation_ms", "模型生成", "Model generation"),
        ("verification_ms", "本地验证", "Local verification"),
        ("expert_review_ms", "专家审阅", "Expert review"),
        ("reverification_ms", "再次验证", "Re-verification"),
        ("docx_ms", "保存 DOCX", "DOCX export"),
        ("pdf_preview_ms", "生成 PDF 预览", "PDF preview"),
    )
    for key, zh_label, en_label in timing_labels:
        value = snapshot.stage_durations_ms.get(key)
        if value is None:
            continue
        display_value = f"{value} ms" if value < 1000 else f"{value / 1000:.1f}s"
        duration_items.append(
            f"<li><span>{_escape(ui_text(locale, zh_label, en_label))}</span>"
            f"<strong>{display_value}</strong></li>"
        )
    total_display = (
        f"{snapshot.total_elapsed_ms} ms"
        if snapshot.total_elapsed_ms < 1000
        else f"{snapshot.total_elapsed_ms / 1000:.1f}s"
    )
    duration_items.append(
        f"<li><span>{_escape(ui_text(locale, '总耗时', 'Total'))}</span>"
        f"<strong>{total_display}</strong></li>"
    )
    timings = (
        f'<details class="job-timings"><summary>{_escape(ui_text(locale, "查看阶段耗时", "View stage timings"))}</summary><ul>{"".join(duration_items)}</ul></details>'
        if duration_items
        else ""
    )
    return f"""<section class="resume-job" data-phase="{_escape(snapshot.phase)}" aria-live="polite">
  <div class="resume-job-header">
    <div>
      <span class="result-kicker">{_escape(ui_text(locale, '后台任务', 'Background job'))}</span>
      <h2>{_escape(phase_label)}</h2>
      <p>{_escape(detail)}</p>
    </div>
    <strong>{snapshot.total_elapsed_ms / 1000:.1f}s</strong>
  </div>
  <progress value="{progress_value}" max="100">{progress_value}%</progress>
  <ol class="resume-job-steps">{"".join(step_items)}</ol>
  {timings}
  <div class="resume-job-actions">
    <a href="{_escape(ui_url('/', locale))}">{_escape(ui_text(locale, '返回 Today', 'Go to Today'))}</a>
    <span>{_escape(ui_text(locale, '任务不会因为离开本页而停止。', 'Leaving this page does not stop the job.'))}</span>
  </div>
</section>"""


def _user_result_card(
    result: UIActionResult | None,
    locale: UILocale = DEFAULT_UI_LOCALE,
    *,
    resume_job: ResumeJobSnapshot | None = None,
) -> str:
    if result is None:
        steps = (
            ("上传现有简历", "Upload your current resume"),
            ("粘贴完整 Job Description", "Paste the complete Job Description"),
            ("核对预览后再下载", "Review the preview before downloading"),
        )
        step_html = "".join(
            f'<li><span class="step-number">{index}</span>{_escape(ui_text(locale, zh, en))}</li>'
            for index, (zh, en) in enumerate(steps, start=1)
        )
        return f"""<section class="result-card empty-state">
  <span class="result-kicker">{_escape(ui_text(locale, '从这里开始', 'Start here'))}</span>
  <h2>{_escape(ui_text(locale, '先从一份真实简历和一个 JD 开始', 'Start with one real resume and one JD'))}</h2>
  <p>{_escape(ui_text(locale, 'SoloScale 只使用你确认过的经历；选择 AI 生成时，会在你确认后仅提交 JD 与这些事实。', 'SoloScale uses only the experience you confirm. With AI generation, only the JD and those facts are submitted after your approval.'))}</p>
  <ol class="empty-steps">{step_html}</ol>
</section>"""
    if result.return_code != 0:
        message = result.stderr or ui_text(
            locale,
            "生成失败，请检查输入后重试。",
            "Generation failed. Check the input and try again.",
        )
        source_unreadable = "PDF content expands beyond the safety limit" in message
        failure_heading = ui_text(
            locale,
            "输入文件无法安全读取" if source_unreadable else "这次没有生成简历",
            "The input file could not be read safely"
            if source_unreadable
            else "No resume was generated this time",
        )
        return f"""<section class="result-card error-state" role="alert">
  <span class="result-kicker">{_escape(ui_text(locale, '需要处理', 'Needs attention'))}</span>
  <h2>{_escape(failure_heading)}</h2>
  <p>{_escape(message)}</p>
</section>"""

    run_dir = _workspace_path(result.stdout)
    if run_dir is None:
        return f"""<section class="result-card error-state" role="alert">
  <h2>{_escape(ui_text(locale, '找不到本次生成结果', 'This result could not be found'))}</h2>
</section>"""
    resume = ""
    try:
        resume = (run_dir / "04_resume.md").read_text(encoding="utf-8")
    except OSError:
        pass
    verification = _load_json_file(run_dir / "07_verification.json") or {}
    gaps_payload = _load_json_file(run_dir / "05_gaps.json") or {}
    user_metadata = _load_json_file(run_dir / "09_user_ui.json") or {}
    variant_cards: list[str] = []
    for variant_dir in _workspace_paths(result.stdout)[1:]:
        if (
            _RUN_ID_RE.fullmatch(variant_dir.name) is None
            or variant_dir.parent != run_dir.parent
            or variant_dir.is_symlink()
        ):
            continue
        variant_metadata = _load_json_file(variant_dir / "09_user_ui.json") or {}
        variant_locale = str(variant_metadata.get("output_locale", ""))
        variant_download = str(variant_metadata.get("download_url", ""))
        variant_preview = str(variant_metadata.get("preview_url", ""))
        variant_pdf_status = str(variant_metadata.get("pdf_status", "PENDING"))
        variant_name = str(
            variant_metadata.get("output_filename", "Tailored Resume.docx")
        )
        if variant_locale not in {"en-US", "zh-CN"} or not variant_download:
            continue
        preview_link = (
            f'<a href="{_escape(variant_preview)}" target="_blank" rel="noopener">PDF</a>'
            if variant_preview
            else f'<span>{_escape(ui_text(locale, "PDF 待处理", "PDF needs attention") if variant_pdf_status == "NEEDS_ATTENTION" else ui_text(locale, "PDF 生成中", "PDF pending"))}</span>'
        )
        variant_cards.append(
            '<article class="resume-variant-card">'
            f'<strong>{_escape(variant_locale)}</strong>'
            f'<a class="secondary-button download" href="{_escape(variant_download)}" download title="{_escape(variant_name)}">DOCX</a>'
            f'{preview_link}</article>'
        )
    request_scoped = (
        user_metadata.get("retention") == "request_scoped_sources_not_persisted"
    )
    coverage = verification.get("coverage")
    if not isinstance(coverage, dict):
        coverage = {}
    gap_items = gaps_payload.get("gaps")
    if not isinstance(gap_items, list):
        gap_items = []
    gap_lines = [
        str(item.get("skill", "")).strip()
        for item in gap_items
        if isinstance(item, dict) and str(item.get("skill", "")).strip()
    ]
    model_gap_quotes = user_metadata.get("model_gap_quotes")
    if not request_scoped and isinstance(model_gap_quotes, list):
        for value in model_gap_quotes:
            gap = str(value).strip()
            if gap and gap not in gap_lines:
                gap_lines.append(gap)
    if request_scoped:
        raw_count = user_metadata.get("unsupported_requirement_count", len(gap_items))
        count = raw_count if isinstance(raw_count, int) else len(gap_items)
        if count > 0:
            gap_lines = [
                ui_text(
                    locale,
                    f"有 {count} 项要求暂未找到受支持证据；投递前请对照原始 JD 复核。",
                    f"{count} requirements still need supported evidence. Compare them with the original JD before applying.",
                )
            ]
    gap_html = "".join(f"<li>{_escape(item)}</li>" for item in gap_lines[:8])
    if not gap_html:
        gap_html = f"<li>{_escape(ui_text(locale, '没有发现明确的未覆盖项。', 'No explicit uncovered requirement was found.'))}</li>"
    download_url = str(user_metadata.get("download_url", ""))
    preview_url = str(user_metadata.get("preview_url", ""))
    pdf_status = str(user_metadata.get("pdf_status", "PENDING"))
    output_name = str(user_metadata.get("output_filename", "Tailored Resume.docx"))
    output_locale = str(user_metadata.get("output_locale", "en-US"))
    internal_path = str(user_metadata.get("internal_docx", run_dir / "08_resume.docx"))
    external_path = str(user_metadata.get("external_docx", ""))
    project_count = user_metadata.get("project_blocks_reordered", 0)
    skill_count = user_metadata.get("skill_bullets_reordered", 0)
    grounded_count = user_metadata.get("grounded_rewrites", 0)
    synthesis_count = user_metadata.get("synthesized_rewrites", 0)
    summary_rewritten = user_metadata.get("summary_rewritten") is True
    rejected_count = user_metadata.get("rejected_rewrites", 0)
    evidence_fact_count = user_metadata.get("candidate_evidence_fact_count", 0)
    retrieved_count = user_metadata.get("evidence_retrieved_count", 0)
    admitted_count = user_metadata.get("evidence_admitted_count", evidence_fact_count)
    requirement_rows = user_metadata.get("evidence_requirements", [])
    if not isinstance(requirement_rows, list):
        requirement_rows = []
    requirement_rows = [item for item in requirement_rows if isinstance(item, dict)]
    if requirement_rows:
        coverage = {
            "total": len(requirement_rows),
            "lexical_candidate_strong": sum(
                item.get("status") == "STRONG" for item in requirement_rows
            ),
            "lexical_candidate_partial": sum(
                item.get("status") == "MEDIUM" for item in requirement_rows
            ),
            "no_lexical_candidate": sum(
                item.get("status") == "GAP" for item in requirement_rows
            ),
        }
    requirement_coverage_html = "".join(
        (
            "<details><summary>"
            f"{_escape(str(item.get('requirement_id', 'Requirement')))} · "
            f"{_escape(str(item.get('status', 'GAP')))}</summary>"
            f"<p>{_escape(', '.join(str(value) for value in item.get('matched_fact_ids', [])) or ui_text(locale, '暂无已验证事实', 'No verified fact yet'))}</p></details>"
        )
        for item in requirement_rows
    )
    source_rows = user_metadata.get("evidence_source_summary", [])
    if not isinstance(source_rows, list):
        source_rows = []
    source_summary_html = "".join(
        (
            "<li><strong>"
            f"{_escape(str(item.get('source_type', '')).replace('_', ' ').title())}"
            "</strong> · "
            f"{_escape(str(item.get('state', 'UNAVAILABLE')))} · "
            f"{_escape(str(item.get('retrieved_count', 0)))} {_escape(ui_text(locale, '条检索', 'retrieved'))} · "
            f"{_escape(str(item.get('sent_count', 0)))} {_escape(ui_text(locale, '条发送', 'sent'))}</li>"
        )
        for item in source_rows
        if isinstance(item, dict)
    )
    adoption_rows = user_metadata.get("evidence_adoption", [])
    if not isinstance(adoption_rows, list):
        adoption_rows = []
    adoption_html = "".join(
        "<li><code>"
        f"{_escape(str(item.get('fact_id', 'FACT')))}</code> · "
        + " → ".join(
            stage
            for stage, active in (
                ("RETRIEVED", item.get("retrieved") is True),
                ("ADMITTED", item.get("admitted") is True),
                ("SENT", item.get("sent_to_model") is True),
                ("PROPOSED", item.get("proposed") is True),
                ("ACCEPTED", item.get("accepted") is True),
                ("RENDERED", item.get("rendered") is True),
            )
            if active
        )
        + (
            " · REJECTED: "
            + _escape(
                ", ".join(str(code) for code in item.get("rejection_rule_codes", []))
            )
            if item.get("rejection_rule_codes")
            else ""
        )
        + "</li>"
        for item in adoption_rows
        if isinstance(item, dict)
        and (item.get("proposed") is True or item.get("rejection_rule_codes"))
    )
    evidence_projects = user_metadata.get("candidate_evidence_projects", [])
    if not isinstance(evidence_projects, list):
        evidence_projects = []
    positioning_role = str(
        user_metadata.get("positioning_role_title") or ui_text(locale, "目标岗位", "Target role")
    )
    tailored_count = (project_count if isinstance(project_count, int) else 0) + (
        skill_count if isinstance(skill_count, int) else 0
    )
    generation_mode = str(user_metadata.get("generation_mode", "template"))
    provider = str(user_metadata.get("provider", "template"))
    ai_service_label = _human_ai_service_label(
        locale, provider, str(user_metadata.get("model") or "")
    )
    if generation_mode == "ai":
        result_summary = ui_text(
            locale,
            f"已按目标 JD 优化整份简历，采用 {grounded_count if isinstance(grounded_count, int) else 0} 项受支持改写，其中 {synthesis_count if isinstance(synthesis_count, int) else 0} 条为多证据综合；Summary{'已重写' if summary_rewritten else '保留原文'}。另有 {rejected_count if isinstance(rejected_count, int) else 0} 项未通过事实校验，已逐项回退。",
            f"The full resume was optimized for the target JD. {grounded_count if isinstance(grounded_count, int) else 0} supported changes were used, including {synthesis_count if isinstance(synthesis_count, int) else 0} multi-evidence bullet syntheses. The Summary was {'rewritten' if summary_rewritten else 'preserved'}. {rejected_count if isinstance(rejected_count, int) else 0} unsafe suggestions were rejected and individually restored.",
        )
        privacy_note = ui_text(
            locale,
            f"AI 服务：{ai_service_label}。本地检索到 {retrieved_count} 条资料线索，最终只准入并提交 {admitted_count} 条已验证事实；原始对话、项目文件和未选资料未发送。",
            f"AI service: {ai_service_label}. Local retrieval found {retrieved_count} discovery clues; only {admitted_count} verified facts were admitted and sent. Raw conversations, project files, and unselected material were not sent.",
        )
        if user_metadata.get("role_strategy_fallback_applied") is True:
            result_summary += ui_text(
                locale,
                " AI 提出的部分岗位定位缺少足够证据，SoloScale 已自动使用安全定位继续生成。",
                " Some AI positioning lacked sufficient evidence, so SoloScale continued with a deterministic safe strategy.",
            )
        if user_metadata.get("expert_review_performed") is True:
            expert_rewrites = user_metadata.get("expert_rewrites", 0)
            result_summary += ui_text(
                locale,
                f" 随后由 GPT-5.6 Sol 提交 {expert_rewrites} 个 patch，并在本地再次通过事实校验。",
                f" GPT-5.6 Sol then proposed {expert_rewrites} patches, which passed the local fact verifier again.",
            )
            privacy_note += ui_text(
                locale,
                " 专家审阅只接收 JD 信号、支持片段和当前草稿。",
                " Expert review received only JD signals, supporting fragments, and the current draft.",
            )
        elif user_metadata.get("expert_review_attempted") is True:
            result_summary += ui_text(
                locale,
                " 基础简历已成功生成；专家优化未通过版本或事实校验，因此没有应用，安全版本已保留。",
                " The base resume succeeded. Expert optimization did not pass draft or fact validation, so it was skipped and the safe version was preserved.",
            )
    else:
        result_summary = ui_text(
            locale,
            f"本次明确使用安全离线模式，仅按 JD 词面相关性调整了 {tailored_count} 个位置，所有候选人陈述保持逐字不变。",
            f"This run explicitly used safe offline mode. It reordered {tailored_count} positions by lexical JD relevance and kept every candidate statement verbatim.",
        )
        privacy_note = ui_text(
            locale,
            "本次是明确选择的安全离线草稿；没有模型或网络调用。",
            "This was an explicitly selected safe offline draft. No model or network call occurred.",
        )
    download = (
        f'<a class="primary-button download" href="{_escape(download_url)}" download '
        f'title="{_escape(output_name)}">{_escape(ui_text(locale, "下载 DOCX 简历", "Download DOCX resume"))}</a>'
        if download_url
        else ""
    )
    if preview_url:
        preview_action = (
            f'<a class="preview-link" href="{_escape(preview_url)}" target="_blank" '
            f'rel="noopener">{_escape(ui_text(locale, "在新窗口打开", "Open in a new window"))}</a>'
        )
        preview_content = f"""<div class="resume-pdf-shell">
        <object class="resume-pdf-preview" data="{_escape(preview_url)}" type="application/pdf">
          <p>{_escape(ui_text(locale, '浏览器无法内嵌 PDF。', 'The PDF cannot be embedded here.'))}<a href="{_escape(preview_url)}" target="_blank"
            rel="noopener">{_escape(ui_text(locale, '打开简历预览', 'Open resume preview'))}</a></p>
        </object>
      </div>"""
        preview_note = ui_text(
            locale,
            "这是最终 DOCX 的本地 PDF 渲染；确认内容和版式后再下载。",
            "This is a local PDF render of the final DOCX. Review content and layout before downloading.",
        )
    else:
        preview_action = ""
        fallback_preview = resume or ui_text(
            locale,
            "DOCX 已生成；文字预览不可用。",
            "The DOCX was generated; a text preview is unavailable.",
        )
        preview_content = f'<pre class="resume-preview">{_escape(fallback_preview)}</pre>'
        if resume_job is not None and resume_job.preview_state in {
            "pending",
            "rendering",
        }:
            preview_note = ui_text(
                locale,
                "DOCX 已可下载；PDF 预览仍在后台生成，本页会自动更新。",
                "The DOCX is ready to download. The PDF preview is still rendering, and this page will update automatically.",
            )
        else:
            preview_note = (
                ui_text(
                    locale,
                    "简历已经生成并可下载 DOCX。PDF 预览未完成；你可以继续使用 DOCX，稍后再处理 PDF 排版。",
                    "The resume and DOCX are ready. PDF preview did not complete; you can use the DOCX now and address PDF layout later.",
                )
                if pdf_status == "NEEDS_ATTENTION"
                else ui_text(
                    locale,
                    "本机未找到 DOCX 渲染器，当前显示内容预览；最终版式保留上传模板。",
                    "No local DOCX renderer was found, so this is a content preview. The final file keeps the uploaded layout.",
                )
            )
    saved_locations = (
        f'<p><strong>SoloScale 私有运行：</strong><code>{_escape(internal_path)}</code></p>'
    )
    if external_path:
        saved_locations += (
            f'<p><strong>Resume Applications：</strong><code>{_escape(external_path)}</code></p>'
        )
    if request_scoped:
        saved_locations += (
            f'<p>{_escape(ui_text(locale, "你选择的原始简历、JD 和支持文件未写入长期存储；只保留生成结果与无正文回执。", "The original resume, JD, and support file you selected were not written to long-term storage. Only the generated result and body-free receipt are retained."))}</p>'
        )
    saved_locations += (
        f'<p><strong>Application Receipt：</strong><code>{_escape(str(run_dir / "application_receipt.json"))}</code></p>'
    )
    defense = (
        ""
        if request_scoped
        else _interview_defense_panel(
            data_root=run_dir.parents[1], run_id=run_dir.name, locale=locale
        )
    )
    provenance_panel = _resume_provenance_panel(run_dir, locale)
    job_running = resume_job is not None and resume_job.phase not in {
        "COMPLETE",
        "FAILED",
    }
    result_kicker = ui_text(
        locale,
        "DOCX 已就绪" if job_running else "已完成",
        "DOCX ready" if job_running else "Complete",
    )
    result_heading = ui_text(
        locale,
        "简历可先下载，预览正在生成" if job_running else "针对性简历已生成",
        "Your resume can be downloaded while preview finishes"
        if job_running
        else "Your tailored resume is ready",
    )
    display_elapsed_ms = (
        resume_job.total_elapsed_ms if resume_job is not None else result.elapsed_ms
    )
    return f"""<section class="result-card success-state" aria-live="polite">
  <div class="result-header">
    <div>
      <span class="result-kicker">{_escape(result_kicker)} · {_escape(_human_duration(locale, display_elapsed_ms))}</span>
      <h2>{_escape(result_heading)}</h2>
      <p>{_escape(result_summary)}</p>
    </div>
    {download}
  </div>
  <p class="privacy-note"><strong>{_escape(ui_text(locale, '输出语言', 'Output language'))}:</strong> {_escape(output_locale)}</p>
  {f'<div class="resume-variant-downloads">{"".join(variant_cards)}</div>' if variant_cards else ''}
  <div class="metrics" aria-label="{_escape(ui_text(locale, '覆盖情况', 'Coverage'))}">
    <div><strong>{_escape(str(coverage.get("total", 0)))}</strong><span>{_escape(ui_text(locale, '岗位要求', 'Requirements'))}</span></div>
    <div><strong>{_escape(str(coverage.get("lexical_candidate_strong", 0)))}</strong><span>{_escape(ui_text(locale, '强证据候选', 'Strong candidates'))}</span></div>
    <div><strong>{_escape(str(coverage.get("lexical_candidate_partial", 0)))}</strong><span>{_escape(ui_text(locale, '部分证据候选', 'Partial candidates'))}</span></div>
    <div><strong>{_escape(str(coverage.get("no_lexical_candidate", 0)))}</strong><span>{_escape(ui_text(locale, '待补证', 'Needs evidence'))}</span></div>
  </div>
  <details>
    <summary>{_escape(ui_text(locale, '查看本次证据来源', 'Evidence for this resume'))}</summary>
    <ul class="gap-list">{source_summary_html}</ul>
  </details>
  <details>
    <summary>{_escape(ui_text(locale, '查看 JD 要求覆盖', 'View JD requirement coverage'))}</summary>
    {requirement_coverage_html}
  </details>
  <details>
    <summary>{_escape(ui_text(locale, '查看证据采用轨迹', 'View evidence adoption trace'))}</summary>
    <ul class="gap-list">{adoption_html or f'<li>{_escape(ui_text(locale, "本次没有模型采用轨迹。", "No model adoption trace is available for this run."))}</li>'}</ul>
  </details>
  <p class="privacy-note"><strong>{_escape(ui_text(locale, 'JD 定位', 'JD positioning'))}:</strong> {_escape(positioning_role)} · <strong>{_escape(ui_text(locale, '候选证据包', 'Candidate Evidence Pack'))}:</strong> {_escape(str(evidence_fact_count))} {_escape(ui_text(locale, '条事实', 'facts'))}{(' · ' + _escape(', '.join(str(item) for item in evidence_projects))) if evidence_projects else ''}</p>
  <div class="result-grid">
    <div>
      <div class="preview-heading"><h3>{_escape(ui_text(locale, '简历预览', 'Resume preview'))}</h3>{preview_action}</div>
      {preview_content}
      <p class="preview-note">{_escape(preview_note)}</p>
    </div>
    <aside>
      <h3>{_escape(ui_text(locale, '建议人工复核', 'Human review suggested'))}</h3>
      <ul class="gap-list">{gap_html}</ul>
      <p class="privacy-note">{_escape(privacy_note)}</p>
      <form method="post" action="/resume/unlock-local-scan">
        <input type="hidden" name="ui_locale" value="{locale}" />
        <button class="secondary-button" type="submit">{_escape(ui_text(locale, '使用我的真实工作资料', 'Use my real work'))}</button>
      </form>
      <p class="hint">{_escape(ui_text(locale, '需要更强证据时，再由你明确选择本地项目、ChatGPT/Codex 导出或 BuildLog 回执。', 'When you need stronger evidence, explicitly choose a local project, ChatGPT/Codex export, or BuildLog receipt.'))}</p>
    </aside>
  </div>
  <details>
    <summary>{_escape(ui_text(locale, '查看自动保存位置', 'View saved locations'))}</summary>
    {saved_locations}
  </details>
  {provenance_panel}
  {defense}
</section>"""


def _available_learning_anchor_pack(
    data_root: Path, repo_root: Path
) -> tuple[str, dict[str, object]] | None:
    root = data_root / "learning-runs"
    if root.is_symlink() or not root.is_dir():
        return None
    for candidate in sorted(root.iterdir(), key=lambda item: item.name, reverse=True):
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        try:
            pack = load_interview_anchor_pack(
                data_root=data_root,
                repository_root=repo_root,
                run_id=candidate.name,
            )
        except (LearningTraceabilityError, OSError, ValueError):
            continue
        return candidate.name, pack
    return None


def _validated_record_anchor_pack(
    *,
    record: InterviewDefenseRecord,
    data_root: Path,
    repo_root: Path,
    learning_run_id: str,
) -> dict[str, object]:
    if record.mapping is None or record.mapping.learning_run_id != learning_run_id:
        raise ValueError("mapping mismatch")
    pack = load_interview_anchor_pack(
        data_root=data_root,
        repository_root=repo_root,
        run_id=learning_run_id,
    )
    project = pack.get("project")
    if not isinstance(project, dict) or record.mapping.anchor_pack != pack:
        raise ValueError("mapping anchor pack mismatch")
    if (
        record.mapping.case_id != pack.get("case_id")
        or record.mapping.mapping_basis != "OPERATOR_CONFIRMED"
        or record.mapping.repository != project.get("repository")
        or record.mapping.branch != project.get("branch")
        or record.mapping.commit != project.get("commit")
    ):
        raise ValueError("mapping identity mismatch")
    return pack


def _interview_defense_panel(
    *,
    data_root: Path,
    run_id: str,
    repo_root: Path | None = None,
    locale: UILocale = DEFAULT_UI_LOCALE,
) -> str:
    repo_root = repo_root or _repo_root()
    try:
        records = load_interview_defense_records(data_root=data_root, run_id=run_id)
    except (ResumeWorkspaceStorageError, ValueError):
        return (
            f'<section class="interview-defense"><h3>{_escape(ui_text(locale, "面试答辩", "Interview Defense"))}</h3>'
            f'<p>{_escape(ui_text(locale, "暂不可用。", "Unavailable."))}</p></section>'
        )
    available = _available_learning_anchor_pack(data_root, repo_root)
    rows: list[str] = []
    for record in records:
        display_status = record.status.value
        if record.mapping is not None and record.status.value == "MAPPED":
            try:
                _validated_record_anchor_pack(
                    record=record,
                    data_root=data_root,
                    repo_root=repo_root,
                    learning_run_id=record.mapping.learning_run_id,
                )
            except (LearningTraceabilityError, OSError, ValueError):
                display_status = "NEEDS_MAPPING"
                action = _escape(
                    ui_text(
                        locale,
                        "关联已失效；请修复原始学习记录，或重新生成这次简历后再关联。",
                        "The mapping is stale. Repair the original Learning run or regenerate this resume before mapping again.",
                    )
                )
            else:
                href = ui_url(
                    "/learning#interview-defense",
                    locale,
                    run_id=record.mapping.learning_run_id,
                    resume_run_id=run_id,
                    bullet_id=record.bullet_id,
                )
                action = f'<a href="{_escape(href)}">{_escape(ui_text(locale, "面试答辩", "Interview Defense"))} →</a>'
        elif available is not None:
            learning_run, _pack = available
            action = f"""<form method="post" action="/resume/interview-defense/map">
  <input type="hidden" name="ui_locale" value="{locale}" />
  <input type="hidden" name="resume_run_id" value="{_escape(run_id)}" />
  <input type="hidden" name="bullet_id" value="{_escape(record.bullet_id)}" />
  <input type="hidden" name="learning_run_id" value="{_escape(learning_run)}" />
  <button type="submit">{_escape(ui_text(locale, '确认关联对话式 RAG 锚点', 'Confirm Conversation RAG anchors'))}</button>
</form>"""
        else:
            action = f'<a href="{ui_url("/learning", locale)}">{_escape(ui_text(locale, "先建立学习黄金案例。", "Build the Learning golden case first."))}</a>'
        rows.append(
            f"<li><code>{_escape(record.bullet_id)}</code> · "
            f"{_escape(ui_display_value(locale, display_status))}<br />"
            f"{_escape(record.bullet_text)}<br />{action}</li>"
        )
    if available is None:
        availability = ui_text(
            locale,
            "当前没有可用的对话式 RAG 学习记录。",
            "No Conversation RAG Learning run is currently available.",
        )
    else:
        learning_run, pack = available
        project = pack.get("project")
        if not isinstance(project, dict):
            availability = ui_text(
                locale,
                "当前没有可用的对话式 RAG 学习记录。",
                "No Conversation RAG Learning run is currently available.",
            )
        else:
            availability = (
                ui_text(locale, "待你确认关联：", "Awaiting your mapping confirmation: ")
                +
                f"{learning_run} · {project.get('repository')} · "
                f"{project.get('branch')} · {project.get('commit')}"
            )
    return (
        f'<section class="interview-defense"><h3>{_escape(ui_text(locale, "面试答辩", "Interview Defense"))}</h3>'
        f"<p>{_escape(availability)}</p><ul>{''.join(rows)}</ul></section>"
    )


def _latest_run_directory(root: Path, prefix: str) -> Path | None:
    if root.is_symlink() or not root.is_dir():
        return None
    pattern = re.compile(rf"^{re.escape(prefix)}-[0-9]{{8}}T[0-9]{{6}}Z-[0-9a-f]+$")
    candidates = [
        path
        for path in root.iterdir()
        if pattern.fullmatch(path.name) and path.is_dir() and not path.is_symlink()
    ]
    return max(candidates, key=lambda path: path.name) if candidates else None


def _home_activity_html(
    data_root: Path,
    locale: UILocale,
    *,
    resume_job: ResumeJobSnapshot | None = None,
) -> str:
    cards: list[str] = []
    if resume_job is not None and resume_job.phase != "COMPLETE":
        if resume_job.phase == "FAILED":
            state = ui_text(locale, "需要处理", "Needs attention")
            title = ui_text(locale, "简历生成已停止", "Resume generation stopped")
            detail = ui_text(
                locale,
                "打开任务查看失败步骤；不会自动改用通用草稿。",
                "Open the job to review the failed step; no generic fallback was created.",
            )
            action = ui_text(locale, "查看失败详情", "Review failure")
        else:
            state = ui_text(locale, "处理中", "In progress")
            title = ui_text(locale, "简历正在后台生成", "Resume is generating in the background")
            detail = ui_text(
                locale,
                f"当前阶段：{_resume_phase_label(resume_job.phase, locale)} · {resume_job.total_elapsed_ms / 1000:.1f}s",
                f"Current stage: {_resume_phase_label(resume_job.phase, locale)} · {resume_job.total_elapsed_ms / 1000:.1f}s",
            )
            action = ui_text(locale, "返回任务进度", "Return to job progress")
        cards.append(
            f'''<article class="activity-card"><span class="activity-state">{_escape(state)}</span>
            <strong>{_escape(title)}</strong><p>{_escape(detail)}</p>
            <a href="{_escape(ui_url(f'/resume/jobs/{resume_job.job_id}', locale))}">{_escape(action)} →</a></article>'''
        )

    resume_dir = _latest_run_directory(data_root / "resume-runs", "resume")
    if resume_dir is not None:
        receipt_path = resume_dir / "09_user_ui.json"
        artifact_path = resume_dir / "08_resume.docx"
        if (
            receipt_path.is_file()
            and not receipt_path.is_symlink()
            and artifact_path.is_file()
            and not artifact_path.is_symlink()
        ):
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                receipt = None
            if isinstance(receipt, dict):
                expected_download = f"/downloads/{resume_dir.name}/resume.docx"
                download_url = receipt.get("download_url")
                output_filename = receipt.get("output_filename")
                if download_url == expected_download and isinstance(output_filename, str):
                    filename = Path(output_filename).name
                    if filename == output_filename:
                        cards.append(
                            f'''<article class="activity-card"><span class="activity-state">{_escape(ui_text(locale, '已生成', 'Generated'))}</span>
                            <strong>{_escape(ui_text(locale, '最近简历可以继续下载', 'Your recent resume is ready'))}</strong>
                            <p>{_escape(filename)}</p><a href="{_escape(expected_download)}">{_escape(ui_text(locale, '下载最近简历', 'Download recent resume'))} →</a></article>'''
                        )

    content_dir = _latest_run_directory(data_root / "content-runs", "content")
    if content_dir is not None:
        try:
            load_content_run(data_root, content_dir.name)
            review = load_content_review(data_root, content_dir.name)
        except (ContentWorkspaceError, OSError, ValueError):
            pass
        else:
            decision = (
                review[0].decision if review is not None else ContentReviewDecision.DRAFT
            )
            state_copy = {
                ContentReviewDecision.DRAFT: ui_text(locale, "待审核", "Ready for review"),
                ContentReviewDecision.APPROVED: ui_text(locale, "已批准", "Approved"),
                ContentReviewDecision.REJECTED: ui_text(locale, "需要处理", "Needs attention"),
            }[decision]
            action_copy = (
                ui_text(locale, "查看并准备发布预览", "Open publishing preview")
                if decision is ContentReviewDecision.APPROVED
                else ui_text(locale, "继续审核内容包", "Continue reviewing bundle")
            )
            cards.append(
                f'''<article class="activity-card"><span class="activity-state">{_escape(state_copy)}</span>
                <strong>{_escape(ui_text(locale, '最近内容包', 'Recent content bundle'))}</strong>
                <p>{_escape(ui_text(locale, '统一内容包已私有保存；发布仍需要你的最终确认。', 'The unified bundle is saved privately; publication still requires your final confirmation.'))}</p>
                <a href="{_escape(ui_url('/creator/create', locale, run_id=content_dir.name))}">{_escape(action_copy)} →</a></article>'''
            )

    cards.append(
        f'''<article class="activity-card"><span class="activity-state">{_escape(ui_text(locale, '本地', 'Local'))}</span>
        <strong>{_escape(ui_text(locale, '扫描我最近做的事', 'Scan my recent work'))}</strong>
        <p>{_escape(ui_text(locale, '只读取已有本地回执和元数据，不调用模型。', 'Uses existing local receipts and metadata without a model call.'))}</p>
        <a href="{_escape(ui_url('/creator/create', locale, scan_range='today'))}">{_escape(ui_text(locale, '扫描今天', 'Scan today'))} →</a></article>'''
    )
    return f'''<section class="today-activity" aria-label="{_escape(ui_text(locale, '最近工作', 'Recent work'))}">
    <span class="kicker">{_escape(ui_text(locale, '继续上次工作', 'Continue where you left off'))}</span>
    <h2>{_escape(ui_text(locale, '最近结果和下一步', 'Recent results and next actions'))}</h2>
    <div class="activity-grid">{''.join(cards)}</div></section>'''


def _home_page(
    locale: UILocale = DEFAULT_UI_LOCALE,
    *,
    data_root: Path | None = None,
    workspace_root: Path | None = None,
    resume_job: ResumeJobSnapshot | None = None,
) -> str:
    """Render the outcome-first product home without exposing internal modules."""

    work_snapshot = load_work_context(
        data_root or Path(".soloscale"), workspace_root=workspace_root
    )
    work_strip = render_work_context_strip(work_snapshot, locale)
    activity = _home_activity_html(
        data_root or Path(".soloscale"), locale, resume_job=resume_job
    )
    body = f"""{work_strip}
<section class="outcome-grid" aria-label="{_escape(ui_text(locale, '主要目标', 'Primary outcomes'))}">
  <article class="outcome-card job-card">
    <a class="outcome-hitbox" href="{ui_url('/resume', locale)}" aria-label="{_escape(ui_text(locale, '定制我的简历', 'Tailor my resume'))}"></a>
    <span class="outcome-number">01</span>
    <span class="kicker">{_escape(ui_text(locale, '找到机会', 'Get the job'))}</span>
    <h2>{_escape(ui_text(locale, '为目标岗位准备更合适的申请', 'Build a stronger application for the role'))}</h2>
    <p>{_escape(ui_text(locale, '上传现有简历、粘贴完整 JD，核对后下载针对性版本。', 'Upload your current resume, paste the full JD, review it, and download a tailored version.'))}</p>
    <span class="outcome-action">{_escape(ui_text(locale, '定制我的简历', 'Tailor my resume'))}<span aria-hidden="true">→</span></span>
  </article>
  <article class="outcome-card defend-card">
    <a class="outcome-hitbox" href="{ui_url('/learning', locale)}" aria-label="{_escape(ui_text(locale, '开始面试准备', 'Prepare for an interview'))}"></a>
    <span class="outcome-number">02</span>
    <span class="kicker">{_escape(ui_text(locale, '能解释自己', 'Defend the job'))}</span>
    <h2>{_escape(ui_text(locale, '把简历里的项目准备成面试答案', 'Turn your project claims into interview-ready answers'))}</h2>
    <p>{_escape(ui_text(locale, '定位知识缺口，回到真实代码、步骤与决策，练习解释和追溯。', 'Find knowledge gaps, return to real code and decisions, and practice explaining and tracing your work.'))}</p>
    <span class="outcome-action">{_escape(ui_text(locale, '开始面试准备', 'Prepare for an interview'))}<span aria-hidden="true">→</span></span>
  </article>
  <article class="outcome-card visibility-card">
    <a class="outcome-hitbox" href="{ui_url('/creator', locale)}" aria-label="{_escape(ui_text(locale, '把工作变成内容', 'Turn my work into content'))}"></a>
    <span class="outcome-number">03</span>
    <span class="kicker">{_escape(ui_text(locale, '建立影响力', 'Build visibility'))}</span>
    <h2>{_escape(ui_text(locale, '把真实工作变成可信的公开内容', 'Turn real work into credible public content'))}</h2>
    <p>{_escape(ui_text(locale, '生成 LinkedIn、X 与视频素材，复核后再由你决定是否发布。', 'Create LinkedIn, X, and video assets, review them, and decide what gets published.'))}</p>
    <span class="outcome-action">{_escape(ui_text(locale, '把工作变成内容', 'Turn my work into content'))}<span aria-hidden="true">→</span></span>
    <div class="secondary-actions">
      <a href="{ui_url('/video', locale)}">{_escape(ui_text(locale, '创建视频', 'Create video'))}</a>
      <a href="{ui_url('/creator/publish', locale)}">{_escape(ui_text(locale, '发布已审核内容', 'Publish approved content'))}</a>
    </div>
  </article>
</section>
{activity}
<aside class="home-promise">
  <strong>{_escape(ui_text(locale, '同一份真实工作，连续支持三个结果。', 'One body of real work, supporting three connected outcomes.'))}</strong>
  <span>{_escape(ui_text(locale, '日常只需选择目标；其他设置在需要时从“更多”打开。', 'Choose an outcome and get to work; other settings remain available under More when needed.'))}</span>
</aside>"""
    return render_app_shell(
        active="home",
        locale=locale,
        current_url="/",
        title=f"SoloScale · {ui_text(locale, '把真实工作变成结果', 'Turn real work into outcomes')}",
        eyebrow=ui_text(locale, "三个结果，一个工作台", "Three outcomes, one workspace"),
        heading=ui_text(locale, "你今天想完成什么？", "What do you want to accomplish today?"),
        description=ui_text(
            locale,
            "SoloScale 帮你把真实工作变成更强的申请、能讲清楚的面试答案，以及可信的公开内容。",
            "SoloScale turns real work into stronger applications, interview-ready understanding, and credible public content.",
        ),
        body=body,
        extra_css="""
.work-context-strip{display:grid;grid-template-columns:minmax(230px,.8fr) minmax(260px,1.2fr) auto;gap:18px;align-items:center;margin-bottom:20px;padding:17px 20px;border:1px solid #d9e2dc;border-radius:17px;background:linear-gradient(110deg,rgba(255,255,255,.96),rgba(239,248,244,.92));box-shadow:0 10px 28px rgb(35 45 70 / 6%)}.work-context-strip>div{display:grid;gap:4px}.work-context-strip strong{font-size:14px}.work-context-strip p{margin:0;color:var(--text-muted)}.work-context-strip a{display:flex;align-items:center;gap:10px;white-space:nowrap;font-weight:850;text-decoration:none}.outcome-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px;align-items:stretch}.outcome-card{position:relative;overflow:hidden;display:flex;flex-direction:column;min-height:360px;padding:26px;border:1px solid #fff;border-radius:var(--radius-xl);box-shadow:var(--shadow-card);background:linear-gradient(160deg,#fff 0%,#f7f8ff 100%);cursor:pointer;transition:transform .16s ease,box-shadow .16s ease}.outcome-card:hover,.outcome-card:focus-within{transform:translateY(-3px);box-shadow:0 20px 52px rgb(35 45 70 / 13%),0 1px 2px rgb(35 45 70 / 6%)}.outcome-card::after{content:"";position:absolute;width:150px;height:150px;border-radius:50%;right:-55px;top:-55px;background:rgb(64 86 180 / 8%)}.outcome-hitbox{position:absolute;inset:0;z-index:2;border-radius:inherit}.outcome-hitbox:focus-visible{outline:3px solid var(--focus);outline-offset:-4px}.defend-card{background:linear-gradient(160deg,#fff 0%,#f2faf6 100%)}.defend-card::after{background:rgb(24 119 92 / 9%)}.visibility-card{background:linear-gradient(160deg,#fff 0%,#fbf6ff 100%)}.visibility-card::after{background:rgb(114 87 173 / 9%)}.outcome-number{color:var(--text-muted);font-size:12px;font-weight:850;letter-spacing:.12em}.outcome-card .kicker{margin-top:28px}.outcome-card h2{margin:10px 0 11px;font-size:25px;line-height:1.2;letter-spacing:-.035em}.outcome-card p{margin:0 0 24px;color:var(--text-muted)}.outcome-action{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:auto;padding:13px 15px;border-radius:13px;background:var(--brand);color:#fff;text-decoration:none;font-weight:850}.defend-card .outcome-action{background:var(--success)}.visibility-card .outcome-action{background:var(--brand-secondary)}.secondary-actions{position:relative;z-index:3;display:flex;gap:12px;flex-wrap:wrap;margin-top:13px}.secondary-actions a{font-size:12px;font-weight:750;text-decoration:none}.today-activity{margin-top:22px;padding:22px;border:1px solid var(--border);border-radius:20px;background:rgb(255 255 255 / 76%)}.today-activity h2{margin:5px 0 14px}.activity-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.activity-card{display:grid;gap:8px;padding:16px;border:1px solid var(--border);border-radius:15px;background:#fff}.activity-card strong{font-size:15px}.activity-card p{margin:0;color:var(--text-muted);font-size:13px}.activity-card a{justify-self:start;font-size:13px;font-weight:850;text-decoration:none}.activity-state{justify-self:start;padding:4px 8px;border-radius:999px;background:var(--success-soft);color:var(--success);font-size:11px;font-weight:850}.home-promise{display:flex;align-items:center;justify-content:center;gap:12px;margin-top:22px;padding:16px 20px;border:1px solid var(--border);border-radius:16px;background:rgb(255 255 255 / 72%);color:var(--text-muted)}.home-promise strong{color:var(--text)}
.work-context-strip .context-note{color:var(--text-muted);font-size:12px}
@media(max-width:940px){.work-context-strip{grid-template-columns:1fr}.outcome-grid,.activity-grid{grid-template-columns:1fr}.outcome-card{min-height:280px}.home-promise{align-items:flex-start;flex-direction:column}}""",
    )


def _applications_section_html(
    resume_library_root: Path, *, locale: UILocale
) -> str:
    try:
        records = list_application_records(resume_library_root)
    except (OSError, ValueError):
        records = []
    if not records:
        return f'''<section class="applications-overview" id="applications"><div class="result-head"><div><span class="kicker">{_escape(ui_text(locale, "申请与机会", "Applications / Opportunities"))}</span><h2>{_escape(ui_text(locale, "还没有申请记录", "No applications yet"))}</h2><p>{_escape(ui_text(locale, "生成并保存第一份针对性简历后，这里会显示申请状态、面试就绪与下一步。简历草稿不会被误当作投递记录。", "After you generate and save your first tailored resume, this area will show application status, interview readiness, and next actions. Resume drafts are never mistaken for applications."))}</p></div></div><a class="text-link" href="{ui_url('/resume#resume-form', locale)}">{_escape(ui_text(locale, "生成第一份简历", "Generate your first resume"))} →</a></section>'''
    status_labels = {
        ApplicationStatus.DRAFT: ui_text(locale, "草稿", "Draft"),
        ApplicationStatus.READY_TO_APPLY: ui_text(locale, "可投递", "Ready to apply"),
        ApplicationStatus.APPLIED: ui_text(locale, "已投递", "Applied"),
        ApplicationStatus.INTERVIEW: ui_text(locale, "面试中", "Interview"),
        ApplicationStatus.OFFER: ui_text(locale, "Offer", "Offer"),
        ApplicationStatus.REJECTED: ui_text(locale, "未通过", "Rejected"),
        ApplicationStatus.WITHDRAWN: ui_text(locale, "已撤回", "Withdrawn"),
    }
    cards: list[str] = []
    for record in records:
        status_options = "".join(
            f'<option value="{status.value}" {"selected" if status is record.status else ""}>{_escape(status_labels[status])}</option>'
            for status in ApplicationStatus
        )
        company = _escape(record.company or ui_text(locale, "未记录公司", "Company not recorded"))
        role = _escape(record.role or ui_text(locale, "未记录职位", "Role not recorded"))
        resume_action = (
            f'<a class="text-link" href="{ui_url(f"/downloads/{record.soloscale_run_id}/resume.docx", locale)}">{_escape(ui_text(locale, "打开简历", "Open resume"))} →</a>'
            if record.soloscale_run_id
            else ""
        )
        interview = (
            ui_text(locale, "是", "Yes")
            if record.interview_ready is True
            else ui_text(locale, "否", "No")
            if record.interview_ready is False
            else ui_text(locale, "未评估", "Not assessed")
        )
        next_action = _escape(record.next_action or ui_text(locale, "下一步由你决定", "Next action is yours to decide"))
        cards.append(
            f'''<article class="application-card"><div><span class="status-badge">{_escape(status_labels[record.status])}</span><h3>{role} · {company}</h3><p>{_escape(record.job_id or ui_text(locale, "无 JD 标识", "No JD identity"))}{(" · " + _escape(record.source_url)) if record.source_url else ""}</p><div class="application-truth"><span>{_escape(ui_text(locale, "面试就绪", "Interview ready"))}: <strong>{interview}</strong></span><span>{_escape(ui_text(locale, "下一步", "Next action"))}: <strong>{next_action}</strong></span></div></div><div class="application-actions">{resume_action}<form method="post" action="/applications/status"><input type="hidden" name="ui_locale" value="{locale}" /><input type="hidden" name="resume_library_root" value="{_escape(str(resume_library_root))}" /><input type="hidden" name="directory_name" value="{_escape(record.directory_name)}" /><select name="status">{status_options}</select><input name="note" placeholder="{_escape(ui_text(locale, "备注（可选）", "Note (optional)"))}" /><input name="next_action" placeholder="{_escape(ui_text(locale, "下一步（可选）", "Next action (optional)"))}" /><button type="submit">{_escape(ui_text(locale, "更新状态", "Update status"))}</button></form><a class="text-link" href="{ui_url('/learning', locale)}">{_escape(ui_text(locale, "准备面试 / 练习缺口", "Prepare interview / practice gap"))} →</a></div></article>'''
        )
    return f'''<section class="applications-overview" id="applications"><div class="result-head"><div><span class="kicker">{_escape(ui_text(locale, "申请与机会", "Applications / Opportunities"))}</span><h2>{_escape(ui_text(locale, "追踪每一次申请，而不是把简历草稿误当作投递记录。", "Track each application instead of mistaking resume drafts for applications."))}</h2><p>{_escape(ui_text(locale, "状态只在你记录真实外部结果后更新；SoloScale 不会替你发明投递或 Offer。", "Status updates only after you record a real external result; SoloScale never invents an application or offer."))}</p></div></div><div class="application-list">{''.join(cards)}</div></section>'''


def _localized_file_input(
    *,
    name: str,
    accept: str,
    required: bool,
    locale: UILocale,
    button_label: str,
    placeholder: str,
) -> str:
    required_attr = " required" if required else ""
    return f'''<span class="file-control"><input class="file-native" type="file" name="{_escape(name)}" accept="{_escape(accept)}"{required_attr} data-file-placeholder="{_escape(placeholder)}" /><span class="file-button" aria-hidden="true">{_escape(button_label)}</span><span class="file-name" data-file-name-for="{_escape(name)}">{_escape(placeholder)}</span></span>'''


def _user_page(
    action_result: UIActionResult | None,
    data_root: Path,
    form: dict[str, str],
    locale: UILocale = DEFAULT_UI_LOCALE,
    *,
    desktop_mode: bool = False,
    workspace_root: Path | None = None,
    resume_job: ResumeJobSnapshot | None = None,
    template_receipt: ResumeTemplateReceipt | None = None,
) -> str:
    job_description = _escape(form.get("job_description", ""))
    company_name = _escape(form.get("company_name", ""))
    company_url = _escape(form.get("company_url", ""))
    job_title = _escape(form.get("job_title", ""))
    job_id = _escape(form.get("job_id", ""))
    tailoring_instructions = _escape(form.get("tailoring_instructions", ""))
    generation_mode = form.get(
        "generation_mode", ModelProviderId.SOLOSCALE_HOSTED.value
    )
    if generation_mode not in {
        ModelProviderId.SOLOSCALE_HOSTED.value,
        ModelProviderId.OLLAMA.value,
        ModelProviderId.OPENAI_COMPATIBLE.value,
        "template",
    }:
        generation_mode = ModelProviderId.SOLOSCALE_HOSTED.value
    provider_model = _escape(form.get("provider_model", "qwen3:8b"))
    expert_review_configured = openai_api_key_is_configured()
    resume_library_root = (
        data_root / "resume-applications"
        if desktop_mode
        else Path.home() / "Documents" / "Resume Applications"
    )
    provider_label = {
        ModelProviderId.SOLOSCALE_HOSTED.value: ui_text(
            locale, "SoloScale 托管 AI · 推荐", "SoloScale Hosted AI · Recommended"
        ),
        ModelProviderId.OLLAMA.value: ui_text(
            locale, "本地 AI · 高级", "Local AI · Advanced"
        ),
        ModelProviderId.OPENAI_COMPATIBLE.value: ui_text(
            locale, "OpenAI API · 高级", "OpenAI API · Advanced"
        ),
        "template": ui_text(locale, "安全离线草稿 · 不使用 AI", "Safe offline draft · No AI"),
    }[generation_mode]
    workspace_class = (
        "workspace has-result"
        if action_result is not None and action_result.return_code == 0
        else "workspace"
    )
    work_summary = render_use_my_work(
        load_work_context(data_root, workspace_root=workspace_root),
        locale,
        boundary=ui_text(
            locale,
            "本次简历仍只使用你确认的模板经历作为事实；其他资料只帮助发现相关项目，不会自动新增经历。",
            "This resume still treats only your approved template experience as fact. Other work can help discovery, but never adds claims automatically.",
        ),
        return_path="/resume",
    )
    template_preview_id = form.get("resume_template_preview_id", "").strip()
    if (
        template_receipt is not None
        and template_preview_id
        and template_receipt.preview_id != template_preview_id
    ):
        template_receipt = None
    template_error = form.get("template_preview_error", "").strip()
    if template_receipt is not None:
        section_labels = {
            "SUMMARY": ui_text(locale, "个人简介", "Summary"),
            "PROJECT HIGHLIGHTS": ui_text(locale, "项目经历", "Projects"),
            "EDUCATION": ui_text(locale, "教育经历", "Education"),
            "TECHNICAL SKILLS": ui_text(locale, "技术技能", "Technical skills"),
            "WORK EXPERIENCE": ui_text(locale, "工作经历", "Work experience"),
        }
        detected_sections = "".join(
            f'<li>✓ {_escape(section_labels.get(item, item))}</li>'
            for item in template_receipt.detected_sections
        )
        template_preview = f"""<section class="template-preview success-state">
          <span class="status-badge">{_escape(ui_text(locale, '模板已识别', 'Template detected'))}</span>
          <h3>{_escape(ui_text(locale, '请确认结构后再生成', 'Confirm the structure before generating'))}</h3>
          <ul>{detected_sections}</ul>
          <p>{_escape(ui_text(locale, '模板只控制栏目顺序；其中的姓名、公司、数字和正文不会成为你的候选事实。', 'The template controls section order only. Names, companies, metrics, and body copy from it never become candidate facts.'))}</p>
        </section>"""
    else:
        template_preview = ""
    template_error_html = (
        f'<p class="template-error" role="alert">{_escape(template_error)}</p>'
        if template_error
        else ""
    )
    language_value = form.get("resume_output_language", "en-US")
    if language_value not in {"en-US", "zh-CN", "both"}:
        language_value = "en-US"
    job_panel = _resume_job_panel(resume_job, locale) if resume_job is not None else ""
    applications_section = _applications_section_html(
        resume_library_root, locale=locale
    )
    body = f"""{work_summary}{applications_section}{job_panel}<div class="{workspace_class}">
      <section class="input-card">
        <span class="result-kicker">{_escape(ui_text(locale, '输入', 'Input'))}</span>
        <h2>{_escape(ui_text(locale, '简历 + Job Description', 'Resume + Job Description'))}</h2>
        <p>{_escape(ui_text(locale, 'AI 只能排序、压缩和改写你批准的事实，不能新增经历；生成后请做最后一次人工检查。', 'AI may rank, compress, and rewrite only the facts you approve. It cannot add experience, and you complete the final review.'))}</p>
        <details class="template-intake" {'open' if template_receipt is not None or template_error else ''}>
          <summary>{_escape(ui_text(locale, '更换简历结构模板（可选）', 'Change layout template (optional)'))}</summary>
          <p>{_escape(ui_text(locale, '先读取模板，再确认使用。支持 DOCX、HTML/HTM、TXT/MD；PDF 仅在结构可可靠识别时接受。', 'Read and confirm a template first. DOCX, HTML/HTM, TXT/MD are supported; PDF is accepted only when its structure is reliable.'))}</p>
          {template_error_html}
          <form method="post" action="/resume/template-preview" enctype="multipart/form-data">
            <input type="hidden" name="ui_locale" value="{locale}" />
            <label>{_escape(ui_text(locale, '上传模板文件', 'Upload template file'))}
              {_localized_file_input(name="layout_template_file", accept=".docx,.html,.htm,.pdf,.txt,.md", required=False, locale=locale, button_label=ui_text(locale, "选择文件", "Choose file"), placeholder=ui_text(locale, "未选择文件", "No file selected"))}
            </label>
            <label>{_escape(ui_text(locale, '或模板网页 URL', 'Or template webpage URL'))}
              <input type="url" name="layout_template_url" placeholder="https://example.com/resume-template" />
            </label>
            <label>{_escape(ui_text(locale, '或粘贴 HTML', 'Or paste HTML'))}
              <textarea name="layout_template_html" maxlength="1048576" placeholder="&lt;html&gt;…&lt;/html&gt;"></textarea>
            </label>
            <button class="secondary-button" type="submit">{_escape(ui_text(locale, '读取并预览模板', 'Read and preview template'))}</button>
          </form>
          {template_preview}
        </details>
        <form id="resume-form" method="post" action="/generate" enctype="multipart/form-data">
          <input type="hidden" name="ui_locale" value="{locale}" />
          <input type="hidden" name="provider_model" value="{provider_model}" />
          <input type="hidden" name="resume_template_preview_id" value="{_escape(template_receipt.preview_id if template_receipt is not None else '')}" />
          <label>{_escape(ui_text(locale, '输出语言', 'Output language'))}
            <select name="resume_output_language">
              <option value="en-US" {'selected' if language_value == 'en-US' else ''}>English (en-US)</option>
              <option value="zh-CN" {'selected' if language_value == 'zh-CN' else ''}>中文 (zh-CN)</option>
              <option value="both" {'selected' if language_value == 'both' else ''}>{_escape(ui_text(locale, '英文 + 中文', 'English + Chinese'))}</option>
            </select>
            <span class="hint">{_escape(ui_text(locale, '双语会分别完成两次语言编辑；两版共享同一套 FACT IDs 和事实边界。', 'Both performs two locale-specific editorial passes. The variants share the same FACT IDs and truth boundary.'))}</span>
          </label>
          {f'<label><input type="checkbox" name="approve_layout_template" value="yes" required />{_escape(ui_text(locale, "我已预览并确认使用这个结构模板；模板正文不会作为我的经历。", "I reviewed and approve this structure template. Its body copy is not my experience."))}</label>' if template_receipt is not None else ''}
          <label>{_escape(ui_text(locale, '现有简历', 'Current resume'))}
            <span class="hint">{_escape(ui_text(locale, '支持 PDF、DOCX、TXT、MD；每个文件最大 5 MB。DOCX 会保留原版式，其他格式会生成简洁 Word 版。', 'PDF, DOCX, TXT, and MD are supported, up to 5 MB each. DOCX keeps its layout; other formats produce a clean Word version.'))}</span>
            {_localized_file_input(name="resume_template", accept=".pdf,.docx,.txt,.md", required=True, locale=locale, button_label=ui_text(locale, "选择文件", "Choose file"), placeholder=ui_text(locale, "未选择文件", "No file selected"))}
          </label>
          <label>{_escape(ui_text(locale, '职位描述（JD）', 'Job Description'))}
            <span class="hint">{_escape(ui_text(locale, '粘贴完整 JD，或在下方上传一个文件；两者选一个。', 'Paste the complete JD, or upload one file below. Choose one method.'))}</span>
            <textarea
              name="job_description"
              placeholder="{_escape(ui_text(locale, '在此粘贴完整职位描述…', 'Paste the full job description here…'))}"
            >{job_description}</textarea>
          </label>
          <label>{_escape(ui_text(locale, '或者上传 JD（可选）', 'Or upload the JD (optional)'))}
            {_localized_file_input(name="job_description_file", accept=".pdf,.docx,.txt,.md", required=False, locale=locale, button_label=ui_text(locale, "选择文件", "Choose file"), placeholder=ui_text(locale, "未选择文件", "No file selected"))}
          </label>
          <label>{_escape(ui_text(locale, '补充材料（可选，最多一份）', 'Supporting document (optional, one file)'))}
            <span class="hint">{_escape(ui_text(locale, '只提取与本次简历相关的摘要；不会上传原始文件、文件名或本地路径。', 'Only a task-relevant summary is prepared. The raw file, filename, and local path never enter the gateway payload.'))}</span>
            {_localized_file_input(name="support_document", accept=".pdf,.docx,.txt,.md", required=False, locale=locale, button_label=ui_text(locale, "选择文件", "Choose file"), placeholder=ui_text(locale, "未选择文件", "No file selected"))}
          </label>
          <label>{_escape(ui_text(locale, '针对性说明（可选）', 'Tailoring instructions (optional)'))}
            <span class="hint">{_escape(ui_text(locale, '例如：突出 RAG、后端工程和产品交付。说明只影响已有内容的排序，不会新增经历。', 'For example: prioritize RAG, backend engineering, and product delivery. Instructions only affect ordering and never add experience.'))}</span>
            <textarea name="tailoring_instructions" maxlength="1200"
              placeholder="{_escape(ui_text(locale, '优先突出相关的已有项目与技能…', 'Prioritize relevant existing projects and skills…'))}"
            >{tailoring_instructions}</textarea>
          </label>
          <div class="metadata">
            <label>{_escape(ui_text(locale, '公司（可选）', 'Company (optional)'))}<input name="company_name" value="{company_name}" /></label>
            <label>{_escape(ui_text(locale, '岗位（可选）', 'Role (optional)'))}<input name="job_title" value="{job_title}" /></label>
            <label>{_escape(ui_text(locale, 'Job URL（可选）', 'Job URL (optional)'))}
              <input type="url" name="company_url" value="{company_url}" />
            </label>
            <label>{_escape(ui_text(locale, 'Job ID（可选）', 'Job ID (optional)'))}<input name="job_id" value="{job_id}" /></label>
          </div>
          <input
            type="hidden" name="resume_library_root"
            value="{_escape(str(resume_library_root))}"
          />
          <div class="save-note">
            {_escape(ui_text(locale, '你选择的原始文件只在本次请求中处理，不会长期保存。SoloScale 仅私密保留生成结果与无正文回执，供你预览和下载。', 'Your selected source files are processed only for this request and are not retained long term. SoloScale privately keeps only the generated result and a body-free receipt for preview and download.'))}
          </div>
          <div class="provider-summary">
            <span>{_escape(ui_text(locale, '本次生成方式', 'Generation mode'))}</span>
            <strong>{_escape(provider_label)}</strong>
            <a href="{ui_url('/settings/ai', locale)}">{_escape(ui_text(locale, '在设置中更换 AI 服务', 'Change AI service in Settings'))}</a>
          </div>
          <fieldset class="expert-review-choice">
            <legend>{_escape(ui_text(locale, '最终审阅', 'Final review'))}</legend>
            <label><input type="radio" name="expert_review_mode" value="local" checked />
              <span><strong>{_escape(ui_text(locale, '本地事实校验', 'Local fact verification'))}</strong><small>{_escape(ui_text(locale, '默认，不产生额外 API 费用。', 'Default; no additional API cost.'))}</small></span>
            </label>
            <label><input id="expert-review-sol" type="radio" name="expert_review_mode" value="openai_sol" {'disabled' if not expert_review_configured else ''} />
              <span><strong>GPT-5.6 Sol Expert Review</strong><small>{_escape(ui_text(locale, '只发送 JD 信号、支持片段和当前草稿；返回 patch 后会在本地再次核验。', 'Sends only JD signals, supporting fragments, and the current draft. Returned patches are verified locally again.'))}</small></span>
            </label>
            <label id="expert-review-approval" class="expert-review-approval"><input type="checkbox" name="approve_expert_review" value="yes" />
              {_escape(ui_text(locale, '我批准本次使用我的 OpenAI API 账户执行一次专家审阅。', 'I approve one expert-review request using my OpenAI API account.'))}
            </label>
            {'' if expert_review_configured else f'<p class="hint">{_escape(ui_text(locale, "先在设置中配置 OpenAI，才能启用可选专家审阅。", "Configure OpenAI in Settings to enable optional expert review."))}</p>'}
          </fieldset>
          <label><input type="checkbox" name="approve_resume_processing" value="yes" required />
            {_escape(ui_text(locale, '我确认简历事实真实，并授权本次处理我主动选择的简历、JD 和可选支持文件。AI 只接收已清洗的简历事实、完整 JD、支持摘要和模板结构；姓名、联系方式、文件名、本地路径、ChatGPT/Codex 对话与项目文件不会发送。', 'I confirm the resume facts are truthful and authorize this task to process only the resume, JD, and optional support file I selected. AI receives only sanitized resume facts, the full JD, a support summary, and allowlisted template structure. Names, contact details, filenames, local paths, ChatGPT/Codex histories, and project files are not sent.'))}
          </label>
          <p class="privacy-note">{_escape(ui_text(locale, 'SoloScale 会使用当前默认 AI 服务；若该服务不可用，本次生成会明确停止，不会静默改用其他服务或通用模板。', 'SoloScale uses the current default AI service. If it is unavailable, this run stops clearly instead of silently switching services or returning a generic template.'))}</p>
          <div id="progress" role="status" aria-live="polite">{_escape(ui_text(locale, '正在读取模板并核对 JD…', 'Reading the template and checking the JD…'))}</div>
          <div class="generate-actions">
            <button id="generate-button" class="primary-button" type="submit" name="generation_mode" value="{_escape(generation_mode)}">{_escape(ui_text(locale, '使用当前 AI 服务生成', 'Generate with the selected AI service') if generation_mode != 'template' else ui_text(locale, '生成安全离线草稿', 'Generate safe offline draft'))}</button>
            {'' if generation_mode == 'template' else f'<button class="secondary-button" type="submit" name="generation_mode" value="template">{_escape(ui_text(locale, "明确改用安全离线草稿", "Explicitly use a safe offline draft"))}</button>'}
          </div>
        </form>
      </section>
      {_user_result_card(action_result, locale=locale, resume_job=resume_job)}
    </div>"""
    poll_script = (
        "window.setTimeout(()=>window.location.reload(),1000);"
        if resume_job is not None and resume_job.phase not in {"COMPLETE", "FAILED"}
        else ""
    )
    script = f"""
    const resumeForm=document.getElementById('resume-form');
    const expertReview=document.getElementById('expert-review-sol');
    const expertApproval=document.querySelector('input[name="approve_expert_review"]');
    const syncExpertApproval=()=>{{
      const selected=expertReview&&expertReview.checked;
      if(expertApproval) expertApproval.required=Boolean(selected);
    }};
    document.querySelectorAll('input[name="expert_review_mode"]').forEach((item)=>item.addEventListener('change',syncExpertApproval));
    syncExpertApproval();
    document.querySelectorAll('input.file-native[type="file"]').forEach((input)=>{{
      const name=document.querySelector(`[data-file-name-for="${{input.name}}"]`);
      const update=()=>{{ if(name) name.textContent = input.files && input.files.length ? Array.from(input.files).map(f=>f.name).join(', ') : (input.dataset.filePlaceholder || ''); }};
      input.addEventListener('change',update);
      update();
    }});
    if(resumeForm) resumeForm.addEventListener('submit',()=>{{
      const progress=document.getElementById('progress');
      const button=document.getElementById('generate-button');
      if(progress) progress.classList.add('visible');
      if(button) button.disabled=true;
      if(button) button.textContent={json.dumps(ui_text(locale, '正在生成…', 'Generating…'))};
      window.setTimeout(()=>{{if(progress) progress.textContent={json.dumps(ui_text(locale, '任务正在后台运行，即将显示实时进度…', 'The job is running in the background. Live progress will appear shortly…'))};}},450);
    }});
    {poll_script}
    """
    return render_app_shell(
        active="resume",
        locale=locale,
        current_url="/resume",
        title=f"SoloScale · {ui_text(locale, '针对性简历', 'Tailored Resume')}",
        eyebrow=ui_text(locale, "简历工作台", "Resume workspace"),
        heading=ui_text(locale, "把真实经历，变成适合这份工作的简历。", "Turn your real experience into a resume for this role."),
        description=ui_text(locale, "上传现有 Word 模板并粘贴完整 JD。SoloScale 会按岗位信号重新排序和改写已批准的事实，并把缺口留给你确认。", "Upload your current Word template and paste the complete JD. SoloScale reprioritizes and rewrites approved facts for the role, while leaving unsupported gaps visible for review."),
        body=body,
        script=script,
        extra_css="""
.use-my-work{display:grid;grid-template-columns:minmax(260px,.75fr) 1fr auto;gap:16px;align-items:center;margin-bottom:18px;padding:15px 18px;border:1px solid #d9e2dc;border-radius:15px;background:linear-gradient(110deg,#fff,#f1f8f5)}.use-my-work>div{display:grid;gap:3px}.use-my-work strong{font-size:13px}.use-my-work p{margin:0;color:var(--text-muted);font-size:13px}.use-my-work a{font-weight:800;text-decoration:none;white-space:nowrap}
.resume-job{display:grid;gap:15px;margin-bottom:18px;padding:20px;border:1px solid #d9e2dc;border-radius:18px;background:linear-gradient(135deg,#fff,#eef8f4);box-shadow:var(--shadow-card)}.resume-job-header{display:flex;justify-content:space-between;gap:18px;align-items:flex-start}.resume-job-header h2{margin:5px 0 5px}.resume-job-header p{margin:0;color:var(--text-muted)}.resume-job>progress{width:100%;height:12px;accent-color:var(--success)}.resume-job-steps{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:7px;padding:0;margin:0;list-style:none}.resume-job-steps li{display:grid;gap:7px;color:var(--text-muted);font-size:11px;font-weight:700}.resume-job-steps li>span{height:7px;border-radius:999px;background:#dfe5e2}.resume-job-steps li.done>span{background:var(--success)}.resume-job-steps li.current>span{background:var(--brand)}.resume-job-steps li.failed>span{background:var(--danger)}.job-timings{font-size:12px}.job-timings summary{cursor:pointer;font-weight:800}.job-timings ul{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;margin:10px 0 0;padding:0;list-style:none}.job-timings li{display:flex;justify-content:space-between;gap:8px;padding:8px;border-radius:9px;background:rgb(255 255 255 / 75%)}.resume-job-actions{display:flex;justify-content:space-between;gap:12px;color:var(--text-muted);font-size:12px}.resume-job-actions a{font-weight:850;text-decoration:none}
.provider-summary{display:grid;gap:4px;padding:14px;border:1px solid var(--border);border-radius:14px;background:linear-gradient(135deg,var(--brand-soft),var(--success-soft))}.provider-summary span{font-size:12px;color:var(--text-muted)}.provider-summary a{font-size:12px;font-weight:800;justify-self:start}.generate-actions{display:grid;gap:9px}.secondary-button{background:var(--surface-subtle);color:var(--brand);border:1px solid var(--border)}
.expert-review-choice{display:grid;gap:10px;padding:15px;border:1px solid var(--border);border-radius:14px}.expert-review-choice legend{padding:0 6px;font-weight:850}.expert-review-choice>label{display:flex;align-items:flex-start;gap:9px}.expert-review-choice span{display:grid;gap:2px}.expert-review-choice small{color:var(--text-muted);font-weight:500}.expert-review-approval{padding:10px;border-radius:10px;background:var(--brand-soft);font-size:12px}
.workspace{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:22px}.workspace.has-result{grid-template-columns:minmax(320px,.72fr) minmax(0,1.28fr)}
.input-card h2,.result-card h2{margin:7px 0 8px;font-size:25px;letter-spacing:-.025em}.input-card>p,.result-card p{color:var(--text-muted)}
.input-card form{gap:18px;margin-top:24px}.input-card textarea{min-height:220px}.input-card input[type=file]{padding:18px;border-style:dashed;background:#fafbff}
.template-intake{margin:18px 0;padding:14px 16px;border:1px solid var(--border);border-radius:14px;background:var(--surface-subtle)}.template-intake>summary{cursor:pointer;font-weight:850}.template-intake>p{margin:10px 0 0;color:var(--text-muted);font-size:12px}.template-intake form{margin-top:14px}.template-intake textarea{min-height:120px}.template-preview{margin-top:14px;padding:14px;border:1px solid #badbcd;border-radius:12px;background:var(--success-soft)}.template-preview h3{margin:7px 0}.template-preview ul{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0;padding:0;list-style:none}.template-preview li{padding:5px 8px;border-radius:999px;background:#fff;font-size:11px;font-weight:750}.template-error{color:var(--danger);font-weight:750}.resume-variant-card{display:flex;align-items:center;gap:9px;margin-top:10px;padding:10px;border:1px solid var(--border);border-radius:12px;background:var(--surface-subtle)}.resume-variant-card strong{margin-right:auto}
.metadata{display:grid;grid-template-columns:1fr 1fr;gap:12px}#progress{display:none;padding:14px;border-radius:13px;background:var(--brand-soft);color:var(--brand)}#progress.visible{display:block}
.error-state{border-color:#efc7c4}.result-header{display:flex;justify-content:space-between;gap:18px;align-items:flex-start}.download{flex:0 0 auto;margin-top:4px}
.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:22px 0}.metrics div{padding:12px;border:1px solid var(--border);border-radius:13px;text-align:center}.metrics strong{display:block;font-size:24px}.metrics span{display:block;color:var(--text-muted);font-size:11px;margin-top:3px}
.result-grid{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(180px,.55fr);gap:18px}.preview-heading{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:10px}.preview-heading h3{margin:0}.preview-link{font-size:12px;font-weight:700;text-decoration:none}
.resume-pdf-shell{height:680px;overflow:hidden;border:1px solid var(--border);border-radius:14px;background:#dfe3ea}.resume-pdf-preview{width:100%;height:100%;border:0;background:white}.preview-note{margin:9px 0 0;font-size:12px}.resume-preview{max-height:450px;overflow:auto}.gap-list{padding-left:20px;color:var(--text-muted);font-size:13px}.result-card details{margin-top:18px;border-top:1px solid var(--border);padding-top:15px;font-size:13px}.result-card summary{cursor:pointer;font-weight:700}code{word-break:break-all}
.resume-provenance>p{color:var(--text-muted)}.provenance-summary{display:flex;align-items:baseline;gap:7px;margin:12px 0}.provenance-summary strong{font-size:26px;color:var(--success)}.provenance-claims{display:grid;gap:9px}.provenance-claim{display:flex;justify-content:space-between;gap:14px;align-items:flex-start;padding:12px;border:1px solid var(--border);border-radius:12px;background:#f8fbf9}.provenance-claim strong{font-size:13px;line-height:1.45}.provenance-claim p{margin:6px 0 0;color:var(--text-muted);font-size:11px}.provenance-claim .status-badge{flex:none;padding:5px 8px;border-radius:999px;background:#e5f5ed;color:#166044;font-size:10px;font-weight:800}
.applications-overview{margin-bottom:20px;padding:20px;border:1px solid var(--border);border-radius:18px;background:linear-gradient(145deg,#fff,var(--brand-soft))}.applications-overview h2{margin:7px 0}.applications-overview p{color:var(--text-muted)}.application-list{display:grid;gap:12px;margin-top:16px}.application-card{display:grid;grid-template-columns:1fr auto;gap:18px;align-items:start;padding:16px;border:1px solid var(--border);border-radius:15px;background:#fff}.application-card h3{margin:7px 0 5px}.application-card p{margin:0;color:var(--text-muted);font-size:12px}.application-truth{display:grid;gap:4px;margin-top:10px;font-size:12px;color:var(--text-muted)}.application-truth strong{color:var(--text)}.application-actions{display:grid;gap:10px;align-items:start}.application-actions form{display:grid;grid-template-columns:auto 1fr 1fr auto;gap:7px}.application-actions input,.application-actions select{min-width:0}.application-actions button{white-space:nowrap}
.file-control{position:relative;display:inline-flex;align-items:center;gap:10px;min-width:220px;padding:10px 12px;border:1px dashed var(--border);border-radius:12px;background:#fafbff}.file-native{position:absolute;inset:0;opacity:0;width:100%;height:100%;cursor:pointer;z-index:2}.file-button{padding:7px 11px;border-radius:9px;background:var(--brand);color:#fff;font-weight:800;font-size:12px;pointer-events:none}.file-name{color:var(--text-muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:280px}.file-control:focus-within{outline:3px solid var(--focus);outline-offset:2px}
@media(max-width:900px){.use-my-work,.workspace,.workspace.has-result,.result-grid,.application-card{grid-template-columns:1fr}.resume-job-steps{grid-template-columns:repeat(3,1fr)}.job-timings ul{grid-template-columns:repeat(2,1fr)}.application-actions form{grid-template-columns:1fr}}@media(max-width:560px){.metadata,.metrics{grid-template-columns:1fr 1fr}.result-header,.resume-job-header,.resume-job-actions{display:block}.download{display:block;margin-top:16px}.resume-pdf-shell{height:560px}.resume-job-steps,.job-timings ul{grid-template-columns:1fr 1fr}}
""",
    )


def _latest_learning_run(data_root: Path) -> Path | None:
    runs_root = data_root / "learning-runs"
    if not runs_root.is_dir() or runs_root.is_symlink():
        return None
    candidates = sorted(
        (
            path
            for path in runs_root.iterdir()
            if path.name.startswith("learning-")
            and path.is_dir()
            and not path.is_symlink()
            and (path / "run.json").is_file()
        ),
        key=lambda path: path.name,
    )
    return candidates[-1] if candidates else None


def _learning_graph(run_dir: Path, locale: UILocale) -> str:
    graph = _load_json_file(run_dir / "04_evidence_graph.json")
    if graph is None:
        return f'<p class="error">{_escape(ui_text(locale, "证据图暂不可用。", "Graph artifact is unavailable."))}</p>'
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return f'<p class="error">{_escape(ui_text(locale, "证据图格式无效。", "Graph artifact has an invalid shape."))}</p>'
    node_json = json.dumps(nodes, ensure_ascii=True).replace("</", "<\\/")
    edge_json = json.dumps(edges, ensure_ascii=True).replace("</", "<\\/")
    run_id = json.dumps(run_dir.name)
    locale_json = json.dumps(locale)
    kind_labels = json.dumps(
        {
            "CONCEPT": ui_text(locale, "概念", "Concept"),
            "CODE": ui_text(locale, "代码", "Code"),
            "TEST": ui_text(locale, "测试", "Test"),
            "DECISION": ui_text(locale, "决策", "Decision"),
        },
        ensure_ascii=False,
    )
    truth_labels = json.dumps(
        {
            value: ui_display_value(locale, value)
            for value in ("UNKNOWN", "ENGINEERING_VERIFIED", "VERIFIED_EVIDENCE", "APPROVED_CLAIM")
        },
        ensure_ascii=False,
    )
    return f"""<section class="panel graph-panel">
  <h2>{_escape(ui_text(locale, "交互式证据图", "Interactive evidence graph"))}</h2>
  <p class="muted">{_escape(ui_text(locale, "单击节点查看关联证据，双击聚焦。", "Click a node for its evidence neighborhood. Double-click to focus it."))}</p>
  <div class="graph-scroll"><svg id="learning-graph" role="img"></svg></div>
  <div class="detail-grid">
    <pre id="learning-node-detail">{_escape(ui_text(locale, "请选择概念、代码、测试或决策节点。", "Select a Concept, Code, Test, or Decision node."))}</pre>
    <p><a id="learning-source-link" class="button-link hidden" target="_blank"
      rel="noopener">{_escape(ui_text(locale, "打开有依据的本地源码", "Open grounded local source"))}</a></p>
  </div>
  <script>
  (function(){{
    const nodes={node_json};
    const edges={edge_json};
    const runId={run_id};
    const locale={locale_json};
    const kindLabels={kind_labels};
    const truthLabels={truth_labels};
    const svg=document.getElementById('learning-graph');
    const detail=document.getElementById('learning-node-detail');
    const sourceLink=document.getElementById('learning-source-link');
    const columns=5;
    const width=1060;
    const rowHeight=128;
    const height=Math.max(450,rowHeight*Math.ceil(nodes.length/columns)+70);
    svg.setAttribute('viewBox',`0 0 ${{width}} ${{height}}`);
    svg.setAttribute('height',height);
    const byId=Object.fromEntries(nodes.map((node,index)=>[
      node.id,
      {{...node,x:105+(index%columns)*210,y:65+Math.floor(index/columns)*rowHeight}},
    ]));
    let focused=null;
    function related(nodeId){{
      const ids=new Set();
      edges.forEach(edge=>{{
        if(edge.source===nodeId)ids.add(edge.target);
        if(edge.target===nodeId)ids.add(edge.source);
      }});
      return [...ids].map(id=>byId[id]).filter(Boolean).map(node=>({{
        id:node.id,kind:node.kind,label:node.label,
      }}));
    }}
    function traceability(nodeId){{
      const visited=new Set([nodeId]);
      const pending=[nodeId];
      while(pending.length){{
        const current=pending.shift();
        edges.forEach(edge=>{{
          let candidate=null;
          if(edge.source===current)candidate=edge.target;
          if(edge.target===current)candidate=edge.source;
          if(candidate&&!visited.has(candidate)){{visited.add(candidate);pending.push(candidate);}}
        }});
      }}
      visited.delete(nodeId);
      return [...visited].map(id=>byId[id]).filter(Boolean).map(node=>({{
        id:node.id,kind:node.kind,label:node.label,
      }}));
    }}
    function inspect(node){{
      detail.textContent=JSON.stringify({{
        id:node.id,
        kind:kindLabels[node.kind]||node.kind,
        label:node.label,
        truth_stage:truthLabels[node.truth_stage]||node.truth_stage,
        detail:node.detail,
        related:related(node.id),
        traceability:traceability(node.id),
      }},null,2);
      if(node.kind==='CODE'||node.kind==='TEST'){{
        sourceLink.href='/learning/source?run_id='+encodeURIComponent(runId)
          +'&anchor_id='+encodeURIComponent(node.id)+'&lang='+encodeURIComponent(locale);
        sourceLink.classList.remove('hidden');
      }}else{{
        sourceLink.classList.add('hidden');
      }}
    }}
    function draw(){{
      svg.innerHTML='';
      edges.forEach(edge=>{{
        const source=byId[edge.source];
        const target=byId[edge.target];
        if(!source||!target)return;
        if(focused&&source.id!==focused&&target.id!==focused)return;
        const line=document.createElementNS('http://www.w3.org/2000/svg','line');
        line.setAttribute('x1',source.x);
        line.setAttribute('y1',source.y);
        line.setAttribute('x2',target.x);
        line.setAttribute('y2',target.y);
        line.setAttribute('stroke','#64748b');
        line.setAttribute('stroke-width','1.5');
        svg.append(line);
      }});
      Object.values(byId).forEach(node=>{{
        if(focused&&node.id!==focused&&!related(focused).some(item=>item.id===node.id))return;
        const group=document.createElementNS('http://www.w3.org/2000/svg','g');
        const box=document.createElementNS('http://www.w3.org/2000/svg','rect');
        const kind=document.createElementNS('http://www.w3.org/2000/svg','text');
        const label=document.createElementNS('http://www.w3.org/2000/svg','text');
        box.setAttribute('x',node.x-84);
        box.setAttribute('y',node.y-34);
        box.setAttribute('width','168');
        box.setAttribute('height','68');
        box.setAttribute('rx','12');
        box.setAttribute('fill',node.kind==='CONCEPT'?'#0f766e':'#1e3a8a');
        box.setAttribute('stroke',node.id===focused?'#fbbf24':'#60a5fa');
        kind.setAttribute('x',node.x);
        kind.setAttribute('y',node.y-8);
        kind.setAttribute('text-anchor','middle');
        kind.setAttribute('fill','#bfdbfe');
        kind.setAttribute('font-size','10');
        kind.textContent=kindLabels[node.kind]||node.kind;
        label.setAttribute('x',node.x);
        label.setAttribute('y',node.y+12);
        label.setAttribute('text-anchor','middle');
        label.setAttribute('fill','white');
        label.setAttribute('font-size','10');
        label.textContent=(node.label||'').slice(0,25);
        group.append(box,kind,label);
        group.style.cursor='pointer';
        group.onclick=()=>inspect(node);
        group.ondblclick=()=>{{focused=focused===node.id?null:node.id;draw();inspect(node);}};
        svg.append(group);
      }});
    }}
    draw();
  }})();
  </script>
</section>"""


def _learning_source_excerpt(
    data_root: Path,
    repo_root: Path,
    run_id: str,
    anchor_id: str,
) -> tuple[str, str]:
    if re.fullmatch(r"learning-[A-Za-z0-9T-]+", run_id) is None:
        raise ValueError("invalid learning run id")
    if re.fullmatch(r"[A-Z0-9-]+", anchor_id) is None:
        raise ValueError("invalid learning anchor id")
    runs_root = data_root / "learning-runs"
    run_dir = runs_root / run_id
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise ValueError("learning run is unavailable")
    anchors_path = run_dir / "03_code_anchors.json"
    if anchors_path.is_symlink():
        raise ValueError("learning anchors are unavailable")
    anchors = _load_json_file(anchors_path)
    if anchors is None:
        raise ValueError("learning anchors are unavailable")
    candidates: list[object] = []
    for key in ("code_anchors", "verification_anchors"):
        value = anchors.get(key, [])
        if isinstance(value, list):
            candidates.extend(value)
    selected = next(
        (item for item in candidates if isinstance(item, dict) and item.get("id") == anchor_id),
        None,
    )
    if not isinstance(selected, dict):
        raise ValueError("learning anchor is unavailable")
    relative_file = selected.get("file")
    line_start = selected.get("line_start")
    line_end = selected.get("line_end")
    if not isinstance(relative_file, str) or not isinstance(line_start, int):
        raise ValueError("learning anchor is invalid")
    if not isinstance(line_end, int) or line_start < 1 or line_end < line_start:
        raise ValueError("learning anchor is invalid")
    if line_end - line_start > 200:
        raise ValueError("learning anchor exceeds the local excerpt limit")
    resolved_root = repo_root.resolve()
    resolved_file = (resolved_root / relative_file).resolve()
    try:
        resolved_file.relative_to(resolved_root)
    except ValueError:
        raise ValueError("learning anchor escapes the repository") from None
    if not resolved_file.is_file() or resolved_file.is_symlink():
        raise ValueError("learning source file is unavailable")
    lines = resolved_file.read_text(encoding="utf-8").splitlines()
    excerpt_start = max(1, line_start - 3)
    excerpt_end = min(len(lines), line_end + 3)
    numbered = "\n".join(
        f"{line_number:>5}  {lines[line_number - 1]}"
        for line_number in range(excerpt_start, excerpt_end + 1)
    )
    title = f"{relative_file}:{line_start}-{line_end}"
    return title, numbered


def _learning_list(value: object, locale: UILocale) -> str:
    if not isinstance(value, list):
        return f"<li>{_escape(ui_text(locale, '暂无内容。', 'Not available.'))}</li>"
    return "".join(f"<li>{_escape(str(item))}</li>" for item in value)


def _anchor_pack_parts(
    pack: dict[str, object],
) -> tuple[
    dict[str, str],
    list[str],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    project_value = pack.get("project")
    keywords_value = pack.get("keywords")
    reasoning_value = pack.get("reasoning")
    code_value = pack.get("code")
    tests_value = pack.get("tests")
    if not isinstance(project_value, dict):
        raise ValueError("interview anchor project is invalid")
    project = {
        key: value
        for key in ("repository", "branch", "commit")
        if isinstance((value := project_value.get(key)), str) and value
    }
    if set(project) != {"repository", "branch", "commit"}:
        raise ValueError("interview anchor project is invalid")
    if not isinstance(keywords_value, list) or not all(
        isinstance(value, str) for value in keywords_value
    ):
        raise ValueError("interview anchor keywords are invalid")

    def object_list(value: object, label: str) -> list[dict[str, object]]:
        if not isinstance(value, list) or not value:
            raise ValueError(f"interview anchor {label} is invalid")
        result: list[dict[str, object]] = []
        for item in value:
            if not isinstance(item, dict):
                raise ValueError(f"interview anchor {label} is invalid")
            result.append(dict(item))
        return result

    return (
        project,
        list(keywords_value),
        object_list(reasoning_value, "reasoning"),
        object_list(code_value, "code"),
        object_list(tests_value, "tests"),
    )


def _anchor_pack_text(bullet: str, pack: dict[str, object]) -> str:
    project, keywords, reasoning, code, tests = _anchor_pack_parts(pack)
    lines = [
        f"Selected bullet: {bullet}",
        f"Case: {pack['case_id']}",
        f"Keywords: {', '.join(keywords)}",
        (f"Project: {project['repository']} · {project['branch']} · {project['commit']}"),
        "Reasoning:",
    ]
    lines += [f"- {item['path']} · {item['sha256']}" for item in reasoning]
    lines.append("Code:")
    lines += [
        f"- {item['file']}:{item['symbol']}:{item['line_start']}-"
        f"{item['line_end']} · {item['sha256']}"
        for item in code
    ]
    lines.append("Tests:")
    lines += [
        f"- {item['file']}:{item['symbol']}:{item['line_start']}-"
        f"{item['line_end']} · {item['command']} · {item['receipt_state']}"
        for item in tests
    ]
    lines.append(
        "Truth boundary: These anchors locate real implementation and committed test "
        "definitions but do not prove authorship, mastery, semantic claim support, test "
        "execution, or resume eligibility."
    )
    return "\n".join(lines)


def _anchor_pack_html(pack: dict[str, object], locale: UILocale) -> str:
    project, keywords, reasoning, code, tests = _anchor_pack_parts(pack)

    def rows(items: list[dict[str, object]], kind: str) -> str:
        rendered: list[str] = []
        for item in items:
            if kind == "reasoning":
                locator = f"{item['path']} · sha256:{item['sha256']}"
            else:
                locator = (
                    f"{item['file']} · {item['symbol']} · "
                    f"L{item['line_start']}-{item['line_end']} · sha256:{item['sha256']}"
                )
                if kind == "tests":
                    locator += f" · {item['command']} · {item['receipt_state']}"
            rendered.append(f"<li><code>{_escape(locator)}</code></li>")
        return "".join(rendered)

    project_text = f"{project['repository']} · {project['branch']} · {project['commit']}"
    return f"""
<p><strong>{_escape(ui_text(locale, '项目', 'Project'))}</strong><br /><code>{_escape(project_text)}</code></p>
<p><strong>{_escape(ui_text(locale, '关键词', 'Keywords'))}</strong><br />{_escape(", ".join(keywords))}</p>
<details open><summary>{_escape(ui_text(locale, '推理锚点', 'Reasoning anchors'))}</summary><ul>{rows(reasoning, "reasoning")}</ul></details>
<details><summary>{_escape(ui_text(locale, '代码锚点', 'Code anchors'))}</summary><ul>{rows(code, "code")}</ul></details>
<details><summary>{_escape(ui_text(locale, '测试锚点', 'Test anchors'))}</summary><ul>{rows(tests, "tests")}</ul></details>
"""


def _active_learning_case(data_root: Path) -> LearningCase | None:
    try:
        cases = CasebookStore(data_root).list_cases()
    except (OSError, ValueError):
        return None
    return max(cases, key=lambda case: case.created_at) if cases else None


def _learning_default_requirement(data_root: Path) -> str:
    active = _active_learning_case(data_root)
    if active is not None:
        return capability_requirement(active)
    return DEFAULT_TARGET_REQUIREMENT


def _practice_panel_html(data_root: Path, form: dict[str, str], locale: UILocale) -> str:
    try:
        cases = CasebookStore(data_root).list_cases()
    except (OSError, ValueError):
        cases = []
    if not cases:
        return f"""<section class="panel practice-panel" id="practice">
  <h2>{_escape(ui_text(locale, '编码练习（VS Code）', 'Coding practice (VS Code)'))}</h2>
  <p class="muted">{_escape(ui_text(locale, '先在下方生产就绪地图中创建学习案例，无需命令行；之后即可把案例缺口变成可练习、可回填掌握度的编码任务。', 'Create a Learning case from the Production Readiness Map below (no command line required); then turn the case gap into a bounded, testable practice task.'))}</p>
</section>"""
    latest = max(cases, key=lambda case: case.created_at)
    exercises = [
        item for item in list_exercises(data_root) if item.case_id == latest.id
    ]
    target_requirement = _escape(
        form.get("target_requirement", _learning_default_requirement(data_root))
    )
    type_options = "".join(
        f'<option value="{item.value}" {"selected" if item is ExerciseType.IMPLEMENT else ""}>{item.value}</option>'
        for item in ExerciseType
    )
    language_options = "".join(
        f'<option value="{item.value}" {"selected" if item is PracticeLanguage.PYTHON else ""}>{item.value}</option>'
        for item in PracticeLanguage
    )
    exercise_rows = "".join(
        _render_practice_exercise_row(item, locale) for item in exercises
    )
    return f"""<section class="panel practice-panel" id="practice">
  <h2>{_escape(ui_text(locale, '编码练习（VS Code）', 'Coding practice (VS Code)'))}</h2>
  <p class="muted">{_escape(ui_text(locale, '把案例缺口变成有边界、可测试、可回填掌握度的练习任务。', 'Turn the case gap into a bounded, testable task that feeds back into mastery.'))}</p>
  <form class="practice-create" method="post" action="/learning/practice/create">
    <input type="hidden" name="ui_locale" value="{locale}" />
    <input type="hidden" name="case_id" value="{_escape(latest.id)}" />
    <label>{_escape(ui_text(locale, 'JD 要求', 'JD requirement'))}
      <input name="target_requirement" value="{target_requirement}" required />
    </label>
    <label>{_escape(ui_text(locale, '类型', 'Type'))}<select name="exercise_type">{type_options}</select></label>
    <label>{_escape(ui_text(locale, '语言', 'Language'))}<select name="practice_language">{language_options}</select></label>
    <label>{_escape(ui_text(locale, '难度', 'Difficulty'))}<select name="difficulty">
      <option value="1">1</option><option value="2" selected>2</option><option value="3">3</option>
      <option value="4">4</option><option value="5">5</option>
    </select></label>
    <button type="submit">{_escape(ui_text(locale, '生成练习工作区', 'Create practice workspace'))}</button>
  </form>
  <div class="practice-list">{exercise_rows}</div>
</section>"""


def _production_readiness_map_html(
    data_root: Path, repo_root: Path | None, form: dict[str, str], locale: UILocale
) -> str:
    """Surface the capability-domain x maturity map without inventing mastery."""

    try:
        cases = CasebookStore(data_root).list_cases()
    except (OSError, ValueError):
        cases = []
    latest = max(cases, key=lambda case: case.created_at) if cases else None
    has_case = latest is not None
    exercises = (
        [item for item in list_exercises(data_root) if item.case_id == latest.id]
        if latest is not None
        else []
    )
    has_project = repo_root is not None
    has_ci = bool(
        repo_root is not None
        and (repo_root / ".github/workflows/ci.yml").is_file()
        and not (repo_root / ".github/workflows/ci.yml").is_symlink()
    )
    domains = (
        "AI Application",
        "Software Engineering",
        "Cloud / DevOps",
        "Observability / Evaluation",
        "System Design",
        "Security / Enterprise",
    )
    maturities = (
        "UNDERSTAND",
        "BUILD",
        "TEST",
        "DEPLOY",
        "OBSERVE",
        "RECOVER",
        "SECURE",
        "EXPLAIN / DEFEND",
    )
    domain_rows = "".join(
        f'''<div class="readiness-domain"><strong>{_escape(domain)}</strong><div class="maturity-row">{"".join(f'<span class="maturity-chip {"ready" if has_case or (dim in {"TEST", "DEPLOY"} and has_ci) else "needs"}">{_escape(dim)}</span>' for dim in maturities)}</div></div>'''
        for domain in domains
    )
    if not has_project:
        gap_type = ui_text(locale, "NO_EVIDENCE", "NO_EVIDENCE")
        gap_title = ui_text(locale, "连接一个真实项目", "Connect a real project")
        gap_note = ui_text(
            locale,
            "没有项目证据时无法生成有边界的练习；先在工作页选择本地 Git 项目。",
            "A bounded practice cannot be generated without project evidence; choose a local Git project first.",
        )
    elif not has_ci:
        gap_type = ui_text(locale, "NO_PRODUCTION_PROOF", "NO_PRODUCTION_PROOF")
        gap_title = ui_text(locale, "把验证命令固化为 GitHub Actions CI", "Automate verification with GitHub Actions")
        gap_note = ui_text(
            locale,
            "项目已在本地反复运行 pytest、ruff、mypy 与构建，但仍需把这条验证流水线作为生产证据自动化并讲清楚。",
            "The project already runs pytest, Ruff, mypy, and build locally, but this verification loop still needs to be automated and explained as production evidence.",
        )
    elif latest is None:
        gap_type = ui_text(locale, "UNDERSTANDING_GAP", "UNDERSTANDING_GAP")
        gap_title = ui_text(locale, "创建第一个 CI/CD 学习案例", "Create the first CI/CD learning case")
        gap_note = ui_text(
            locale,
            "仓库已有 CI workflow，但还没有把它沉淀为可练习、可复核的学习案例。",
            "The repository already has a CI workflow, but it has not been turned into a practiceable, reviewable learning case yet.",
        )
    elif not exercises:
        gap_type = ui_text(locale, "INTERVIEW_GAP", "INTERVIEW_GAP")
        gap_title = ui_text(locale, "生成并完成一次 CI/CD 练习", "Generate and complete one CI/CD exercise")
        gap_note = ui_text(
            locale,
            "已有案例；下一步是生成有边界的练习工作区并在 VS Code 中完成。",
            "A case exists; the next step is to generate a bounded practice workspace and complete it in VS Code.",
        )
    else:
        gap_type = ui_text(locale, "INTERVIEW_GAP", "INTERVIEW_GAP")
        gap_title = ui_text(locale, "提交证据并复核掌握", "Submit evidence and review mastery")
        gap_note = ui_text(
            locale,
            "练习工作区已生成；完成编码后提交结果，掌握等级仍需你确认，不会自动授予。",
            "A practice workspace exists; after coding, submit your result. Mastery still requires your confirmation and is never auto-granted.",
        )
    if latest is not None:
        action = f'''<form method="post" action="/learning/practice/create"><input type="hidden" name="ui_locale" value="{locale}" /><input type="hidden" name="case_id" value="{_escape(latest.id)}" /><input type="hidden" name="target_requirement" value="{_escape(form.get("target_requirement", _learning_default_requirement(data_root)))}" /><input type="hidden" name="exercise_type" value="IMPLEMENT" /><input type="hidden" name="practice_language" value="python" /><input type="hidden" name="difficulty" value="2" /><button class="primary" type="submit">{_escape(ui_text(locale, "生成练习", "Generate practice"))}</button></form>'''
    elif has_project:
        action = f'''<form method="post" action="/learning/case/create"><input type="hidden" name="ui_locale" value="{locale}" /><input type="hidden" name="target_requirement" value="{_escape(form.get("target_requirement", _learning_default_requirement(data_root)))}" /><button class="primary" type="submit">{_escape(ui_text(locale, "创建学习案例并继续", "Create learning case"))}</button></form>'''
    else:
        action = f'<a class="button-link" href="{ui_url("/work", locale)}">{_escape(ui_text(locale, "选择项目", "Choose project"))}</a>'
    return f"""<section class="panel readiness-map" id="readiness-map">
  <span class="kicker">{_escape(ui_text(locale, "生产就绪地图", "Production readiness map"))}</span>
  <h2>{_escape(ui_text(locale, "能力域 × 工程成熟度", "Capability domain x engineering maturity"))}</h2>
  <p class="muted">{_escape(ui_text(locale, "这里用定性状态标记证据，不伪造单一精确百分比；掌握证据仍需你复核。", "This map uses qualitative evidence states and never fabricates one precise mastery percentage; mastery still needs your review."))}</p>
  <div class="readiness-domains">{domain_rows}</div>
  <div class="highest-gap"><div><span class="status-badge">{_escape(gap_type)}</span><h3>{_escape(gap_title)}</h3><p>{_escape(gap_note)}</p></div>{action}</div>
</section>"""


def _render_practice_exercise_row(item: LearningExercise, locale: UILocale) -> str:
    actions: list[str] = []
    if item.status is not ExerciseStatus.COMPLETED:
        actions.append(
            f"""<form method="post" action="/learning/practice/open">
  <input type="hidden" name="exercise_id" value="{_escape(item.id)}" />
  <button type="submit">{_escape(ui_text(locale, '在 VS Code 中打开', 'Open in VS Code'))}</button>
</form>"""
        )
        actions.append(
            f"""<form class="practice-complete" method="post" action="/learning/practice/complete" enctype="multipart/form-data">
  <input type="hidden" name="exercise_id" value="{_escape(item.id)}" />
  <label>{_escape(ui_text(locale, '证据文件（完成状态由练习的验收条件决定）', 'Evidence file (completion is decided by the exercise acceptance criteria)'))}
    <input type="file" name="evidence" accept=".py,.md,.txt,.ts,.sql,.sh,.json,.log" />
  </label>
  <label>{_escape(ui_text(locale, '通过', 'Passed'))}<input type="number" name="tests_passed" value="0" min="0" /></label>
  <label>{_escape(ui_text(locale, '失败', 'Failed'))}<input type="number" name="tests_failed" value="0" min="0" /></label>
  <label>{_escape(ui_text(locale, '说明（未完成时必填）', 'Note (required when not passing)'))}
    <textarea name="note" rows="2"></textarea>
  </label>
  <button type="submit">{_escape(ui_text(locale, '提交练习结果', 'Submit practice result'))}</button>
</form>"""
        )
    return f"""<article class="practice-item">
  <h3>{_escape(item.exercise_type.value)} · <code>{_escape(item.id)}</code></h3>
  <p class="muted">{_escape(ui_text(locale, '状态', 'Status'))} <strong>{_escape(item.status.value)}</strong>
    · {_escape(ui_text(locale, '语言', 'Language'))} {_escape(item.practice_language.value)}
    · {_escape(ui_text(locale, '难度', 'Difficulty'))} {item.difficulty}/5</p>
  <p class="muted"><code>{_escape(item.workspace_path or '')}</code></p>
  <div class="practice-actions">{"".join(actions)}</div>
</article>"""


def _create_learning_case_ui(
    fields: dict[str, str], data_root: Path, repo_root: Path | None
) -> UIActionResult:
    if repo_root is None:
        return UIActionResult(
            "learning-case",
            "create learning case",
            1,
            "",
            "这个练习需要一个已连接的代码项目。请先选择项目。",
            0,
        )
    requirement = fields.get("target_requirement", "").strip()
    if not requirement:
        return UIActionResult(
            "learning-case",
            "create learning case",
            1,
            "",
            "请填写目标 JD 要求。",
            0,
        )
    evidence_candidates = (
        repo_root / ".github/workflows/ci.yml",
        repo_root / "pyproject.toml",
        repo_root / "README.md",
    )
    evidence_file = next(
        (path for path in evidence_candidates if path.is_file() and not path.is_symlink()),
        None,
    )
    if evidence_file is None:
        return UIActionResult(
            "learning-case",
            "create learning case",
            1,
            "",
            "没有找到可归档的本地证据文件（CI workflow / pyproject / README）。",
            0,
        )
    repository_name = repo_root.name
    try:
        from soloscale.learning_traceability import _repository_identity

        repository_name = _repository_identity(repo_root).name
    except Exception:
        pass
    store = CasebookStore(data_root)
    case_id = "ci-cd-automation"
    try:
        store.load_case(case_id)
    except CasebookError:
        pass
    else:
        return UIActionResult(
            "learning-case",
            "create learning case",
            0,
            "CI/CD 学习案例已存在；现在可以在下方生成练习工作区。",
            "",
            0,
        )
    try:
        store.create_case(
            title="Automate SoloScale verification with GitHub Actions",
            project=repository_name,
            problem=(
                "The repository already runs pytest, Ruff, mypy, and package build, "
                "but the verification loop still needs to be understood and owned as "
                "automated CI/CD."
            ),
            expected_behavior=(
                "A GitHub Actions workflow runs lint, type-check, tests, and package "
                "build on push and pull requests."
            ),
            actual_behavior=(
                "A workflow file exists, but the operator still needs to practice "
                "explaining, rebuilding, and defending it."
            ),
            root_cause=(
                "Verification was historically orchestrated manually; CI automation "
                "needs deliberate practice and ownership."
            ),
            resolution=(
                "Complete a bounded exercise: read the workflow, rebuild a minimal "
                "local version, run it, and record evidence."
            ),
            verification=["ruff check .", "mypy src tests", "pytest -q", "python -m build"],
            concepts=["CI/CD", "GitHub Actions", "test automation", "verification gates"],
            evidence_sources=[(EvidenceKind.CI, evidence_file)],
            case_id=case_id,
            repository=repository_name,
        )
    except (ValueError, OSError, KeyError, CasebookError) as exc:
        return UIActionResult(
            "learning-case", "create learning case", 1, "", str(exc), 0
        )
    return UIActionResult(
        "learning-case",
        "create learning case",
        0,
        f"学习案例已创建：{case_id}。现在可以在下方生成练习工作区。",
        "",
        0,
    )


def _create_practice_exercise_ui(fields: dict[str, str], data_root: Path) -> UIActionResult:
    case_id = fields.get("case_id", "").strip()
    requirement = fields.get("target_requirement", "").strip()
    if not case_id or not requirement:
        return UIActionResult(
            "learning-practice",
            "create practice workspace",
            1,
            "",
            "请选择学习案例并填写 JD 要求。",
            0,
        )
    try:
        store = CasebookStore(data_root)
        case = store.load_case(case_id)
        exercise = generate_practice_exercise(
            case=case,
            jd_requirement=requirement,
            exercise_type=ExerciseType(fields.get("exercise_type", "IMPLEMENT")),
            difficulty=int(fields.get("difficulty", "2") or "2"),
            practice_language=PracticeLanguage(fields.get("practice_language", "python")),
        )
        workspace = create_practice_workspace(exercise, data_root)
    except (ValueError, OSError, KeyError) as exc:
        return UIActionResult("learning-practice", "create practice workspace", 1, "", str(exc), 0)
    return UIActionResult(
        "learning-practice",
        "create practice workspace",
        0,
        f"练习工作区已创建（USER_PRACTICE_REQUIRED）：{workspace}",
        "",
        0,
    )


def _open_practice_exercise_ui(fields: dict[str, str], data_root: Path) -> UIActionResult:
    exercise_id = fields.get("exercise_id", "").strip()
    if not exercise_id:
        return UIActionResult("learning-practice", "open practice workspace", 1, "", "缺少练习 ID。", 0)
    try:
        exercise = load_exercise(exercise_id, data_root)
        if not exercise.workspace_path:
            raise ValueError("exercise has no practice workspace yet")
        workspace = Path(exercise.workspace_path)
        opened = open_practice_workspace(workspace)
        if opened:
            save_exercise(
                exercise.model_copy(update={"status": ExerciseStatus.OPENED}),
                data_root,
            )
    except (ValueError, OSError) as exc:
        return UIActionResult("learning-practice", "open practice workspace", 1, "", str(exc), 0)
    message = (
        f"已在 VS Code 打开：{workspace}"
        if opened
        else f"未检测到 code CLI，请手动打开：{workspace}"
    )
    return UIActionResult(
        "learning-practice",
        "open practice workspace",
        0 if opened else 1,
        message,
        "",
        0,
    )


def _complete_practice_exercise_ui(
    fields: dict[str, str],
    files: dict[str, UploadedFile],
    data_root: Path,
) -> UIActionResult:
    exercise_id = fields.get("exercise_id", "").strip()
    if not exercise_id:
        return UIActionResult("learning-practice", "complete practice", 1, "", "缺少练习 ID。", 0)
    evidence = files.get("evidence")
    try:
        exercise = load_exercise(exercise_id, data_root)
        store = CasebookStore(data_root)
        temporary: Path | None = None
        evidence_path: Path | None = None
        if evidence is not None and evidence.content:
            uploads = practice_workspace_root(data_root)
            uploads.mkdir(mode=0o700, parents=True, exist_ok=True)
            suffix = Path(evidence.filename).suffix or ".bin"
            temporary = uploads / f".upload-{uuid4().hex}{suffix}"
            temporary.write_bytes(evidence.content)
            temporary.chmod(0o600)
            evidence_path = temporary
        try:
            receipt, result = ingest_practice_completion(
                store=store,
                exercise=exercise,
                evidence_path=evidence_path,
                tests_passed=int(fields.get("tests_passed", "0") or "0"),
                tests_failed=int(fields.get("tests_failed", "0") or "0"),
                note=fields.get("note", "") or None,
            )
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    except (ValueError, OSError) as exc:
        return UIActionResult("learning-practice", "complete practice", 1, "", str(exc), 0)
    outcome = "PASS" if receipt.completed else "NEEDS_WORK"
    before = receipt.mastery_before.value if receipt.mastery_before else "L0 Seen"
    after = receipt.mastery_after.value if receipt.mastery_after else "L0 Seen"
    next_action = result.mastery.next_stage.value if result.mastery.next_stage else "COMPLETE"
    ready = "是" if receipt.interview_ready else "否"
    return UIActionResult(
        "learning-practice",
        "complete practice",
        0 if receipt.completed else 1,
        (
            f"已记录 {outcome}。掌握：{before} → {after}；"
            f"面试就绪：{ready}；下一步：{next_action}"
        ),
        "",
        0,
    )


def _learning_page(
    data_root: Path,
    repo_root: Path | None,
    form: dict[str, str],
    result: UIActionResult | None = None,
    locale: UILocale = DEFAULT_UI_LOCALE,
) -> str:
    project_binding = None
    fixture_missing_anchors: tuple[str, ...] = ()
    if repo_root is not None:
        try:
            project_binding = inspect_learning_project(repo_root)
            fixture_missing_anchors = missing_learning_case_anchors(repo_root)
        except (LearningTraceabilityError, OSError, ValueError):
            project_binding = None
    project_available = project_binding is not None
    fixture_available = project_available and not fixture_missing_anchors
    requested_run_id = form.get("run_id", "")
    if requested_run_id:
        run_dir = (
            data_root / "learning-runs" / requested_run_id
            if re.fullmatch(r"learning-\d{8}T\d{6}Z-[0-9a-f]{10}", requested_run_id)
            else None
        )
    else:
        run_dir = _latest_learning_run(data_root)
    if run_dir is not None and (run_dir.is_symlink() or not run_dir.is_dir()):
        run_dir = None
    response_saved_stage = form.get("response_saved_stage", "")
    target_requirement = _escape(
        form.get("target_requirement", _learning_default_requirement(data_root))
    )
    result_html = ""
    if result is not None:
        class_name = "success" if result.return_code == 0 else "error"
        body = result.stdout or result.stderr
        result_html = (
            f'<div class="notice {class_name}"><strong>{_escape(body)}</strong>'
            f" · {result.elapsed_ms}ms</div>"
        )
        if result.return_code == 0:
            prefix = "Learning workspace: "
            path_text = next(
                (
                    line[len(prefix) :]
                    for line in result.stdout.splitlines()
                    if line.startswith(prefix)
                ),
                "",
            )
            if path_text:
                run_dir = Path(path_text)
    dashboard = ""
    project_matches_run = False
    if run_dir is not None:
        case = _load_json_file(run_dir / "01_case.json") or {}
        mastery = _load_json_file(run_dir / "06_mastery.json") or {}
        learning_plan = _load_json_file(run_dir / "07_learning_plan.json") or {}
        claim = _load_json_file(run_dir / "09_claim_eligibility.json") or {}
        run = _load_json_file(run_dir / "run.json") or {}
        run_project_source_id = run.get("project_source_id")
        project_matches_run = bool(
            project_binding is not None
            and (
                (
                    isinstance(run_project_source_id, str)
                    and run_project_source_id == project_binding.project_source_id
                )
                or (
                    not isinstance(run_project_source_id, str)
                    and run.get("repository") == project_binding.repository
                )
            )
        )
        architecture = learning_plan.get("architecture_walkthrough_5_minutes", [])
        deep_dive = learning_plan.get("technical_deep_dive_15_minutes", {})
        decisions = learning_plan.get("decisions_and_trade_offs", [])
        unknowns = learning_plan.get("known_failures_and_unknowns", [])
        exercises = learning_plan.get("exercises", {})
        explain = exercises.get("Explain", {}) if isinstance(exercises, dict) else {}
        trace = exercises.get("Trace", {}) if isinstance(exercises, dict) else {}
        engineering_state = _escape(
            ui_display_value(locale, case.get("engineering_state", "UNKNOWN"))
        )
        mastery_level = _escape(ui_display_value(locale, mastery.get("level", "UNKNOWN")))
        mastery_ready = _escape(ui_bool(locale, mastery.get("interview_ready") is True))
        next_action = _escape(
            ui_display_value(locale, mastery.get("next_action", "UNKNOWN"))
        )
        engineering_truth = _escape(
            ui_display_value(locale, claim.get("engineering_truth_stage", "UNKNOWN"))
        )
        interview_ready = _escape(ui_bool(locale, claim.get("interview_ready") is True))
        resume_eligible = _escape(ui_bool(locale, claim.get("resume_eligible") is True))
        explain_saved = (
            f'<p class="saved-response" role="status">{_escape(ui_text(locale, "讲解回答已私有保存。仍需人工复核，掌握等级没有自动提升。", "Explain response saved privately. Review is still required; mastery was not advanced."))}</p>'
            if response_saved_stage == "explain"
            else ""
        )
        trace_saved = (
            f'<p class="saved-response" role="status">{_escape(ui_text(locale, "追踪回答已私有保存。仍需人工复核，掌握等级没有自动提升。", "Trace response saved privately. Review is still required; mastery was not advanced."))}</p>'
            if response_saved_stage == "trace"
            else ""
        )
        trace_exercise = (
            f"""<section id="exercise-trace" class="panel exercise">
  <p class="eyebrow">{_escape(ui_text(locale, 'L2 · 追踪练习', 'L2 · Trace exercise'))}</p>
  <h2>{_escape(ui_text(locale, '沿着真实符号追踪', 'Follow the real symbols'))}</h2>
  <p>{_escape(str(trace.get('prompt', ui_text(locale, '暂无内容。', 'Not available.'))))}</p>
  <p class="muted">{_escape(ui_text(locale, '使用图中的代码和测试节点打开有限的本地摘录。', "Use the graph's Code and Test nodes to open bounded local excerpts."))}</p>
  <form class="response-form" method="post" action="/learning/respond#exercise-trace">
    <input type="hidden" name="ui_locale" value="{locale}" />
    <input type="hidden" name="run_id" value="{_escape(run_dir.name)}" />
    <input type="hidden" name="stage" value="Trace" />
    <input type="hidden" name="target_requirement" value="{target_requirement}" />
    <label for="learning-trace-response">{_escape(ui_text(locale, '你的追踪回答', 'Your Trace response'))}</label>
    <textarea id="learning-trace-response" name="response" rows="8" maxlength="20000" required
      placeholder="{_escape(ui_text(locale, '追踪已记录的符号与测试，并指出仍然未知的部分。', 'Trace the recorded symbols and tests. Name any remaining unknowns.'))}"></textarea>
    <button type="submit">{_escape(ui_text(locale, '私有保存追踪回答', 'Save private Trace response'))}</button>
  </form>
  {trace_saved}
  <p class="muted">{_escape(ui_text(locale, '回答会在本地保存为待复核；提交不会自动提升掌握等级。', 'Saved locally as pending review. Submission never advances mastery.'))}</p>
</section>"""
            if project_matches_run
            else f"""<section id="exercise-trace" class="panel exercise needs-project">
  <p class="eyebrow">{_escape(ui_text(locale, 'L2 · 追踪练习', 'L2 · Trace exercise'))}</p>
  <h2>{_escape(ui_text(locale, '需要项目证据', 'Project evidence required'))}</h2>
  <p>{_escape(ui_text(locale, '这个练习需要一个已连接的代码项目。讲解练习仍然可用。', 'This exercise needs a connected code project. Explain remains available.'))}</p>
  <a class="button-link" href="{ui_url('/work', locale)}">{_escape(ui_text(locale, '选择项目', 'Choose project'))}</a>
</section>"""
        )
        backlink = ""
        resume_run_id = form.get("resume_run_id", "")
        bullet_id = form.get("bullet_id", "")
        if resume_run_id or bullet_id:
            try:
                if repo_root is None:
                    raise LearningTraceabilityError(
                        "selected project evidence is unavailable"
                    )
                records = load_interview_defense_records(data_root=data_root, run_id=resume_run_id)
                linked = next(item for item in records if item.bullet_id == bullet_id)
                pack = _validated_record_anchor_pack(
                    record=linked,
                    data_root=data_root,
                    repo_root=repo_root,
                    learning_run_id=run_dir.name,
                )
                pack_json = json.dumps(_anchor_pack_text(linked.bullet_text, pack)).replace(
                    "</", "<\\/"
                )
                backlink = f"""<section id="interview-defense" class="panel">
  <h2>{_escape(ui_text(locale, '面试答辩锚点', 'Interview Defense anchors'))}</h2>
  <p><strong>{_escape(linked.bullet_id)}</strong> · {_escape(linked.bullet_text)}</p>
  <p>{_escape(ui_text(locale, '精确学习记录：', 'Exact Learning run:'))} <code>{_escape(run_dir.name)}</code></p>
  {_anchor_pack_html(pack, locale)}
  <p class="muted">{_escape(ui_text(locale, '这些锚点用于定位真实实现，不证明作者身份、掌握程度、语义支持、测试执行或简历资格。', 'These anchors locate real implementation; they do not prove authorship, mastery, semantic support, test execution, or resume eligibility.'))}</p>
  <button id="copy-anchor-pack" type="button">{_escape(ui_text(locale, '复制锚点包', 'Copy anchor pack'))}</button>
  <span id="copy-anchor-status" class="muted"></span>
  <script>
    document.getElementById('copy-anchor-pack').addEventListener('click',async()=>{{
      try{{
        await navigator.clipboard.writeText({pack_json});
        document.getElementById('copy-anchor-status').textContent={json.dumps(' 已复制' if locale == 'zh-CN' else ' Copied')};
      }}catch(_error){{
        document.getElementById('copy-anchor-status').textContent={json.dumps(' 复制不可用' if locale == 'zh-CN' else ' Copy unavailable')};
      }}
    }});
  </script>
</section>"""
            except (
                ResumeWorkspaceStorageError,
                LearningTraceabilityError,
                OSError,
                ValueError,
                StopIteration,
            ):
                backlink = (
                    '<section id="interview-defense" class="panel error">'
                    f'<p>{_escape(ui_text(locale, "面试答辩关联暂不可用。", "Interview Defense mapping is unavailable."))}</p></section>'
                )
        dashboard = f"""
<section class="status-grid">
  <article class="status-card verified">
    <span>{_escape(ui_text(locale, '工程状态', 'Engineering'))}</span><strong>{engineering_state}</strong>
    <small>{_escape(ui_text(locale, '真实代码 + 已提交的测试定义', 'Real code + committed test definitions'))}</small>
  </article>
  <article class="status-card warning">
    <span>{_escape(ui_text(locale, '个人掌握', 'Human mastery'))}</span><strong>{mastery_level}</strong>
    <small>{_escape(ui_text(locale, '面试就绪：', 'Interview ready:'))} {mastery_ready}</small>
  </article>
  <article class="status-card action">
    <span>{_escape(ui_text(locale, '明确下一步', 'Exact next action'))}</span><strong>{next_action}</strong>
    <small>{_escape(ui_text(locale, '不会自动升级', 'No automatic promotion'))}</small>
  </article>
  <article class="status-card">
    <span>{_escape(ui_text(locale, '练习语言', 'Practice language'))}</span><strong>English</strong>
    <small>{_escape(ui_text(locale, '独立于界面语言', 'Independent of UI locale'))}</small>
  </article>
</section>
<section class="panel hero-copy">
  <p class="eyebrow">{_escape(ui_text(locale, '30 秒讲解', '30-second explanation'))}</p>
  <h2>{_escape(ui_text(locale, '对话式 RAG · 分块与检索', 'Conversation RAG · Chunking + Retrieval'))}</h2>
  <p>{_escape(str(learning_plan.get("plain_language_30_seconds", ui_text(locale, "暂无内容。", "Not available."))))}</p>
  <div class="button-row">
    <a class="button-link" href="#exercise-explain">{_escape(ui_text(locale, '开始讲解', 'Start Explain'))}</a>
    <a class="button-link secondary" href="#exercise-trace">{_escape(ui_text(locale, '开始追踪', 'Start Trace'))}</a>
  </div>
</section>
<details class="panel">
  <summary>{_escape(ui_text(locale, '查看证据图（进阶）', 'View Evidence Graph (advanced)'))}</summary>
  {_learning_graph(run_dir, locale)}
</details>
{backlink}
<section class="panel">
  <h2>{_escape(ui_text(locale, '目标 JD 相关性', 'Target JD relevance'))}</h2>
  <p>{_escape(str(claim.get("target_requirement", ui_text(locale, "暂无内容。", "Not available."))))}</p>
  <div class="truth-grid">
    <div><span>{_escape(ui_text(locale, '工程事实', 'Engineering truth'))}</span><strong>{engineering_truth}</strong></div>
    <div><span>{_escape(ui_text(locale, '面试就绪', 'Interview ready'))}</span><strong>{interview_ready}</strong></div>
    <div><span>{_escape(ui_text(locale, '简历可用', 'Resume eligible'))}</span><strong>{resume_eligible}</strong></div>
  </div>
  <p class="warning-text">{_escape(str(claim.get("rationale", ui_text(locale, "暂无内容。", "Not available."))))}</p>
</section>
<details class="panel" open>
  <summary>{_escape(ui_text(locale, '架构讲解 · 5 分钟', 'Architecture · 5 minutes'))}</summary>
  <ol>{_learning_list(architecture, locale)}</ol>
</details>
<details class="panel">
  <summary>{_escape(ui_text(locale, '代码、证据与 15 分钟深挖', 'Code, evidence, and 15-minute deep dive'))}</summary>
  <pre>{_escape(json.dumps(deep_dive, ensure_ascii=False, indent=2))}</pre>
  <h3>{_escape(ui_text(locale, '决策与权衡', 'Decisions and trade-offs'))}</h3><ul>{_learning_list(decisions, locale)}</ul>
  <h3>{_escape(ui_text(locale, '已知失败与未知项', 'Known failures and unknowns'))}</h3><ul>{_learning_list(unknowns, locale)}</ul>
</details>
<section id="exercise-explain" class="panel exercise">
  <p class="eyebrow">{_escape(ui_text(locale, 'L1 · 讲解练习', 'L1 · Explain exercise'))}</p>
  <h2>{_escape(ui_text(locale, '先独立回答，不看标准答案', 'Start without an answer key'))}</h2>
  <p>{_escape(str(explain.get("prompt", ui_text(locale, "暂无内容。", "Not available."))))}</p>
  <p class="muted">{_escape(ui_text(locale, '完成需要你自己写下回答回执；仅打开此卡片不代表通过 L1。', 'Completion requires a user-authored receipt; opening this card does not pass L1.'))}</p>
  <form class="response-form" method="post"
    action="/learning/respond#exercise-explain">
    <input type="hidden" name="ui_locale" value="{locale}" />
    <input type="hidden" name="run_id" value="{_escape(run_dir.name)}" />
    <input type="hidden" name="stage" value="Explain" />
    <input type="hidden" name="target_requirement" value="{target_requirement}" />
    <label for="learning-explain-response">{_escape(ui_text(locale, '你的讲解回答', 'Your Explain response'))}</label>
    <textarea id="learning-explain-response" name="response" rows="8" maxlength="20000"
      required
      placeholder="{_escape(ui_text(locale, '请讲清楚它，并包含一个权衡和一个事实边界。', 'Explain it. Include one trade-off and one truth boundary.'))}"></textarea>
    <button type="submit">{_escape(ui_text(locale, '私有保存讲解回答', 'Save private Explain response'))}</button>
  </form>
  {explain_saved}
  <p class="muted">{_escape(ui_text(locale, '回答会在本地保存为待复核；提交不会自动提升掌握等级。', 'Saved locally as pending review. Submission never advances mastery.'))}</p>
</section>
{trace_exercise}
<section class="panel footnote">
  <strong>{_escape(ui_text(locale, '私有记录', 'Private run'))}</strong> <code>{_escape(str(run_dir))}</code><br />
  <span>{_escape(ui_text(locale, '分支', 'Branch'))} {_escape(str(run.get("branch", "")))} ·
    {_escape(ui_text(locale, '提交', 'commit'))} {_escape(str(run.get("commit", "")))}</span>
</section>
"""
    else:
        dashboard = f"""
<section class="panel empty-state">
  <span class="kicker">{_escape(ui_text(locale, '从这里开始', 'Start here'))}</span>
  <h2>{_escape(ui_text(locale, '把一个真实项目，变成面试时能讲清楚的能力', 'Turn one real project into interview confidence'))}</h2>
  <p>{_escape(ui_text(locale, '选择你想练习的岗位要求。练习会私有保存，但系统不会自动把你标记为“已掌握”。', 'Choose one role requirement to practice. Responses are saved privately, and the system never marks you ready automatically.'))}</p>
</section>
"""
    repository_notice = ""
    if not project_available:
        repository_notice = f"""<section class="notice warning" role="status">
  <strong>{_escape(ui_text(locale, '这个练习需要一个已连接的代码项目。', 'This exercise needs a connected code project.'))}</strong>
  <p>{_escape(ui_text(locale, '选择一个本地 Git 项目以启用代码级追踪和调试；已有证据支持的讲解练习仍可继续。', 'Choose a local Git project to enable code-level Trace and Debug practice. Explain remains available when an existing case has enough evidence.'))}</p>
</section>"""
    elif not fixture_available:
        repository_notice = f"""<section class="notice warning" role="status">
  <strong>{_escape(ui_text(locale, '当前项目已连接，但不适用于这个示例练习。', 'The current project is connected but does not match this seed exercise.'))}</strong>
  <p>{_escape(ui_text(locale, '这个项目没有找到此练习需要的代码锚点。你可以选择其他项目或创建新的学习案例。', 'This project does not contain the code anchors required by this exercise. Choose another project or create a new Learning case.'))}</p>
</section>"""
    build_button = (
        f'<button type="submit">{_escape(ui_text(locale, "创建 / 刷新学习案例", "Build / refresh learning case"))}</button>'
        if fixture_available
        else f'<a class="button-link" href="{ui_url("/work", locale)}">{_escape(ui_text(locale, "选择项目", "Choose project"))}</a>'
    )
    work_summary = render_use_my_work(
        load_work_context(
            data_root,
            workspace_root=repo_root if project_available else None,
        ),
        locale,
        boundary=ui_text(
            locale,
            "面试练习只会引用当前案例明确记录的项目、代码和测试锚点；个人掌握仍需你自己完成。",
            "Interview practice uses only project, code, and test anchors explicitly recorded for the current case. Your mastery still requires your own work.",
        ),
    )
    explain_state = "AVAILABLE" if run_dir is not None else "NEEDS_CASE_EVIDENCE"
    trace_state = "AVAILABLE" if run_dir is not None and project_matches_run else "NEEDS_PROJECT_EVIDENCE"
    debug_state = (
        "NEEDS_CASE_STAGE"
        if run_dir is not None and project_matches_run
        else "NEEDS_PROJECT_EVIDENCE"
    )
    capability_state_labels = {
        "AVAILABLE": ui_text(locale, "可用", "Available"),
        "NEEDS_PROJECT_EVIDENCE": ui_text(
            locale, "需要项目证据", "Needs project evidence"
        ),
        "NEEDS_CASE_EVIDENCE": ui_text(
            locale, "需要案例证据", "Needs case evidence"
        ),
        "NEEDS_CASE_STAGE": ui_text(
            locale, "当前案例尚未开放", "Not available in this case yet"
        ),
    }
    capabilities = f"""<section class="learning-capabilities" aria-label="{_escape(ui_text(locale, '练习能力', 'Exercise capabilities'))}">
  <article data-learning-capability="explain" data-state="{explain_state}"><span>EXPLAIN</span><strong>{_escape(capability_state_labels[explain_state])}</strong></article>
  <article data-learning-capability="trace" data-state="{trace_state}"><span>TRACE</span><strong>{_escape(capability_state_labels[trace_state])}</strong></article>
  <article data-learning-capability="debug" data-state="{debug_state}"><span>DEBUG</span><strong>{_escape(capability_state_labels[debug_state])}</strong></article>
</section>"""
    practice_panel = _practice_panel_html(data_root, form, locale)
    readiness_map = _production_readiness_map_html(
        data_root, repo_root, form, locale
    )
    active_case = _active_learning_case(data_root)
    active_case_panel = ""
    seed_case_note = ""
    if active_case is not None and active_case.id != "conversation-rag-chunking-retrieval":
        active_case_panel = f"""<section class="panel active-case">
  <span class="kicker">{_escape(ui_text(locale, '当前学习案例', 'Active Learning Case'))}</span>
  <h2>{_escape(active_case.title)}</h2>
  <p>{_escape(active_case.problem)}</p>
  <p class="muted">{_escape(", ".join(active_case.concepts))}</p>
</section>"""
        seed_case_note = f"""<section class="notice warning" role="status">
  <strong>{_escape(ui_text(locale, '下方是独立的历史参考案例（Conversation RAG），不是当前活动案例。', 'The section below is a separate historical reference case (Conversation RAG), not the active case.'))}</strong>
</section>"""
    body = f"""
    {work_summary}
    {active_case_panel}
    {repository_notice}
    {capabilities}
    {readiness_map}
    <form class="panel build-form" method="post" action="/learning/run">
      <input type="hidden" name="ui_locale" value="{locale}" />
      <label>{_escape(ui_text(locale, '当前目标 JD 要求', 'Current target-JD requirement'))}
        <input name="target_requirement" value="{target_requirement}" required />
      </label>
      {build_button}
    </form>
    {result_html}
    {seed_case_note}
    {dashboard}
    {practice_panel}
    """
    learning_current_url = ui_url(
        "/learning",
        locale,
        run_id=run_dir.name if run_dir is not None else "",
        resume_run_id=form.get("resume_run_id", ""),
        bullet_id=form.get("bullet_id", ""),
    )
    return render_app_shell(
        active="learning",
        locale=locale,
        current_url=learning_current_url,
        title=f"SoloScale · {ui_text(locale, '学习工作台', 'Learning Workspace')}",
        eyebrow=ui_text(locale, "学习工作台", "Learning workspace"),
        heading=ui_text(locale, "看清自己会什么，也知道下一步练什么。", "See what you can explain—and what to practice next."),
        description=ui_text(locale, "真实证据与个人掌握分开记录；每一步都由你自己完成。", "Engineering evidence and personal mastery stay separate, so every step remains honestly yours."),
        body=body,
        extra_css="""
#main-content{display:grid;gap:18px}.panel{padding:20px}.build-form{display:grid;grid-template-columns:1fr auto;gap:12px;align-items:end}
.use-my-work{display:grid;grid-template-columns:minmax(260px,.75fr) 1fr auto;gap:16px;align-items:center;padding:15px 18px;border:1px solid #d9e2dc;border-radius:15px;background:linear-gradient(110deg,#fff,#f1f8f5)}.use-my-work>div{display:grid;gap:3px}.use-my-work strong{font-size:13px}.use-my-work p{margin:0;color:var(--text-muted);font-size:13px}.use-my-work a{font-weight:800;text-decoration:none;white-space:nowrap}
.button-link{display:inline-flex;align-items:center;min-height:44px;border-radius:13px;padding:11px 15px;background:var(--brand);color:white;font-weight:800;text-decoration:none}.button-link.secondary{background:var(--success)}.button-row{display:flex;gap:10px;flex-wrap:wrap}
.saved-response{border-left:3px solid var(--success);padding:9px 12px;background:var(--success-soft);color:var(--success);border-radius:8px}
.learning-capabilities{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.learning-capabilities article{display:grid;gap:6px;border:1px solid var(--border);border-radius:13px;padding:14px;background:var(--surface-subtle)}.learning-capabilities span{font-size:.75rem;letter-spacing:.08em;color:var(--text-muted)}.learning-capabilities strong{font-size:.78rem;color:var(--brand)}.learning-capabilities [data-state="NEEDS_PROJECT_EVIDENCE"] strong,.learning-capabilities [data-state="NEEDS_CASE_EVIDENCE"] strong,.learning-capabilities [data-state="NEEDS_CASE_STAGE"] strong{color:var(--warning)}
.status-grid,.truth-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.status-card,.truth-grid div{border:1px solid var(--border);border-radius:14px;padding:16px;background:var(--surface-subtle);display:grid;gap:7px}.status-card span,.truth-grid span{color:var(--text-muted);text-transform:uppercase;letter-spacing:.09em;font-size:.75rem}.status-card strong{font-size:1.15rem}.verified strong{color:var(--success)}.warning strong,.warning-text{color:var(--warning)}.action strong{color:var(--brand)}
.hero-copy p:not(.eyebrow){max-width:820px;font-size:1.08rem;line-height:1.7}.graph-scroll{overflow:auto;background:#f7f9fc;border:1px solid var(--border);border-radius:12px}#learning-graph{width:1060px;display:block}.panel pre{background:#f7f9fc;color:#2c3548}.panel details summary,details.panel summary{cursor:pointer;font-size:1.05rem;font-weight:750}.hidden{display:none}.footnote span{color:var(--text-muted)}
.readiness-map{background:linear-gradient(145deg,#fff,var(--brand-soft))}.readiness-map h2{margin:7px 0}.readiness-map .muted{color:var(--text-muted)}.readiness-domains{display:grid;gap:12px;margin-top:16px}.readiness-domain{display:grid;gap:8px;padding:13px;border:1px solid var(--border);border-radius:13px;background:#fff}.readiness-domain>strong{font-size:13px}.maturity-row{display:flex;gap:6px;flex-wrap:wrap}.maturity-chip{padding:4px 8px;border-radius:999px;background:var(--surface-subtle);color:var(--text-muted);font-size:10px;font-weight:800}.maturity-chip.ready{background:var(--success-soft);color:var(--success)}.maturity-chip.needs{background:var(--warning-soft);color:var(--warning)}.highest-gap{display:grid;grid-template-columns:1fr auto;gap:18px;align-items:center;margin-top:18px;padding:16px;border:1px solid var(--brand);border-radius:14px;background:var(--brand-soft)}.highest-gap h3{margin:8px 0 5px}.highest-gap p{margin:0;color:var(--text-muted)}.highest-gap form,.highest-gap .button-link{align-self:center;white-space:nowrap}
@media(max-width:760px){.use-my-work,.learning-capabilities,.status-grid,.truth-grid,.build-form,.highest-gap{grid-template-columns:1fr}.highest-gap form,.highest-gap .button-link{justify-self:start}}
""",
    )


def _control_tower_section(
    data_root: Path, locale: UILocale = DEFAULT_UI_LOCALE
) -> str:
    exists, _ = _read_control_tower(data_root)
    if not exists:
        return f'<p class="tool-state">{_escape(ui_text(locale, "还没有工程概览。生成后可以在这里查看。", "No engineering overview yet. Generate one to view it here."))}</p>'
    return (
        f'<p class="tool-state"><a href="{ui_url("/control-tower", locale)}">'
        + _escape(ui_text(locale, "打开工程概览", "Open engineering overview"))
        + "</a></p>"
    )


def _ai_settings_page(
    data_root: Path,
    *,
    locale: UILocale = DEFAULT_UI_LOCALE,
    detail: str | None = None,
    notice: str | None = None,
    desktop_mode: bool = False,
) -> str:
    preference = _load_ai_provider_preference(data_root)
    local_status = _ollama_readiness(preference)
    hosted_gateway = model_gateway_for(ModelProviderId.SOLOSCALE_HOSTED)
    hosted_ready = (
        hosted_gateway.descriptor.configuration_state
        is GatewayConfigurationState.CONFIGURED
    )
    openai_ready = openai_api_key_is_configured()
    provider_names = {
        ModelProviderId.OLLAMA: ui_text(locale, "本地 AI", "Local AI"),
        ModelProviderId.SOLOSCALE_HOSTED: ui_text(
            locale, "SoloScale 托管 AI", "SoloScale Hosted AI"
        ),
        ModelProviderId.OPENAI_COMPATIBLE: "OpenAI API",
    }
    status_map = {
        ModelProviderId.OLLAMA: (
            ui_text(locale, "可用", "Ready")
            if local_status.ready
            else ui_text(locale, "需要设置", "Setup needed")
        ),
        ModelProviderId.SOLOSCALE_HOSTED: (
            ui_text(locale, "可用", "Available")
            if hosted_ready
            else ui_text(locale, "当前不可用", "Unavailable")
        ),
        ModelProviderId.OPENAI_COMPATIBLE: (
            ui_text(locale, "已配置", "Configured")
            if openai_ready
            else ui_text(locale, "未配置", "Not configured")
        ),
    }
    models = {
        ModelProviderId.OLLAMA: preference.ollama_model,
        ModelProviderId.SOLOSCALE_HOSTED: hosted_gateway.descriptor.model or "—",
        ModelProviderId.OPENAI_COMPATIBLE: preference.openai_model,
    }
    notice_html = (
        f'<p class="notice" role="status">{_escape(notice)}</p>' if notice else ""
    )
    if detail is None:
        other_cards = "".join(
            f"""<a class="service-card" href="{ui_url(f'/settings/ai/{path}', locale)}">
  <span><strong>{_escape(provider_names[provider])}</strong><small>{_escape(models[provider])}</small></span>
  <span class="service-state">{_escape(status_map[provider])} →</span>
</a>"""
            for path, provider in (
                ("local", ModelProviderId.OLLAMA),
                ("hosted", ModelProviderId.SOLOSCALE_HOSTED),
                ("openai", ModelProviderId.OPENAI_COMPATIBLE),
            )
            if provider is not preference.provider
        )
        state_labels = {
            "Ready": ui_text(locale, "已就绪", "Ready"),
            "Not connected": ui_text(locale, "未连接", "Not connected"),
            "Reconnect": ui_text(locale, "需要重新连接", "Reconnect"),
            "Needs attention": ui_text(locale, "需要处理", "Needs attention"),
            "Credential detected": ui_text(locale, "已检测到凭据", "Credential detected"),
            "Connected": ui_text(locale, "已连接", "Connected"),
            "Not configured": ui_text(locale, "未配置", "Not configured"),
            "Handoff ready": ui_text(locale, "分段交接可用", "Handoff ready"),
            "Export package ready": ui_text(locale, "导出包可用", "Export package ready"),
        }
        service_details = {
            "HeyGen": ui_text(
                locale,
                "可导出精确分段并把下载后的 Avatar 视频映射回场景。",
                "Export exact segments and map downloaded Avatar clips back to scenes.",
            ),
            "LinkedIn": ui_text(
                locale,
                "已审核 Artifact 经发布队列绑定精确账号后执行；BuildLog 只提供历史与回执投影。",
                "Approved artifacts run through the Publish Queue against an exact account; BuildLog provides history and receipt projection only.",
            ),
            "X": ui_text(
                locale,
                "单帖或 Thread 由发布队列选择精确 ChannelAccount；BuildLog 保留溯源与回执投影。",
                "Posts and threads use the Publish Queue with an exact ChannelAccount; BuildLog retains provenance and receipt projection.",
            ),
            "YouTube": ui_text(
                locale,
                "当前准备视频、封面、字幕和元数据；直接上传尚未启用。",
                "Currently prepares video, thumbnail, subtitles, and metadata; direct upload is not enabled.",
            ),
        }
        connected_cards = "".join(
            (
                f'''<a class="integration-card integration-link" href="{ui_url('/settings/media/heygen', locale)}"><div><strong>{_escape(status.service)}</strong><p>{_escape(service_details[status.service])}</p></div>
                <span class="integration-state {'pass' if status.ready else 'pending'}">{_escape(state_labels.get(status.state, status.state))} →</span></a>'''
                if status.service == "HeyGen"
                else f'''<article class="integration-card"><div><strong>{_escape(status.service)}</strong><p>{_escape(service_details[status.service])}</p></div>
                <span class="integration-state {'pass' if status.ready else 'pending'}">{_escape(state_labels.get(status.state, status.state))}</span></article>'''
            )
            for status in connected_service_statuses()
        )
        body = f"""{notice_html}<section class="current-service">
  <span class="kicker">{_escape(ui_text(locale, '当前 AI 服务', 'Current AI service'))}</span>
  <div><h2>{_escape(provider_names[preference.provider])}</h2><p>{_escape(models[preference.provider])}</p></div>
  <strong class="ready-dot">● {_escape(status_map[preference.provider])}</strong>
  <a class="button-link" href="{ui_url('/settings/ai/' + {'ollama':'local','soloscale_hosted':'hosted','openai_compatible':'openai'}[preference.provider.value], locale)}">{_escape(ui_text(locale, '管理', 'Manage'))}</a>
</section>
<section class="other-services"><span class="kicker">{_escape(ui_text(locale, '其他选择', 'Other options'))}</span>{other_cards}</section>
<section class="connected-services"><span class="kicker">{_escape(ui_text(locale, '创作与发布服务', 'Creation and publishing services'))}</span>
<h2>{_escape(ui_text(locale, '连接状态一目了然', 'Connection status at a glance'))}</h2>
<div class="connected-grid">{connected_cards}</div></section>"""
        current_url = "/settings/ai"
        script = ""
    elif detail == "local":
        checks = (
            (ui_text(locale, "Ollama 已安装", "Ollama installed"), local_status.installed),
            (ui_text(locale, "运行服务可连接", "Runtime reachable"), local_status.reachable),
            (ui_text(locale, "模型已安装", "Model available"), local_status.model_available),
        )
        check_html = "".join(
            f'<li class="{"pass" if passed else "pending"}">{"✓" if passed else "○"} {_escape(label)}</li>'
            for label, passed in checks
        )
        action_buttons = "" if local_status.ready else (
            f'<button name="action" value="start" type="submit">{_escape(ui_text(locale, "启动 Ollama", "Start Ollama"))}</button>'
            if local_status.installed and not local_status.reachable
            else f'<button name="action" value="download" type="submit">{_escape(ui_text(locale, "下载模型", "Download model"))}</button>'
            if local_status.reachable and not local_status.model_available
            else f'<a class="button-link" href="https://ollama.com/download/mac">{_escape(ui_text(locale, "安装 Ollama", "Install Ollama"))}</a>'
        )
        body = f"""<a class="back-link" href="{ui_url('/settings/ai', locale)}">← {_escape(ui_text(locale, 'AI 服务', 'AI Service'))}</a>{notice_html}
<section class="setup-card"><span class="kicker">{_escape(ui_text(locale, '本地 AI', 'Local AI'))}</span><h2>Ollama · {_escape(preference.ollama_model)}</h2>
<ul class="readiness-list">{check_html}</ul>
<form method="post" action="/settings/ai/local"><input type="hidden" name="ui_locale" value="{locale}" />
<label>{_escape(ui_text(locale, '模型', 'Model'))}<input name="model" value="{_escape(preference.ollama_model)}" /></label>
<input type="hidden" name="ollama_url" value="{_escape(preference.ollama_url)}" />
<div class="button-row"><button name="action" value="test" type="submit">{_escape(ui_text(locale, '测试', 'Test'))}</button>{action_buttons}
<button class="secondary" name="action" value="use_default" type="submit" {'disabled' if not local_status.ready else ''}>{_escape(ui_text(locale, '设为默认', 'Use as default'))}</button></div></form></section>"""
        current_url = "/settings/ai/local"
        script = ""
    elif detail == "hosted":
        body = f"""<a class="back-link" href="{ui_url('/settings/ai', locale)}">← {_escape(ui_text(locale, 'AI 服务', 'AI Service'))}</a>{notice_html}
<section class="setup-card"><span class="kicker">SoloScale Hosted AI</span><h2>{_escape(hosted_gateway.descriptor.model or '—')}</h2>
<p class="service-state">{_escape(status_map[ModelProviderId.SOLOSCALE_HOSTED])}</p>
<p>{_escape(ui_text(locale, '托管服务不可用时，SoloScale 会保留当前工作并让你选择其他 AI 服务。', 'When Hosted AI is unavailable, SoloScale preserves your work and lets you choose another service.'))}</p>
<form method="post" action="/settings/ai/hosted"><input type="hidden" name="ui_locale" value="{locale}" /><div class="button-row">
<button name="action" value="test" type="submit">{_escape(ui_text(locale, '重试', 'Retry'))}</button>
<button class="secondary" name="action" value="use_default" type="submit" {'disabled' if not hosted_ready else ''}>{_escape(ui_text(locale, '设为默认', 'Use as default'))}</button></div></form></section>"""
        current_url = "/settings/ai/hosted"
        script = ""
    elif detail == "openai":
        desktop_note = ui_text(
            locale,
            "API key 只会保存到 macOS Keychain；页面、设置文件和日志都不会显示它。",
            "The API key is stored only in macOS Keychain and never appears in pages, settings files, or logs.",
        )
        unavailable = "" if desktop_mode else f'<p class="notice warning">{_escape(ui_text(locale, "请在 SoloScale Desktop App 中配置 OpenAI；普通浏览器不会接收密钥。", "Configure OpenAI in the SoloScale Desktop App. A normal browser never accepts the key."))}</p>'
        save_disabled = "" if desktop_mode else " disabled"
        delete_button = (
            f'<button id="delete-openai-key" class="danger" type="button">{_escape(ui_text(locale, "移除 Keychain 密钥", "Remove Keychain key"))}</button>'
            if openai_ready and desktop_mode
            else ""
        )
        use_default_button = (
            f'<button class="secondary" name="action" value="use_default" type="submit">{_escape(ui_text(locale, "设为默认", "Use as default"))}</button>'
            if openai_ready
            else ""
        )
        body = f"""<a class="back-link" href="{ui_url('/settings/ai', locale)}">← {_escape(ui_text(locale, 'AI 服务', 'AI Service'))}</a>{notice_html}{unavailable}
<section class="setup-card"><span class="kicker">OpenAI API</span><h2>{_escape(status_map[ModelProviderId.OPENAI_COMPATIBLE])}</h2><p>{_escape(desktop_note)}</p>
<form id="openai-setup"><input type="hidden" id="openai-locale" value="{locale}" />
<label>API Key<input id="openai-api-key" type="password" maxlength="512" autocomplete="new-password" value="" placeholder="sk-…"{save_disabled} /></label>
<label>{_escape(ui_text(locale, '模型', 'Model'))}<input id="openai-model" maxlength="120" value="{_escape(preference.openai_model)}" /></label>
<div class="button-row"><button id="save-openai-key" type="submit"{save_disabled}>{_escape(ui_text(locale, '保存并使用 OpenAI', 'Save & use OpenAI'))}</button>{delete_button}</div></form>
<form method="post" action="/settings/ai/openai"><input type="hidden" name="ui_locale" value="{locale}" /><input type="hidden" name="openai_model" value="{_escape(preference.openai_model)}" /><div class="button-row"><button class="secondary" name="action" value="test" type="submit" {'disabled' if not openai_ready else ''}>{_escape(ui_text(locale, '测试连接', 'Test connection'))}</button>{use_default_button}</div></form>
<p id="openai-setup-status" role="status"></p></section>"""
        current_url = "/settings/ai/openai"
        script = f"""
const setup=document.getElementById('openai-setup');
if(setup) setup.addEventListener('submit',async(event)=>{{
  event.preventDefault();
  const status=document.getElementById('openai-setup-status');
  const key=document.getElementById('openai-api-key');
  const model=document.getElementById('openai-model');
  const bridge=window.webkit?.messageHandlers?.soloscaleCredentials;
  if(!bridge){{status.textContent={json.dumps(ui_text(locale, '请在 Desktop App 中完成设置。', 'Complete setup in the Desktop App.'))};return;}}
  if(!key.value.trim()){{status.textContent={json.dumps(ui_text(locale, '请输入 API key。', 'Enter an API key.'))};return;}}
  const response=await fetch('/settings/ai/openai',{{method:'POST',headers:{{'Content-Type':'application/x-www-form-urlencoded'}},body:new URLSearchParams({{ui_locale:document.getElementById('openai-locale').value,action:'prepare',openai_model:model.value}})}});
  if(!response.ok){{status.textContent={json.dumps(ui_text(locale, '模型设置无法保存。', 'The model setting could not be saved.'))};return;}}
  const secret=key.value; key.value='';
  bridge.postMessage({{action:'saveOpenAIKey',apiKey:secret,returnPath:{json.dumps(ui_url('/settings/ai/openai', locale, provider='saved'))}}});
}});
const remove=document.getElementById('delete-openai-key');
if(remove) remove.addEventListener('click',()=>window.webkit.messageHandlers.soloscaleCredentials.postMessage({{action:'deleteOpenAIKey',returnPath:{json.dumps(ui_url('/settings/ai/openai', locale, provider='removed'))}}}));
"""
    else:
        raise ValueError("unknown AI settings detail")
    return render_app_shell(
        active="advanced",
        locale=locale,
        current_url=current_url,
        title=f"SoloScale · {ui_text(locale, 'AI 服务', 'AI Service')}",
        eyebrow=ui_text(locale, "设置", "Settings"),
        heading=ui_text(locale, "选择一次，所有工作流自动使用。", "Choose once. Every workflow follows."),
        description=ui_text(locale, "简历与内容共享同一个默认 AI 服务；高级设置只在这里出现。", "Resume and Content share one default AI service. Advanced setup stays here."),
        body=body,
        script=script,
        extra_css="""
.current-service,.setup-card,.other-services,.connected-services{display:grid;gap:16px;padding:24px;border:1px solid var(--border);border-radius:20px;background:linear-gradient(145deg,#fff,var(--brand-soft))}.current-service{grid-template-columns:1fr auto;align-items:center}.current-service .kicker,.current-service div{grid-column:1}.current-service h2{margin:4px 0}.current-service p{margin:0;color:var(--text-muted)}.current-service .ready-dot,.current-service .button-link{grid-column:2}.ready-dot{color:var(--success)}.button-link{display:inline-flex;padding:10px 14px;border-radius:12px;background:var(--brand);color:white;text-decoration:none;font-weight:800}.other-services,.connected-services{margin-top:18px;background:#fff}.service-card{display:flex;justify-content:space-between;gap:16px;align-items:center;padding:16px;border:1px solid var(--border);border-radius:14px;text-decoration:none;color:var(--text);background:var(--surface-subtle)}.service-card span:first-child{display:grid;gap:3px}.service-card small,.service-state{color:var(--text-muted)}.connected-services h2{margin:0}.connected-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.integration-card{display:flex;justify-content:space-between;gap:14px;align-items:flex-start;padding:16px;border:1px solid var(--border);border-radius:14px;background:var(--surface-subtle)}.integration-card p{margin:6px 0 0;color:var(--text-muted);font-size:.9rem}.integration-state{white-space:nowrap;font-size:.85rem;font-weight:800}.integration-state.pass{color:var(--success)}.integration-state.pending{color:var(--warning)}.setup-card{max-width:780px;margin-top:16px}.setup-card h2{margin:0}.setup-card form{display:grid;gap:14px}.readiness-list{list-style:none;padding:0;display:grid;gap:8px}.readiness-list .pass{color:var(--success)}.readiness-list .pending{color:var(--warning)}.button-row{display:flex;flex-wrap:wrap;gap:10px}.button-row button,.setup-card>form>button{width:auto}.button-row .secondary,.setup-card>form>.secondary{background:var(--surface-subtle);color:var(--brand);border:1px solid var(--border)}.button-row .danger{background:#fff0ef;color:var(--danger)}.back-link{font-weight:800;text-decoration:none}
.integration-link{text-decoration:none;color:inherit}
@media(max-width:700px){.current-service,.connected-grid{grid-template-columns:1fr}.current-service .ready-dot,.current-service .button-link{grid-column:1;justify-self:start}.service-card,.integration-card{align-items:flex-start;flex-direction:column}}
""",
    )


def _heygen_settings_page(
    data_root: Path,
    *,
    locale: UILocale = DEFAULT_UI_LOCALE,
    outcome: str | None = None,
    desktop_mode: bool = False,
) -> str:
    try:
        profile = load_media_profile_settings(data_root)
    except MediaProfileError:
        profile = None
    try:
        budget_policy = load_budget_policy(data_root)
    except MediaCostError:
        budget_policy = BudgetPolicy()
    configured = heygen_api_key_is_configured()
    notices = {
        "saved": ui_text(
            locale,
            "HeyGen API key 已安全保存到 macOS Keychain。",
            "The HeyGen API key is securely stored in macOS Keychain.",
        ),
        "removed": ui_text(
            locale,
            "HeyGen API key 已从 macOS Keychain 移除；手工分段交接仍可使用。",
            "The HeyGen API key was removed from Keychain. Manual segment handoff remains available.",
        ),
        "profile-saved": ui_text(
            locale,
            "Avatar 与中英文 voice 配置已保存。",
            "Avatar and bilingual voice settings were saved.",
        ),
        "budget-saved": ui_text(
            locale,
            "媒体预算已保存；付费步骤会先按这些上限检查。",
            "Media budgets were saved. Paid steps will check these limits first.",
        ),
        "budget-invalid": ui_text(
            locale,
            "预算未保存；请输入空值或不小于 0 的美元金额。",
            "Budgets were not saved. Enter blank values or non-negative USD amounts.",
        ),
        "invalid": ui_text(
            locale,
            "配置未保存；请只使用 HeyGen 提供的 ID。",
            "Settings were not saved. Use only IDs supplied by HeyGen.",
        ),
    }
    notice = notices.get(outcome or "")
    notice_html = (
        f'<p class="notice" role="status">{_escape(notice)}</p>' if notice else ""
    )
    desktop_warning = (
        ""
        if desktop_mode
        else f'<p class="notice warning">{_escape(ui_text(locale, "请在 SoloScale Desktop App 中保存 HeyGen key；普通浏览器不会接收它。", "Save the HeyGen key in the SoloScale Desktop App. A normal browser never accepts it."))}</p>'
    )
    disabled = "" if desktop_mode else " disabled"
    profile_fields = profile or MediaProfile()
    def budget_value(value: Decimal | None) -> str:
        return "" if value is None else format(value, "f")
    delete_button = (
        f'<button id="delete-heygen-key" class="danger" type="button">{_escape(ui_text(locale, "移除 Keychain 密钥", "Remove Keychain key"))}</button>'
        if configured and desktop_mode
        else ""
    )
    body = f"""<a class="back-link" href="{ui_url('/settings/ai', locale)}">← {_escape(ui_text(locale, 'AI 与发布服务', 'AI and publishing services'))}</a>{notice_html}{desktop_warning}
<section class="setup-card">
  <span class="kicker">HeyGen · AvatarProvider</span>
  <h2>{_escape(ui_text(locale, '已连接' if configured else '未配置', 'Connected' if configured else 'Not configured'))}</h2>
  <p>{_escape(ui_text(locale, 'API key 只保存于 macOS Keychain。SoloScale 会优先复用人物素材，仅在需要新口播片段且你确认预算后调用 HeyGen。', 'The API key stays in macOS Keychain. SoloScale reuses presenter assets first and calls HeyGen only for new speaking segments after budget approval.'))}</p>
  <form id="heygen-key-setup">
    <label>API Key<input id="heygen-api-key" type="password" maxlength="512" autocomplete="new-password" value="" placeholder="HeyGen API key"{disabled} /></label>
    <div class="button-row"><button type="submit"{disabled}>{_escape(ui_text(locale, '安全保存', 'Save securely'))}</button>{delete_button}</div>
  </form>
  <p id="heygen-key-status" role="status"></p>
</section>
<section class="setup-card">
  <span class="kicker">BudgetGuard</span>
  <h2>{_escape(ui_text(locale, '媒体费用上限', 'Media spending limits'))}</h2>
  <p>{_escape(ui_text(locale, '留空表示不设置该级别上限；未知价格仍会停下来要求单次确认。', 'Leave a field blank for no limit at that scope. Unknown pricing still stops for one-time approval.'))}</p>
  <form method="post" action="/settings/media/heygen">
    <input type="hidden" name="ui_locale" value="{locale}" />
    <input type="hidden" name="action" value="save_budget" />
    <label>{_escape(ui_text(locale, '每次付费操作 USD', 'Per paid operation USD'))}<input type="number" min="0" step="0.01" name="per_paid_operation_usd" value="{budget_value(budget_policy.per_paid_operation_usd)}" /></label>
    <label>{_escape(ui_text(locale, '每个故事 USD', 'Per story USD'))}<input type="number" min="0" step="0.01" name="per_story_usd" value="{budget_value(budget_policy.per_story_usd)}" /></label>
    <label>{_escape(ui_text(locale, '每个视频 USD', 'Per video USD'))}<input type="number" min="0" step="0.01" name="per_video_usd" value="{budget_value(budget_policy.per_video_usd)}" /></label>
    <label>{_escape(ui_text(locale, '每日 USD', 'Daily USD'))}<input type="number" min="0" step="0.01" name="daily_usd" value="{budget_value(budget_policy.daily_usd)}" /></label>
    <label>{_escape(ui_text(locale, '每月 USD', 'Monthly USD'))}<input type="number" min="0" step="0.01" name="monthly_usd" value="{budget_value(budget_policy.monthly_usd)}" /></label>
    <button type="submit">{_escape(ui_text(locale, '保存预算', 'Save budgets'))}</button>
  </form>
</section>
<section class="setup-card">
  <span class="kicker">{_escape(ui_text(locale, '人物配置', 'Presenter profile'))}</span>
  <form method="post" action="/settings/media/heygen">
    <input type="hidden" name="ui_locale" value="{locale}" />
    <input type="hidden" name="action" value="save_profile" />
    <label>{_escape(ui_text(locale, 'Avatar Group ID（可选）', 'Avatar Group ID (optional)'))}<input name="avatar_group_id" maxlength="160" value="{_escape(profile_fields.heygen_avatar_group_id or '')}" /></label>
    <label>{_escape(ui_text(locale, 'Avatar / Look ID', 'Avatar / Look ID'))}<input name="avatar_look_id" maxlength="160" value="{_escape(profile_fields.heygen_avatar_look_id or '')}" /></label>
    <label>{_escape(ui_text(locale, '中文 Voice ID（仅文本 voice fallback）', 'Chinese Voice ID (text-voice fallback only)'))}<input name="zh_voice_id" maxlength="160" value="{_escape(profile_fields.heygen_zh_voice_id or '')}" /></label>
    <label>{_escape(ui_text(locale, '英文 Voice ID（仅文本 voice fallback）', 'English Voice ID (text-voice fallback only)'))}<input name="en_voice_id" maxlength="160" value="{_escape(profile_fields.heygen_en_voice_id or '')}" /></label>
    <div class="button-row"><button type="submit">{_escape(ui_text(locale, '保存人物配置', 'Save presenter profile'))}</button><button class="secondary" type="button" disabled>{_escape(ui_text(locale, '测试 Avatar · 需先预估费用', 'Test Avatar · cost preview required'))}</button></div>
  </form>
  <p>{_escape(ui_text(locale, '默认路径：本地 Qwen voice → 音频 → HeyGen lip-sync/avatar。手工导出/导入路径继续保留，且 API 成本为 $0。', 'Default path: local Qwen voice → audio → HeyGen lip-sync/avatar. Manual export/import remains available at $0 API cost.'))}</p>
</section>"""
    script = f"""
const setup=document.getElementById('heygen-key-setup');
if(setup) setup.addEventListener('submit',(event)=>{{
  event.preventDefault();
  const status=document.getElementById('heygen-key-status');
  const key=document.getElementById('heygen-api-key');
  const bridge=window.webkit?.messageHandlers?.soloscaleCredentials;
  if(!bridge){{status.textContent={json.dumps(ui_text(locale, '请在 Desktop App 中完成设置。', 'Complete setup in the Desktop App.'))};return;}}
  if(!key.value.trim()){{status.textContent={json.dumps(ui_text(locale, '请输入 API key。', 'Enter an API key.'))};return;}}
  const secret=key.value; key.value='';
  bridge.postMessage({{action:'saveHeyGenKey',apiKey:secret,returnPath:{json.dumps(ui_url('/settings/media/heygen', locale, provider='saved'))}}});
}});
const remove=document.getElementById('delete-heygen-key');
if(remove) remove.addEventListener('click',()=>window.webkit.messageHandlers.soloscaleCredentials.postMessage({{action:'deleteHeyGenKey',returnPath:{json.dumps(ui_url('/settings/media/heygen', locale, provider='removed'))}}}));
"""
    return render_app_shell(
        active="advanced",
        locale=locale,
        current_url="/settings/media/heygen",
        title="SoloScale · HeyGen",
        eyebrow=ui_text(locale, "媒体设置", "Media settings"),
        heading=ui_text(locale, "只为真正需要的新人物片段付费。", "Pay only for genuinely new presenter footage."),
        description=ui_text(locale, "人物素材优先复用；凭据、调用与最终视频保持清晰边界。", "Reuse presenter assets first, with clear boundaries around credentials, calls, and final video."),
        body=body,
        script=script,
        extra_css="""
.setup-card{display:grid;gap:16px;max-width:820px;margin-top:18px;padding:24px;border:1px solid var(--border);border-radius:20px;background:linear-gradient(145deg,#fff,var(--brand-soft))}.setup-card h2,.setup-card p{margin:0}.setup-card form{display:grid;gap:14px}.button-row{display:flex;flex-wrap:wrap;gap:10px}.button-row button{width:auto}.button-row .secondary{background:var(--surface-subtle);color:var(--brand);border:1px solid var(--border)}.button-row .danger{background:#fff0ef;color:var(--danger)}.back-link{font-weight:800;text-decoration:none}
""",
    )


def _page(
    action_result: UIActionResult | None,
    data_root: Path,
    form: dict[str, str],
    locale: UILocale = DEFAULT_UI_LOCALE,
    provider_notice: str | None = None,
) -> str:
    query = _escape(form.get("query", ""))
    source_kind = form.get("source_kind", "")
    ai_preference = _load_ai_provider_preference(data_root)
    ai_provider_name = {
        ModelProviderId.SOLOSCALE_HOSTED: ui_text(
            locale, "SoloScale 托管 AI", "SoloScale Hosted AI"
        ),
        ModelProviderId.OLLAMA: ui_text(locale, "本地 AI", "Local AI"),
        ModelProviderId.OPENAI_COMPATIBLE: "OpenAI API",
    }[ai_preference.provider]
    result_section = (
        f'<section class="card full result-wrap"><h2>{_escape(ui_text(locale, "最近一次运行", "Latest run"))}</h2>{_result_card(action_result, locale)}</section>'
        if action_result is not None
        else ""
    )
    body = f"""<div class="advanced-grid">
    <section class="card tool-card">
      <span class="kicker">{_escape(ui_text(locale, '本地资料', 'Local knowledge'))}</span>
      <h2>{_escape(ui_text(locale, '检查资料库状态', 'Check knowledge status'))}</h2>
      <p class="tool-description">{_escape(ui_text(locale, '确认本机索引是否可用；这一步不会上传任何数据。', 'Confirm that the local index is available. Nothing is uploaded.'))}</p>
      <form method="post" action="/run">
        <input type="hidden" name="action" value="knowledge-status" />
        <button type="submit">{_escape(ui_text(locale, '检查状态', 'Check status'))}</button>
      </form>
      <a class="text-link" href="{ui_url('/evidence', locale)}">{_escape(ui_text(locale, '打开技术证据目录', 'Open technical evidence catalog'))}</a>
      <details class="technical-details">
        <summary>{_escape(ui_text(locale, '技术详情', 'Technical details'))}</summary>
        <p>{_escape(ui_text(locale, '本机私有数据目录', 'Private data directory'))}: <code>{_escape(str(data_root))}</code></p>
      </details>
    </section>

    <section class="card tool-card">
      <span class="kicker">{_escape(ui_text(locale, '工程概览', 'Engineering overview'))}</span>
      <h2>{_escape(ui_text(locale, '更新工程概览', 'Update engineering overview'))}</h2>
      <p class="tool-description">{_escape(ui_text(locale, '把已有工程证据整理成一页只读概览。', 'Turn existing engineering evidence into a read-only overview.'))}</p>
      <form method="post" action="/run">
        <input type="hidden" name="action" value="control-tower-build" />
        <button type="submit">{_escape(ui_text(locale, '生成概览', 'Generate overview'))}</button>
      </form>
      {_control_tower_section(data_root, locale)}
    </section>

    <section class="card full tool-card">
      <span class="kicker">{_escape(ui_text(locale, '本地搜索', 'Local search'))}</span>
      <h2>{_escape(ui_text(locale, '搜索本地证据', 'Search local evidence'))}</h2>
      <p class="tool-description">{_escape(ui_text(locale, '按关键词检查系统目前能找到哪些资料。', 'Check which local records the system can find for a keyword.'))}</p>
      <form method="post" action="/run">
        <input type="hidden" name="action" value="knowledge-search" />
        <label>{_escape(ui_text(locale, '搜索内容', 'Search query'))}
          <input name="query" value="{query}" />
        </label>
        <label>{_escape(ui_text(locale, '资料类型（可选）', 'Source type (optional)'))}
          <select name="source_kind">
            <option value="" {"selected" if source_kind == "" else ""}>{_escape(ui_text(locale, '全部来源', 'All sources'))}</option>
            <option value="codex_session"
              {"selected" if source_kind == "codex_session" else ""}>Codex</option>
            <option value="buildlog_run"
              {"selected" if source_kind == "buildlog_run" else ""}>BuildLog</option>
            <option value="chatgpt_export"
              {"selected" if source_kind == "chatgpt_export" else ""}
            >ChatGPT export</option>
          </select>
        </label>
        <button type="submit">{_escape(ui_text(locale, '开始搜索', 'Search'))}</button>
      </form>
    </section>

    <section class="card full tool-card provider-settings">
      <span class="kicker">{_escape(ui_text(locale, 'AI 服务', 'AI service'))}</span>
      <h2>{_escape(ui_text(locale, '当前 AI 服务（只读诊断）', 'Current AI service (read-only diagnostic)'))}</h2>
      <p class="tool-description">{_escape(ui_text(locale, '显示当前默认服务与模型。连接、模型和密钥设置集中在独立页面。', 'Shows the current default service and model. Connection, model, and credential setup live on a dedicated page.'))}</p>
      <div class="provider-option"><span><strong>{_escape(ai_provider_name)}</strong><small>{_escape(ai_preference.model)}</small></span></div>
    </section>

    <aside class="notice full">{_escape(ui_text(locale, '简历生成在“找到机会”页面。这里的证据结果只用于核对，不会自动写进简历。', 'Resume generation lives on the Get the job page. Evidence results here are for verification and are never inserted into a resume automatically.'))}</aside>
    {result_section}
  </div>"""
    locale_json = json.dumps(locale)
    return render_app_shell(
        active="advanced",
        locale=locale,
        current_url="/advanced",
        title=f"SoloScale · {ui_text(locale, '高级工具', 'Advanced Tools')}",
        eyebrow=ui_text(locale, "高级工具", "Advanced tools"),
        heading=ui_text(locale, "只读诊断工具，集中放在这里。", "Read-only diagnostics, together in one place."),
        description=ui_text(locale, "资料刷新与生成都在各自的工作页面；这里只保留检查、搜索与运行状态。", "Refreshing and generation live on their own pages. This page keeps checks, search, and runtime status only."),
        body=body,
        compact_hero=True,
        script=f"""
document.querySelectorAll('form[method="post"]').forEach(form=>{{
  if(!form.querySelector('input[name="ui_locale"]')){{
    const input=document.createElement('input');input.type='hidden';input.name='ui_locale';input.value={locale_json};form.append(input);
  }}
}});
""",
        extra_css="""
.advanced-grid{display:grid;gap:18px;grid-template-columns:repeat(2,minmax(0,1fr))}.advanced-grid .card{padding:24px}.full{grid-column:1/-1}.tool-card{display:flex;flex-direction:column;gap:11px}.tool-card h2,.result-wrap h2{margin:0;font-size:1.35rem;letter-spacing:-.02em}.tool-description{margin:0;color:var(--text-muted);max-width:760px}.tool-card form{gap:12px;margin-top:4px}.tool-card>form>button{justify-self:start;min-width:180px}.check-row,.provider-option{display:flex;align-items:flex-start;gap:11px;padding:13px 14px;border:1px solid var(--border);border-radius:13px;background:var(--surface-subtle);cursor:pointer}.check-row span,.provider-option span{display:grid;gap:2px}.check-row small,.provider-option small{color:var(--text-muted);font-weight:400}.provider-settings{background:linear-gradient(135deg,#fff,var(--brand-soft))}.technical-details{margin-top:4px;border-top:1px solid var(--border);padding-top:10px;color:var(--text-muted)}.technical-details summary,.legacy-tool>summary{cursor:pointer;color:var(--text);font-weight:750}.source-settings{display:grid;gap:12px}.source-settings[open]{padding-bottom:4px}.tool-state{margin:4px 0 0;padding:10px 12px;border-radius:11px;background:var(--surface-subtle);color:var(--text-muted)}.legacy-tool>summary{font-size:1.05rem}.legacy-tool[open]>summary{margin-bottom:12px}.legacy-tool form{margin-top:14px}.tool-result{padding:16px;border:1px solid var(--border);border-radius:14px}.tool-result h3{margin:9px 0 4px}.tool-result p{white-space:pre-wrap}.result-wrap{display:grid;gap:12px}.advanced-grid pre{max-height:320px;overflow:auto}.advanced-grid .success{border-color:#b9dfcf;color:var(--success)}.advanced-grid .error{border-color:#efc7c4;color:var(--danger)}
@media(max-width:760px){.advanced-grid{grid-template-columns:1fr}.full{grid-column:auto}.tool-card>form>button{width:100%}}
""",
    )


def _serve_control_tower(handler: BaseHTTPRequestHandler, data_root: Path) -> None:
    exists, document = _read_control_tower(data_root)
    if not exists:
        handler.send_error(404, "Control Tower not generated")
        return
    body = document.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _resume_run_artifact(data_root: Path, run_id: str, filename: str) -> Path | None:
    if _RUN_ID_RE.fullmatch(run_id) is None:
        return None
    runs_root = (data_root / "resume-runs").resolve()
    run_dir = (runs_root / run_id).resolve()
    target = run_dir / filename
    if run_dir.parent != runs_root or target.is_symlink() or not target.is_file():
        return None
    return target


def _serve_resume_download(
    handler: BaseHTTPRequestHandler,
    data_root: Path,
    run_id: str,
    *,
    desktop_mode: bool = False,
) -> None:
    # WKWebView owns the user download destination in Desktop mode. Returning a
    # second HTML confirmation body from a link marked `download` made WebKit save
    # that HTML as an additional fake DOCX download.
    del desktop_mode
    target = _resume_run_artifact(data_root, run_id, "08_resume.docx")
    if target is None:
        handler.send_error(404, "Resume not found")
        return
    run_dir = target.parent
    metadata = _load_json_file(run_dir / "09_user_ui.json") or {}
    filename = str(metadata.get("output_filename", "Tailored_Resume.docx"))
    safe_ascii = _safe_filename_component(Path(filename).stem, "Tailored_Resume") + ".docx"
    content = target.read_bytes()
    _record_resume_event(data_root, ResumeFunnelEventType.RESUME_EXPORTED, run_id=run_id)
    handler.send_response(200)
    handler.send_header("Content-Type", _DOCX_CONTENT_TYPE)
    handler.send_header("Content-Length", str(len(content)))
    handler.send_header(
        "Content-Disposition",
        f"attachment; filename=\"{safe_ascii}\"; filename*=UTF-8''{urllib.parse.quote(filename)}",
    )
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(content)


def _serve_resume_preview(handler: BaseHTTPRequestHandler, data_root: Path, run_id: str) -> None:
    target = _resume_run_artifact(data_root, run_id, "10_resume_preview.pdf")
    if target is None:
        handler.send_error(404, "Resume preview not found")
        return
    content = target.read_bytes()
    _record_resume_event(data_root, ResumeFunnelEventType.PREVIEW_VIEWED, run_id=run_id)
    handler.send_response(200)
    handler.send_header("Content-Type", "application/pdf")
    handler.send_header("Content-Length", str(len(content)))
    handler.send_header("Content-Disposition", 'inline; filename="resume-preview.pdf"')
    handler.send_header("Cache-Control", "private, no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(content)


def _serve_learning_source(
    handler: BaseHTTPRequestHandler,
    data_root: Path,
    repo_root: Path | None,
    locale: UILocale,
) -> None:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    run_id = query.get("run_id", [""])[0]
    anchor_id = query.get("anchor_id", [""])[0]
    try:
        if repo_root is None:
            raise ValueError("select a connected project to open code evidence")
        title, excerpt = _learning_source_excerpt(
            data_root,
            repo_root,
            run_id,
            anchor_id,
        )
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        handler.send_error(404, str(exc))
        return
    document = f"""<!doctype html><html lang="{locale}"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>{_escape(title)}</title><style>
body{{margin:0;padding:24px;background:#07111f;color:#e5edf7;font-family:ui-monospace,monospace}}
a{{color:#93c5fd}} pre{{white-space:pre;overflow:auto;background:#111827;border:1px solid #334155;
border-radius:12px;padding:18px;line-height:1.55}}</style></head><body>
<p><a href="{ui_url('/learning', locale)}">← {_escape(ui_text(locale, '学习工作台', 'Learning Workspace'))}</a></p><h1>{_escape(title)}</h1>
<p>{_escape(ui_text(locale, '来自已记录锚点的只读有限摘录。', 'Read-only bounded excerpt from a recorded anchor.'))}</p>
<pre>{_escape(excerpt)}</pre></body></html>"""
    body = document.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class SoloScaleLocalUIHandler(BaseHTTPRequestHandler):
    ui_data_root: Path = Path(".soloscale")
    repo_root: Path = _repo_root()
    creator_video_root: Path = resolve_runtime_paths().resource_root
    workspace_root: Path | None = None
    pending_chatgpt_export: Path | None = None
    desktop_session_token: str | None = None
    desktop_expected_host: str | None = None
    desktop_origin: str | None = None
    desktop_pid: int | None = None
    desktop_session_cookie: str | None = None
    desktop_bootstrap_consumed: bool = False
    ui_locale: UILocale = DEFAULT_UI_LOCALE
    latest_form: dict[str, str] = {}
    latest_user_form: dict[str, str] = {}
    latest_learning_form: dict[str, str] = {}
    latest_content_form: dict[str, str] = {}
    latest_resume_template_receipt: ResumeTemplateReceipt | None = None
    resume_job_manager: ResumeJobManager | None = None
    video_story_job_manager: LocalVideoJobManager | None = None
    creator_video_job_manager: CreatorVideoJobManager | None = None
    codex_import_job_manager: CodexImportJobManager | None = None
    youtube_job_manager: YouTubePublishingJobManager | None = None
    creator_production_job_manager: CreatorProductionJobManager | None = None

    def log_message(self, format: str, *args: object) -> None:
        if self.desktop_session_token is not None:
            return
        super().log_message(format, *args)

    def _video_data_root(self) -> Path:
        return self.ui_data_root.absolute() / "video"

    def _adopt_ui_locale(self, form: dict[str, str]) -> None:
        value = form.get("ui_locale")
        if value is not None:
            self.ui_locale = normalize_ui_locale(value)

    def _send_desktop_denied(self) -> None:
        body = b"Desktop session authorization required"
        self.send_response(403)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _desktop_host_allowed(self) -> bool:
        expected = self.desktop_expected_host
        if expected is None:
            return True
        host = self._single_header("Host")
        if host is not None and hmac.compare_digest(host, expected):
            return True
        self._send_desktop_denied()
        return False

    def _desktop_session_allowed(self) -> bool:
        if self.desktop_session_token is None:
            return True
        raw_cookie = self.headers.get("Cookie", "")
        try:
            cookies = SimpleCookie(raw_cookie)
        except CookieError:
            cookies = SimpleCookie()
        session = cookies.get(_DESKTOP_COOKIE_NAME)
        expected_cookie = self.desktop_session_cookie
        if (
            session is not None
            and expected_cookie is not None
            and hmac.compare_digest(session.value, expected_cookie)
        ):
            return True
        self._send_desktop_denied()
        return False

    def _single_header(self, name: str) -> str | None:
        values = self.headers.get_all(name, failobj=[])
        return values[0] if len(values) == 1 else None

    def _handle_desktop_bootstrap(self) -> None:
        token = self.desktop_session_token
        origin = self.desktop_origin
        pid = self.desktop_pid
        handler = type(self)
        nonce = self._single_header(_DESKTOP_NONCE_HEADER)
        proof = self._single_header(_DESKTOP_PROOF_HEADER)
        if (
            token is None
            or origin is None
            or pid is None
            or handler.desktop_bootstrap_consumed
            or self.path != _DESKTOP_BOOTSTRAP_PATH
            or self._single_header("Content-Length") != "0"
            or nonce is None
            or _DESKTOP_NONCE_RE.fullmatch(nonce) is None
            or proof is None
            or not hmac.compare_digest(
                proof,
                _desktop_bootstrap_request_proof(
                    token=token, url=origin, pid=pid, nonce=nonce
                ),
            )
        ):
            self._send_desktop_denied()
            return

        cookie = _desktop_session_cookie(
            token=token, url=origin, pid=pid, nonce=nonce
        )
        response_proof = _desktop_bootstrap_response_proof(
            token=token, url=origin, pid=pid, nonce=nonce, cookie=cookie
        )
        handler.desktop_session_cookie = cookie
        handler.desktop_bootstrap_consumed = True
        self.send_response(200)
        self.send_header(_DESKTOP_NONCE_HEADER, nonce)
        self.send_header(_DESKTOP_PROOF_HEADER, response_proof)
        self.send_header(
            "Set-Cookie",
            f"{_DESKTOP_COOKIE_NAME}={cookie}; Path=/; HttpOnly; SameSite=Strict",
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_advanced_page(
        self, result: UIActionResult | None, provider_notice: str | None = None
    ) -> None:
        data_root = self.ui_data_root.absolute()
        display_form = dict(self.latest_form)
        preference = _load_ai_provider_preference(data_root)
        display_form["provider"] = preference.provider.value
        display_form["provider_model"] = preference.model
        page = _page(
            result,
            data_root,
            display_form,
            self.ui_locale,
            provider_notice=provider_notice,
        )
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_ai_settings_page(
        self,
        *,
        detail: str | None = None,
        outcome: str | None = None,
    ) -> None:
        notices = {
            "saved": ui_text(
                self.ui_locale,
                "已保存为默认 AI 服务。简历和内容会自动使用它。",
                "Saved as the default AI service. Resume and Content now use it automatically.",
            ),
            "prepared": ui_text(
                self.ui_locale,
                "模型设置已保存，正在交给 macOS 安全保存密钥。",
                "Model settings are saved. macOS is now storing the key securely.",
            ),
            "removed": ui_text(
                self.ui_locale,
                "OpenAI API key 已从 macOS Keychain 移除。",
                "The OpenAI API key was removed from macOS Keychain.",
            ),
            "ready": ui_text(
                self.ui_locale,
                "连接测试通过，可以设为默认服务。",
                "Connection test passed. This service can be used as the default.",
            ),
            "not-ready": ui_text(
                self.ui_locale,
                "服务尚未准备好；请先完成页面上标出的步骤。",
                "This service is not ready yet. Complete the steps shown on this page first.",
            ),
            "starting": ui_text(
                self.ui_locale,
                "已请求启动 Ollama。等待几秒后再测试。",
                "Ollama was asked to start. Wait a few seconds, then test again.",
            ),
            "download-started": ui_text(
                self.ui_locale,
                "模型下载已开始。完成后再点一次测试。",
                "The model download has started. Test again after it completes.",
            ),
            "download-unavailable": ui_text(
                self.ui_locale,
                "找不到可用的 Ollama 命令；请先启动或重新安装 Ollama。",
                "The Ollama command is unavailable. Start or reinstall Ollama first.",
            ),
            "unavailable": ui_text(
                self.ui_locale,
                "这个服务当前不可用；默认选择没有改变。",
                "This service is currently unavailable. The default selection was not changed.",
            ),
            "not-configured": ui_text(
                self.ui_locale,
                "请先在 Desktop App 中保存 API key。",
                "Save an API key in the Desktop App first.",
            ),
            "unauthorized": ui_text(
                self.ui_locale,
                "OpenAI 拒绝了这个密钥；请更换后重试。",
                "OpenAI rejected this key. Replace it and try again.",
            ),
            "model-unavailable": ui_text(
                self.ui_locale,
                "这个 OpenAI 模型对当前项目不可用。",
                "This OpenAI model is not available to the current project.",
            ),
            "test-failed": ui_text(
                self.ui_locale,
                "连接测试未完成；密钥和默认选择保持不变。",
                "The connection test did not complete. The key and default selection are unchanged.",
            ),
            "invalid": ui_text(
                self.ui_locale,
                "设置无效，没有保存任何更改。",
                "The settings are invalid. No changes were saved.",
            ),
            "moved": ui_text(
                self.ui_locale,
                "AI 服务现在在这个页面集中管理。",
                "AI services are now managed from this page.",
            ),
        }
        page = _ai_settings_page(
            self.ui_data_root.absolute(),
            locale=self.ui_locale,
            detail=detail,
            notice=notices.get(outcome or ""),
            desktop_mode=self.desktop_session_token is not None,
        )
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_home_page(self) -> None:
        manager = self.resume_job_manager
        body = _home_page(
            self.ui_locale,
            data_root=self.ui_data_root.absolute(),
            workspace_root=self.workspace_root,
            resume_job=manager.latest() if manager is not None else None,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_creator_accounts_page(
        self,
        notice: str | None = None,
        *,
        youtube_job_id: str | None = None,
        auth_platform: str | None = None,
        auth_attempt_id: str | None = None,
    ) -> None:
        manager = self.youtube_job_manager
        youtube_job = (
            manager.get(self.ui_data_root.absolute(), youtube_job_id)
            if manager is not None and youtube_job_id
            else None
        )
        auth_attempt = None
        if (
            auth_platform in {"x", "linkedin", "douyin"}
            and auth_attempt_id
        ):
            try:
                auth_attempt = load_authorization_attempt(
                    self.ui_data_root.absolute(),
                    cast(Any, auth_platform),
                    auth_attempt_id,
                )
            except PlatformAccountError:
                auth_attempt = None
        body = creator_accounts_page(
            self.ui_data_root.absolute(),
            locale=self.ui_locale,
            notice=notice,
            youtube_job=youtube_job,
            auth_attempt=auth_attempt,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_user_page(
        self,
        result: UIActionResult | None,
        *,
        resume_job: ResumeJobSnapshot | None = None,
    ) -> None:
        data_root = self.ui_data_root.absolute()
        page = _user_page(
            result,
            data_root,
            self.latest_user_form,
            self.ui_locale,
            desktop_mode=self.desktop_session_token is not None,
            workspace_root=self.workspace_root,
            resume_job=resume_job,
            template_receipt=self.latest_resume_template_receipt,
        )
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_resume_job_page(self, job_id: str) -> None:
        manager = self.resume_job_manager
        snapshot = manager.get(job_id) if manager is not None else None
        if snapshot is None:
            self.send_error(404, "Resume job not found")
            return
        self._send_user_page(snapshot.result, resume_job=snapshot)

    def _send_learning_page(
        self, result: UIActionResult | None, response_saved_stage: str | None = None
    ) -> None:
        data_root = self.ui_data_root.absolute()
        display_form = dict(self.latest_learning_form)
        if response_saved_stage is not None:
            display_form["response_saved_stage"] = response_saved_stage
        page = _learning_page(
            data_root, self.workspace_root, display_form, result, self.ui_locale
        )
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_content_page(
        self,
        *,
        run_id: str | None = None,
        result: ContentFormResult | None = None,
        error: str | None = None,
        notice: str | None = None,
        scan_range: str | None = None,
        candidate_id: str | None = None,
        workspace_view: Literal["all", "stories", "create"] = "all",
        canon_story_id: str | None = None,
        canon_format: str | None = None,
        creator_job: CreatorProductionJob | None = None,
    ) -> None:
        video_snapshot = (
            self.creator_video_job_manager.get(run_id)
            if self.creator_video_job_manager is not None and run_id is not None
            else None
        )
        page = content_page(
            data_root=self.ui_data_root.absolute(),
            form=self.latest_content_form,
            run_id=run_id,
            error=error or (result.error if result is not None else None),
            notice=(
                notice
                or result.message
                if result is not None and result.error is None
                else notice
            ),
            locale=self.ui_locale,
            creator_video_available=creator_video_runtime_available(
                self.creator_video_root
            ),
            repository_root=self.workspace_root,
            scan_range=scan_range,
            candidate_id=candidate_id,
            creator_video_phase=(video_snapshot.phase if video_snapshot else None),
            creator_video_error=(video_snapshot.error if video_snapshot else None),
            workspace_view=workspace_view,
            canon_story_id=canon_story_id,
            canon_format=canon_format,
            creator_job=creator_job,
        )
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_evidence_page(self) -> None:
        body = evidence_page(
            self.ui_data_root.absolute(), self.ui_locale
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_work_page(self, query: dict[str, list[str]] | None = None) -> None:
        values = query or {}
        notice_code = values.get("notice", [""])[0]
        error_code = values.get("error", [""])[0]
        return_path = values.get("return_path", [""])[0]
        if (
            not return_path
            or not return_path.startswith("/")
            or return_path.startswith("//")
        ):
            return_path = "/"
        try:
            added = max(0, int(values.get("added", ["0"])[0]))
        except ValueError:
            added = 0
        codex_job = None
        manager = self.codex_import_job_manager
        requested_job_id = values.get("codex_job", [""])[0]
        if manager is not None:
            codex_job = (
                manager.get(self.ui_data_root.absolute(), requested_job_id)
                if requested_job_id
                else manager.latest(self.ui_data_root.absolute())
            )
        if codex_job is not None and requested_job_id:
            if codex_job.phase == "READY":
                notice_code = (
                    "codex-partial" if codex_job.failed else "codex-added"
                )
                added = codex_job.imported + codex_job.updated
            elif codex_job.phase == "FAILED":
                error_code = "codex-import-failed"
        notices = {
            "project-connected": ui_text(
                self.ui_locale,
                "本地 Git 项目已连接。下一步请在项目卡片中准备安全快照。",
                "Your local Git project is connected. Next, prepare its safe snapshot in the project card.",
            ),
            "chatgpt-selected": ui_text(
                self.ui_locale,
                "ChatGPT 导出文件已选择。勾选一次性授权后即可导入。",
                "Your ChatGPT export is selected. Approve the one-time import to continue.",
            ),
            "codex-added": ui_text(
                self.ui_locale,
                f"Codex 资料已添加：{added} 份新增或更新记录。",
                f"Codex work added: {added} new or updated records.",
            ),
            "codex-partial": ui_text(
                self.ui_locale,
                f"Codex 资料已更新：{added} 份新增或更新记录；个别会话需要重试。",
                f"Codex work updated: {added} new or updated records; some sessions need attention.",
            ),
            "chatgpt-added": ui_text(
                self.ui_locale,
                f"ChatGPT 资料已添加：{added} 份新增或更新记录。",
                f"ChatGPT work added: {added} new or updated records.",
            ),
            "refreshed": ui_text(
                self.ui_locale,
                "已更新你明确选择的工作资料。",
                "Your explicitly selected work has been refreshed.",
            ),
            "github-disconnected": ui_text(
                self.ui_locale,
                "GitHub 已断开；Keychain 中的访问令牌和本地仓库选择已移除。",
                "GitHub is disconnected. Its Keychain token and local repository selection were removed.",
            ),
            "github-selection-saved": ui_text(
                self.ui_locale,
                "GitHub 仓库选择已保存。现在可以手动刷新只读证据。",
                "Your GitHub repository selection is saved. You can now refresh read-only Evidence.",
            ),
            "github-refreshed": ui_text(
                self.ui_locale,
                "已从所选 GitHub 仓库更新只读元数据。",
                "Read-only metadata was refreshed from the selected GitHub repositories.",
            ),
        }
        errors = {
            "approval-required": ui_text(
                self.ui_locale,
                "请先勾选授权，再读取你选择的资料。",
                "Approve this one-time import before SoloScale reads the selected source.",
            ),
            "codex-import-failed": ui_text(
                self.ui_locale,
                "Codex 资料未能安全添加；原始资料没有被修改。",
                "Codex work could not be added safely; the source was not modified.",
            ),
            "chatgpt-import-failed": ui_text(
                self.ui_locale,
                "ChatGPT 导出未能安全添加；原始文件没有被修改。",
                "The ChatGPT export could not be added safely; the source file was not modified.",
            ),
            "chatgpt-not-selected": ui_text(
                self.ui_locale,
                "请先从系统选择器选择一个 ChatGPT JSON 或 ZIP 导出文件。",
                "Choose a ChatGPT JSON or ZIP export with the system picker first.",
            ),
            "refresh-failed": ui_text(
                self.ui_locale,
                "资料更新未完成；已有资料保持不变。",
                "The refresh did not complete; existing work remains unchanged.",
            ),
            "upload-too-large": ui_text(
                self.ui_locale,
                "选择的导出文件超过 256 MB 限制。",
                "The selected export exceeds the 256 MB limit.",
            ),
            "github-not-connected": ui_text(
                self.ui_locale,
                "请先通过 Desktop App 连接 GitHub。",
                "Connect GitHub through the Desktop App first.",
            ),
            "github-read-failed": ui_text(
                self.ui_locale,
                "GitHub 只读数据未能加载；现有本地证据保持不变。",
                "GitHub read-only data could not be loaded; existing local Evidence is unchanged.",
            ),
            "github-selection-invalid": ui_text(
                self.ui_locale,
                "仓库选择无效或超过 20 个，未保存更改。",
                "The repository selection is invalid or exceeds 20; no change was saved.",
            ),
        }
        body = work_page(
            data_root=self.ui_data_root.absolute(),
            workspace_root=self.workspace_root,
            locale=self.ui_locale,
            desktop_mode=self.desktop_session_token is not None,
            github_token_configured=github_access_token_is_configured(),
            github_connect_available=(
                self.desktop_session_token is not None
                and os.environ.get("SOLOSCALE_GITHUB_CONNECT_AVAILABLE") == "1"
            ),
            chatgpt_export_selected=self.pending_chatgpt_export is not None,
            codex_job=codex_job,
            notice=notices.get(notice_code),
            error=errors.get(error_code),
            return_path=return_path,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_github_repositories_page(
        self, query: dict[str, list[str]] | None = None
    ) -> None:
        token = github_access_token()
        if token is None or self.desktop_session_token is None:
            self.send_response(303)
            self.send_header(
                "Location",
                ui_url("/work", self.ui_locale, error="github-not-connected"),
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        store = GitHubConnectionStore(self.ui_data_root.absolute())
        saved_notice: str | None = None
        state: GitHubConnectionState | None = None
        try:
            account_id, account_login, repositories = GitHubReadOnlyClient(
                token
            ).discover()
            state = store.save_inventory(
                account_id=account_id,
                account_login=account_login,
                repositories=repositories,
            )
        except (GitHubConnectError, OSError, ValueError):
            try:
                state = store.load()
            except (GitHubConnectError, OSError, ValueError):
                state = None
            if state is None:
                self.send_response(303)
                self.send_header(
                    "Location",
                    ui_url("/work", self.ui_locale, error="github-read-failed"),
                )
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            saved_notice = ui_text(
                self.ui_locale,
                "GitHub 当前无法刷新；正在显示上次保存的仓库列表。",
                "GitHub could not refresh now; showing the last saved repository list.",
            )
        if (query or {}).get("notice", [""])[0] == "selection-saved":
            saved_notice = ui_text(
                self.ui_locale,
                "仓库选择已保存。",
                "Repository selection saved.",
            )
        if state is None:
            raise RuntimeError("GitHub repository state is unavailable")
        body = github_repositories_page(
            state,
            locale=self.ui_locale,
            notice=saved_notice,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_video_page(
        self,
        job_id: str | None = None,
        error: str | None = None,
        local_job_id: str | None = None,
        content_run_id: str | None = None,
    ) -> None:
        local_job = None
        manager = self.video_story_job_manager
        if manager is not None:
            local_job = (
                manager.get(self.ui_data_root.absolute(), local_job_id)
                if local_job_id is not None
                else manager.latest(self.ui_data_root.absolute())
            )
        local_available = (
            (self.repo_root / "video_factory" / "render.mjs").is_file()
            and (self.repo_root / "video_factory" / "node_modules").is_dir()
        )
        try:
            page = _video_page(
                self._video_data_root(),
                job_id,
                error,
                self.ui_locale,
                local_job=local_job,
                local_video_available=local_available,
                content_run_id=content_run_id,
                content_data_root=self.ui_data_root.absolute(),
            )
        except VideoGenerationError:
            page = _video_page(
                self._video_data_root(),
                None,
                ui_text(
                    self.ui_locale,
                    "这份视频任务不可用。",
                    "Video job is unavailable.",
                ),
                self.ui_locale,
                local_job=local_job,
                local_video_available=local_available,
                content_run_id=content_run_id,
                content_data_root=self.ui_data_root.absolute(),
            )
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        split = urllib.parse.urlsplit(self.path)
        path = split.path
        if not self._desktop_host_allowed():
            return
        if path == "/health" and self.desktop_session_token is None:
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if not self._desktop_session_allowed():
            return
        if path == "/health":
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        query = urllib.parse.parse_qs(split.query)
        if "lang" in query:
            self.ui_locale = normalize_ui_locale(query.get("lang", [""])[0])
        if path in {"/", ""}:
            self._send_home_page()
            return
        codex_import_job_match = re.fullmatch(
            r"/work/import-codex/jobs/(codex-import-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{10})",
            path,
        )
        if codex_import_job_match is not None:
            manager = self.codex_import_job_manager
            snapshot = (
                manager.get(
                    self.ui_data_root.absolute(),
                    codex_import_job_match.group(1),
                )
                if manager is not None
                else None
            )
            if snapshot is None:
                self.send_error(404, "Codex import job not found")
                return
            body = json.dumps(
                {
                    "job_id": snapshot.job_id,
                    "phase": snapshot.phase,
                    "total": snapshot.total,
                    "processed": snapshot.processed,
                    "running": snapshot.running,
                    "imported": snapshot.imported,
                    "updated": snapshot.updated,
                    "skipped": snapshot.skipped,
                    "failed": snapshot.failed,
                    "failure_codes": list(snapshot.failure_codes),
                    "created_at": snapshot.created_at,
                    "updated_at": snapshot.updated_at,
                },
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/work":
            self._send_work_page(query)
            return
        if path == "/work/github":
            self._send_github_repositories_page(query)
            return
        resume_job_match = re.fullmatch(
            r"/resume/jobs/(resume-job-[a-f0-9]{12})", path
        )
        if resume_job_match is not None:
            self._send_resume_job_page(resume_job_match.group(1))
            return
        if path == "/resume":
            _apply_ai_provider_preference(
                self.latest_user_form, self.ui_data_root.absolute()
            )
            preview_id = query.get("template_preview", [""])[0]
            if preview_id:
                self.latest_user_form["resume_template_preview_id"] = preview_id
                self.latest_user_form.pop("template_preview_error", None)
            self._send_user_page(None)
            return
        ai_settings_detail = {
            "/settings/ai": None,
            "/settings/ai/local": "local",
            "/settings/ai/openai": "openai",
            "/settings/ai/hosted": "hosted",
        }
        if path in ai_settings_detail:
            self._send_ai_settings_page(
                detail=ai_settings_detail[path],
                outcome=query.get("provider", [""])[0],
            )
            return
        if path == "/settings/media/heygen":
            page = _heygen_settings_page(
                self.ui_data_root.absolute(),
                locale=self.ui_locale,
                outcome=query.get("provider", [""])[0],
                desktop_mode=self.desktop_session_token is not None,
            )
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/advanced":
            provider_notice = {
                "saved": ui_text(
                    self.ui_locale,
                    "已保存。简历和内容现在会使用这个 AI 服务。",
                    "Saved. Resume and Content will now use this AI service.",
                ),
                "save-failed": ui_text(
                    self.ui_locale,
                    "无法保存这个 AI 服务。请检查本地模型名称后重试。",
                    "This AI service could not be saved. Check the local model name and try again.",
                ),
            }.get(query.get("provider", [""])[0])
            self._send_advanced_page(None, provider_notice)
            return
        if path == "/learning":
            self.latest_learning_form = {key: values[0] for key, values in query.items() if values}
            saved_stage = query.get("response_saved", [""])[0]
            self._send_learning_page(
                None, saved_stage if saved_stage in {"explain", "trace"} else None
            )
            return
        if path == "/creator":
            body = creator_overview_page(
                self.ui_data_root.absolute(), locale=self.ui_locale
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/creator/stories":
            mining_notice = query.get("mining_notice", [""])[0]
            notice = None
            if mining_notice == "ok":
                notice = ui_text(
                    self.ui_locale,
                    "已从最新工作生成候选故事（确定性草稿，未调用模型）。",
                    "Generated candidate stories from recent work (deterministic drafts; no model call).",
                )
            elif mining_notice == "empty":
                notice = ui_text(
                    self.ui_locale,
                    "扫描完成，没有发现新的可用故事。",
                    "Scan complete; no new usable stories were found.",
                )
            elif mining_notice == "error":
                notice = ui_text(
                    self.ui_locale,
                    "故事挖掘未能完成；请检查已批准的工作证据后重试。",
                    "Story mining could not complete; check approved work evidence and try again.",
                )
            self._send_content_page(workspace_view="stories", notice=notice)
            return
        if path == "/creator/create":
            _apply_ai_provider_preference(
                self.latest_content_form, self.ui_data_root.absolute()
            )
            creator_job_id = query.get("creator_job", [""])[0]
            production_manager = self.creator_production_job_manager
            creator_job = (
                production_manager.get(
                    self.ui_data_root.absolute(), creator_job_id
                )
                if production_manager is not None and creator_job_id
                else None
            )
            self._send_content_page(
                run_id=query.get("run_id", [""])[0] or None,
                scan_range=query.get("scan_range", [None])[0],
                candidate_id=query.get("candidate_id", [None])[0],
                workspace_view="create",
                canon_story_id=query.get("canon_story_id", [None])[0],
                canon_format=query.get("canon_format", [None])[0],
                creator_job=creator_job,
            )
            return
        if path == "/creator/publish":
            youtube_manager = self.youtube_job_manager
            requested_youtube_job = query.get("youtube_job", [""])[0]
            youtube_job = (
                youtube_manager.get(
                    self.ui_data_root.absolute(), requested_youtube_job
                )
                if youtube_manager is not None and requested_youtube_job
                else youtube_manager.latest(
                    self.ui_data_root.absolute(), kind="upload"
                )
                if youtube_manager is not None
                else None
            )
            body = editorial_publishing_page(
                data_root=self.ui_data_root.absolute(),
                locale=self.ui_locale,
                creator_mode=True,
                youtube_job=youtube_job,
                selected_run_id=query.get("run_id", [""])[0] or None,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/creator/history":
            body = creator_history_page(
                self.ui_data_root.absolute(), locale=self.ui_locale
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/content":
            _apply_ai_provider_preference(
                self.latest_content_form, self.ui_data_root.absolute()
            )
            run_id = query.get("run_id", [""])[0]
            reference_id = query.get("reference_id", [""])[0]
            if reference_id:
                self.latest_content_form["reference_id"] = reference_id
            review_notice = {
                "saved": ui_text(self.ui_locale, "修改已保存。", "Edits saved."),
                "approved": ui_text(
                    self.ui_locale,
                    "内容包已批准；仍需在发布队列的精确预览中输入 PUBLISH 才会发布。",
                    "Bundle approved. Publication still requires PUBLISH in the exact Publish Queue preview.",
                ),
                "rejected": ui_text(
                    self.ui_locale, "内容包已拒绝；没有发布。", "Bundle rejected; nothing was published."
                ),
                "regenerated": ui_text(
                    self.ui_locale,
                    "已用安全离线模板重新生成所选适配。",
                    "The selected adaptation was regenerated with the safe offline template.",
                ),
            }.get(query.get("review", [""])[0])
            reference_notice = (
                ui_text(
                    self.ui_locale,
                    "参考视频已在本机分析完成，并已选入当前内容表单。",
                    "The reference video was analyzed locally and selected for this content form.",
                )
                if query.get("reference", [""])[0] == "analyzed"
                else None
            )
            self._send_content_page(
                run_id=run_id or None,
                notice=review_notice or reference_notice,
                scan_range=query.get("scan_range", [None])[0],
                candidate_id=query.get("candidate_id", [None])[0],
                workspace_view="create",
            )
            return
        if path == "/creator/accounts":
            account_notice = {
                "saved": ui_text(
                    self.ui_locale, "账号入口已保存。", "Account entry saved."
                ),
                "invalid": ui_text(
                    self.ui_locale,
                    "无法保存；请检查网址和状态。",
                    "Could not save. Check the URLs and status.",
                ),
                "configured": ui_text(
                    self.ui_locale,
                    "开发者应用配置已保存在本机；密钥不会回显。",
                    "Developer integration saved locally; secrets are not displayed.",
                ),
                "config-invalid": ui_text(
                    self.ui_locale,
                    "开发者应用配置无效或不完整。",
                    "Developer integration configuration is invalid or incomplete.",
                ),
                "disconnected": ui_text(
                    self.ui_locale,
                    "账号连接已从本机移除。",
                    "The local account connection was removed.",
                ),
                "connected": ui_text(
                    self.ui_locale,
                    "账号身份已验证并安全保存在本机。",
                    "The account identity was verified and saved locally.",
                ),
            }.get(query.get("account", [""])[0])
            self._send_creator_accounts_page(
                account_notice,
                youtube_job_id=query.get("youtube_job", [""])[0] or None,
                auth_platform=query.get("auth_platform", [""])[0] or None,
                auth_attempt_id=query.get("auth_attempt", [""])[0] or None,
            )
            return
        youtube_job_match = re.fullmatch(
            r"/creator/youtube/jobs/(youtube-(?:auth|upload)-[a-f0-9]{12})",
            path,
        )
        if youtube_job_match is not None:
            manager = self.youtube_job_manager
            snapshot = (
                manager.get(
                    self.ui_data_root.absolute(), youtube_job_match.group(1)
                )
                if manager is not None
                else None
            )
            if snapshot is None:
                self.send_error(404, "YouTube job not found")
                return
            body = json.dumps(
                {
                    "job_id": snapshot.job_id,
                    "kind": snapshot.kind,
                    "phase": snapshot.phase,
                    "progress_percent": snapshot.progress_percent,
                    "channel_id": snapshot.channel_id,
                    "run_id": snapshot.run_id,
                    "video_id": snapshot.video_id,
                    "video_url": snapshot.video_url,
                    "error_code": snapshot.error_code,
                    "error_message": snapshot.error_message,
                    "updated_at": snapshot.updated_at,
                },
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/evidence":
            self._send_evidence_page()
            return
        if path == "/video":
            self._send_video_page(
                query.get("job_id", [None])[0],
                local_job_id=query.get("local_job_id", [None])[0],
                content_run_id=query.get("content_run_id", [None])[0],
            )
            return
        if path == "/publishing":
            self.send_response(303)
            self.send_header(
                "Location",
                ui_url(
                    "/creator/publish",
                    self.ui_locale,
                    run_id=query.get("run_id", [""])[0],
                ),
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        editorial_image_match = re.fullmatch(
            r"/publishing/editorial/(linkedin|x)/image", path
        )
        if editorial_image_match is not None:
            try:
                content = editorial_image_preview(
                    self.ui_data_root.absolute(),
                    cast(EditorialChannel, editorial_image_match.group(1)),
                )
            except (EditorialPublishingError, OSError):
                self.send_error(404, "Editorial image preview not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)
            return
        video_download_match = re.fullmatch(
            r"/video/downloads/(video-[a-f0-9]{12})/output\.mp4", path
        )
        if video_download_match is not None:
            try:
                job = load_job(self._video_data_root(), video_download_match.group(1))
                output = Path(job.output_path or "")
                if not output.is_file() or output.is_symlink():
                    raise VideoGenerationError("video output is unavailable")
                content = output.read_bytes()
            except (OSError, VideoGenerationError):
                self.send_error(404, "Video output not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Content-Disposition", 'attachment; filename="output.mp4"')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)
            return
        local_video_download_match = re.fullmatch(
            r"/video/local/downloads/(video-story-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{10})/([a-z]+)",
            path,
        )
        if local_video_download_match is not None:
            job_id, artifact = local_video_download_match.groups()
            try:
                output = local_video_artifact(self.ui_data_root.absolute(), job_id, artifact)
                content = output.read_bytes()
                filename = local_video_download_names()[artifact]
            except (OSError, KeyError, VideoStoryError):
                self.send_error(404, "Local video artifact not found")
                return
            content_type = {
                "video": "video/mp4",
                "subtitles": "application/x-subrip; charset=utf-8",
                "thumbnail": "image/png",
                "story": "text/markdown; charset=utf-8",
                "narration": "text/markdown; charset=utf-8",
                "manifest": "application/json",
                "receipt": "application/json",
            }.get(artifact, "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)
            return
        content_download_match = re.fullmatch(r"/content/downloads/([^/]+)/([^/]+)", path)
        if content_download_match is not None:
            try:
                filename, content = content_download(
                    self.ui_data_root.absolute(),
                    content_download_match.group(1),
                    content_download_match.group(2),
                )
            except ContentWorkspaceError:
                self.send_error(404, "Content artifact not found")
                return
            content_type = (
                "application/json"
                if filename.endswith(".json")
                else "video/mp4"
                if filename.endswith(".mp4")
                else "image/png"
                if filename.endswith(".png")
                else "text/markdown; charset=utf-8"
            )
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{content_download_match.group(2)}"',
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)
            return
        if path == "/learning/source":
            _serve_learning_source(
                self,
                self.ui_data_root.absolute(),
                self.workspace_root,
                self.ui_locale,
            )
            return
        if path == "/control-tower":
            _serve_control_tower(self, self.ui_data_root.absolute())
            return
        download_match = re.fullmatch(r"/downloads/([^/]+)/resume\.docx", path)
        if download_match is not None:
            _serve_resume_download(
                self,
                self.ui_data_root.absolute(),
                download_match.group(1),
                desktop_mode=self.desktop_session_token is not None,
            )
            return
        preview_match = re.fullmatch(r"/previews/([^/]+)/resume\.pdf", path)
        if preview_match is not None:
            _serve_resume_preview(self, self.ui_data_root.absolute(), preview_match.group(1))
            return
        self.send_error(404, "Not found")

    def do_POST(self) -> None:  # noqa: N802
        if not self._desktop_host_allowed():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == _DESKTOP_BOOTSTRAP_PATH:
            self._handle_desktop_bootstrap()
            return
        if not self._desktop_session_allowed():
            return
        resume_post_started = time.perf_counter() if path == "/generate" else None
        if path == "/creator/accounts/youtube/connect":
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > 8 * 1024:
                self.send_error(413, "YouTube connection request is too large")
                return
            form = _parse_form(self.rfile.read(length)) if length else {}
            self._adopt_ui_locale(form)
            manager = self.youtube_job_manager
            try:
                if manager is None:
                    raise YouTubePublishingError(
                        "YouTube background worker is unavailable",
                        code="WORKER_UNAVAILABLE",
                    )
                job = manager.start_connect(data_root=self.ui_data_root.absolute())
            except (OSError, YouTubePublishingError) as exc:
                self._send_creator_accounts_page(str(exc))
                return
            self.send_response(303)
            self.send_header(
                "Location",
                ui_url(
                    "/creator/accounts",
                    self.ui_locale,
                    youtube_job=job.job_id,
                ),
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/creator/accounts/youtube/cancel":
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > 8 * 1024:
                self.send_error(413, "YouTube cancellation request is too large")
                return
            form = _parse_form(self.rfile.read(length)) if length else {}
            self._adopt_ui_locale(form)
            manager = self.youtube_job_manager
            try:
                if manager is None:
                    raise YouTubePublishingError(
                        "YouTube background worker is unavailable",
                        code="WORKER_UNAVAILABLE",
                    )
                job_id = form.get("job_id", "")
                job = manager.cancel_connect(
                    data_root=self.ui_data_root.absolute(),
                    job_id=job_id,
                )
            except (OSError, YouTubePublishingError) as exc:
                self._send_creator_accounts_page(str(exc))
                return
            self.send_response(303)
            self.send_header(
                "Location",
                ui_url(
                    "/creator/accounts",
                    self.ui_locale,
                    youtube_job=job.job_id,
                ),
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/creator/accounts/configure":
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > 16 * 1024:
                self.send_error(413, "Developer configuration request is too large")
                return
            form = _parse_form(self.rfile.read(length))
            self._adopt_ui_locale(form)
            platform = form.get("platform", "")
            outcome = "config-invalid"
            try:
                if platform not in {"x", "linkedin", "douyin", "xiaohongshu"}:
                    raise PlatformAccountError("Unsupported platform")
                selected_platform = cast(Any, platform)
                existing = load_developer_config(
                    self.ui_data_root.absolute(), selected_platform
                )
                values: dict[str, str] = {}
                for field in PROVIDERS[selected_platform].required_fields:
                    submitted = form.get(field, "").strip()
                    if submitted:
                        values[field] = submitted
                    elif existing.source == "local" and existing.values.get(field):
                        values[field] = existing.values[field]
                save_developer_config(
                    self.ui_data_root.absolute(), selected_platform, values
                )
                outcome = "configured"
            except (OSError, PlatformAccountError):
                outcome = "config-invalid"
            self.send_response(303)
            self.send_header(
                "Location",
                ui_url("/creator/accounts", self.ui_locale, account=outcome),
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        provider_connect = re.fullmatch(
            r"/creator/accounts/(x|linkedin|douyin)/connect", path
        )
        if provider_connect is not None:
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > 8 * 1024:
                self.send_error(413, "Connection request is too large")
                return
            form = _parse_form(self.rfile.read(length)) if length else {}
            self._adopt_ui_locale(form)
            platform = cast(Any, provider_connect.group(1))
            try:
                attempt = start_authorization_attempt(
                    self.ui_data_root.absolute(), platform
                )
            except PlatformAccountError as exc:
                self._send_creator_accounts_page(str(exc))
                return
            self.send_response(303)
            self.send_header(
                "Location",
                ui_url(
                    "/creator/accounts",
                    self.ui_locale,
                    auth_platform=attempt.platform,
                    auth_attempt=attempt.attempt_id,
                ),
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/creator/accounts/auth/cancel":
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > 8 * 1024:
                self.send_error(413, "Cancellation request is too large")
                return
            form = _parse_form(self.rfile.read(length)) if length else {}
            self._adopt_ui_locale(form)
            platform = form.get("platform", "")
            attempt_id = form.get("attempt_id", "")
            try:
                if platform not in {"x", "linkedin", "douyin"}:
                    raise PlatformAccountError("Unsupported platform")
                attempt = cancel_authorization_attempt(
                    self.ui_data_root.absolute(), cast(Any, platform), attempt_id
                )
            except PlatformAccountError as exc:
                self._send_creator_accounts_page(str(exc))
                return
            self.send_response(303)
            self.send_header(
                "Location",
                ui_url(
                    "/creator/accounts",
                    self.ui_locale,
                    auth_platform=attempt.platform,
                    auth_attempt=attempt.attempt_id,
                ),
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/creator/accounts/auth/complete":
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > 16 * 1024:
                self.send_error(413, "Authorization response is too large")
                return
            form = _parse_form(self.rfile.read(length)) if length else {}
            self._adopt_ui_locale(form)
            platform = form.get("platform", "")
            attempt_id = form.get("attempt_id", "")
            try:
                if platform not in {"x", "linkedin", "douyin"}:
                    raise PlatformAccountError("Unsupported platform")
                complete_authorization_response(
                    self.ui_data_root.absolute(),
                    cast(Any, platform),
                    attempt_id,
                    form.get("authorization_response", ""),
                )
            except PlatformAccountError as exc:
                self._send_creator_accounts_page(
                    str(exc),
                    auth_platform=platform,
                    auth_attempt_id=attempt_id,
                )
                return
            self.send_response(303)
            self.send_header(
                "Location",
                ui_url("/creator/accounts", self.ui_locale, account="connected"),
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/creator/accounts/disconnect":
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > 8 * 1024:
                self.send_error(413, "Disconnect request is too large")
                return
            form = _parse_form(self.rfile.read(length)) if length else {}
            self._adopt_ui_locale(form)
            platform = form.get("platform", "")
            try:
                if platform not in {"x", "linkedin", "douyin", "xiaohongshu"}:
                    raise PlatformAccountError("Unsupported platform")
                disconnect_identity(
                    self.ui_data_root.absolute(),
                    cast(Any, platform),
                    form.get("external_account_id", ""),
                )
            except PlatformAccountError as exc:
                self._send_creator_accounts_page(str(exc))
                return
            self.send_response(303)
            self.send_header(
                "Location", ui_url("/creator/accounts", self.ui_locale, account="disconnected")
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/creator/accounts/save":
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > 16 * 1024:
                self.send_error(413, "Account settings request is too large")
                return
            form = _parse_form(self.rfile.read(length))
            self._adopt_ui_locale(form)
            forbidden = {"token", "secret", "password", "api_key", "oauth"}
            outcome = "invalid"
            if not forbidden.intersection(form) and form.get("platform") == "independent_site":
                try:
                    account = normalize_account(
                        platform=form.get("platform", ""),
                        display_name=form.get("display_name", ""),
                        handle=form.get("handle", ""),
                        profile_url=form.get("profile_url", ""),
                        admin_url=form.get("admin_url", ""),
                        status=form.get("status", "NOT_CONFIGURED"),
                    )
                    save_creator_account(self.ui_data_root.absolute(), account)
                    outcome = "saved"
                except (CreatorAccountError, OSError):
                    outcome = "invalid"
            self.send_response(303)
            self.send_header(
                "Location",
                ui_url("/creator/accounts", self.ui_locale, account=outcome),
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/settings/ai-provider":
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > 8 * 1024:
                self.send_error(413, "Settings request is too large")
                return
            form = _parse_form(self.rfile.read(length))
            self._adopt_ui_locale(form)
            if "api_key" in form or "openai_api_key" in form:
                self.send_error(400, "Credentials are not accepted by this endpoint")
                return
            self.send_response(303)
            self.send_header(
                "Location",
                ui_url("/settings/ai", self.ui_locale, provider="moved"),
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/settings/media/heygen":
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > 8 * 1024:
                self.send_error(413, "Settings request is too large")
                return
            form = _parse_form(self.rfile.read(length))
            self._adopt_ui_locale(form)
            if "api_key" in form or "heygen_api_key" in form:
                self.send_error(400, "Credentials are not accepted by this endpoint")
                return
            outcome = "invalid"
            action = form.get("action")
            if action == "save_profile":
                values = {
                    "heygen_avatar_group_id": form.get("avatar_group_id", "").strip(),
                    "heygen_avatar_look_id": form.get("avatar_look_id", "").strip(),
                    "heygen_zh_voice_id": form.get("zh_voice_id", "").strip(),
                    "heygen_en_voice_id": form.get("en_voice_id", "").strip(),
                }
                if all(
                    not value or re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value)
                    for value in values.values()
                ):
                    try:
                        profile = load_media_profile_settings(
                            self.ui_data_root.absolute()
                        ).model_copy(
                            update={
                                key: value or None for key, value in values.items()
                            }
                        )
                        save_media_profile(self.ui_data_root.absolute(), profile)
                    except (MediaProfileError, OSError, ValueError):
                        outcome = "invalid"
                    else:
                        outcome = "profile-saved"
            elif action == "save_budget":
                try:
                    current_policy = load_budget_policy(
                        self.ui_data_root.absolute()
                    )

                    def optional_amount(name: str) -> Decimal | None:
                        raw = form.get(name, "").strip()
                        if not raw:
                            return None
                        value = Decimal(raw)
                        if not value.is_finite() or value < 0:
                            raise ValueError("budget must be a non-negative amount")
                        return value

                    save_budget_policy(
                        self.ui_data_root.absolute(),
                        BudgetPolicy(
                            per_paid_operation_usd=optional_amount(
                                "per_paid_operation_usd"
                            ),
                            per_story_usd=optional_amount("per_story_usd"),
                            per_video_usd=optional_amount("per_video_usd"),
                            daily_usd=optional_amount("daily_usd"),
                            monthly_usd=optional_amount("monthly_usd"),
                            warning_ratio=current_policy.warning_ratio,
                        ),
                    )
                except (
                    InvalidOperation,
                    MediaCostError,
                    OSError,
                    ValueError,
                ):
                    outcome = "budget-invalid"
                else:
                    outcome = "budget-saved"
            self.send_response(303)
            self.send_header(
                "Location",
                ui_url(
                    "/settings/media/heygen",
                    self.ui_locale,
                    provider=outcome,
                ),
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path in {
            "/settings/ai/local",
            "/settings/ai/openai",
            "/settings/ai/hosted",
        }:
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > 8 * 1024:
                self.send_error(413, "Settings request is too large")
                return
            form = _parse_form(self.rfile.read(length))
            self._adopt_ui_locale(form)
            if "api_key" in form or "openai_api_key" in form:
                self.send_error(400, "Credentials are not accepted by this endpoint")
                return
            action = form.get("action", "")
            outcome = "invalid"
            data_root = self.ui_data_root.absolute()
            try:
                if path == "/settings/ai/local":
                    preference = _save_ai_provider_preference(
                        data_root,
                        provider=ModelProviderId.OLLAMA.value,
                        model=form.get("model", "qwen3:8b"),
                        ollama_url=form.get("ollama_url", _OLLAMA_DEFAULT_URL),
                        set_default=False,
                    )
                    readiness = _ollama_readiness(preference)
                    if action == "test":
                        outcome = "ready" if readiness.ready else "not-ready"
                    elif action == "start":
                        if not readiness.installed:
                            outcome = "not-ready"
                        else:
                            subprocess.Popen(
                                ["/usr/bin/open", "-a", "Ollama"],
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                close_fds=True,
                            )
                            outcome = "starting"
                    elif action == "download":
                        cli = _ollama_cli_path()
                        if not readiness.reachable or cli is None:
                            outcome = "download-unavailable"
                        else:
                            subprocess.Popen(
                                [cli, "pull", preference.ollama_model],
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                close_fds=True,
                            )
                            outcome = "download-started"
                    elif action == "use_default":
                        if readiness.ready:
                            _save_ai_provider_preference(
                                data_root,
                                provider=ModelProviderId.OLLAMA.value,
                                model=preference.ollama_model,
                                ollama_url=preference.ollama_url,
                            )
                            outcome = "saved"
                        else:
                            outcome = "not-ready"
                elif path == "/settings/ai/hosted":
                    hosted_gateway = model_gateway_for(
                        ModelProviderId.SOLOSCALE_HOSTED
                    )
                    hosted_ready = (
                        hosted_gateway.descriptor.configuration_state
                        is GatewayConfigurationState.CONFIGURED
                    )
                    if action == "test":
                        outcome = "ready" if hosted_ready else "unavailable"
                    elif action == "use_default" and hosted_ready:
                        _save_ai_provider_preference(
                            data_root,
                            provider=ModelProviderId.SOLOSCALE_HOSTED.value,
                        )
                        outcome = "saved"
                    elif action == "use_default":
                        outcome = "unavailable"
                else:
                    if action == "prepare":
                        if self.desktop_session_token is None:
                            outcome = "unavailable"
                        else:
                            _save_ai_provider_preference(
                                data_root,
                                provider=ModelProviderId.OPENAI_COMPATIBLE.value,
                                openai_model=form.get(
                                    "openai_model", _OPENAI_DEFAULT_MODEL
                                ),
                            )
                            outcome = "prepared"
                    elif action == "test":
                        preference = _save_ai_provider_preference(
                            data_root,
                            provider=ModelProviderId.OPENAI_COMPATIBLE.value,
                            openai_model=form.get(
                                "openai_model", _OPENAI_DEFAULT_MODEL
                            ),
                            set_default=False,
                        )
                        outcome = _openai_connection_status(preference)
                    elif action == "use_default":
                        if openai_api_key_is_configured():
                            _save_ai_provider_preference(
                                data_root,
                                provider=ModelProviderId.OPENAI_COMPATIBLE.value,
                                openai_model=form.get(
                                    "openai_model", _OPENAI_DEFAULT_MODEL
                                ),
                            )
                            outcome = "saved"
                        else:
                            outcome = "not-configured"
            except (
                OSError,
                ResumeWorkspaceStorageError,
                subprocess.SubprocessError,
                ValueError,
            ):
                outcome = "invalid"
            if path == "/settings/ai/openai" and action == "prepare":
                if outcome == "prepared":
                    self.send_response(204)
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            self.send_response(303)
            self.send_header(
                "Location",
                ui_url(path, self.ui_locale, provider=outcome),
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/resume/unlock-local-scan":
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length < 0 or length > 8 * 1024:
                self.send_error(413, "Request is too large")
                return
            form = _parse_form(self.rfile.read(length))
            self._adopt_ui_locale(form)
            _record_resume_event(
                self.ui_data_root.absolute(),
                ResumeFunnelEventType.UNLOCK_LOCAL_SCAN_CLICKED,
            )
            self.send_response(303)
            self.send_header("Location", ui_url("/work", self.ui_locale))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/evidence/refresh":
            try:
                receipt = refresh_evidence_catalog(
                    self.ui_data_root.absolute(), repository_root=self.repo_root
                )
            except (EvidenceHubError, OSError, ValueError):
                location = ui_url("/evidence", self.ui_locale, refresh="failed")
            else:
                location = (
                    ui_url("/evidence", self.ui_locale, refresh="complete")
                    if receipt.status.value == "succeeded"
                    else ui_url("/evidence", self.ui_locale, refresh="failed")
                )
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/work/import-codex":
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > 64 * 1024:
                self.send_error(413, "Import request is too large")
                return
            form = _parse_form(self.rfile.read(length))
            self._adopt_ui_locale(form)
            if form.get("approve") != "yes":
                location = ui_url(
                    "/work", self.ui_locale, error="approval-required"
                )
            else:
                try:
                    manager = self.codex_import_job_manager
                    if manager is None:
                        raise WorkContextError(
                            "Codex background import is unavailable."
                        )
                    job_id = manager.submit(
                        data_root=self.ui_data_root.absolute(),
                    )
                except (WorkContextError, OSError, ValueError):
                    location = ui_url(
                        "/work", self.ui_locale, error="codex-import-failed"
                    )
                else:
                    location = ui_url(
                        "/work",
                        self.ui_locale,
                        codex_job=job_id,
                    )
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/work/import-chatgpt":
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_WORK_IMPORT_BYTES:
                location = ui_url(
                    "/work", self.ui_locale, error="upload-too-large"
                )
                self.send_response(303)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            raw = self.rfile.read(length)
            try:
                submission = _parse_submission(
                    raw,
                    self.headers.get("Content-Type", ""),
                    max_bytes=MAX_WORK_IMPORT_BYTES,
                )
            except (UnicodeError, ValueError):
                location = ui_url(
                    "/work", self.ui_locale, error="chatgpt-import-failed"
                )
            else:
                self._adopt_ui_locale(submission.fields)
                if submission.fields.get("approve") != "yes":
                    location = ui_url(
                        "/work", self.ui_locale, error="approval-required"
                    )
                else:
                    pending = self.pending_chatgpt_export
                    type(self).pending_chatgpt_export = None
                    try:
                        if pending is not None:
                            chatgpt_result = import_chatgpt_export(
                                self.ui_data_root.absolute(), pending
                            )
                        else:
                            uploaded = submission.files.get("chatgpt_export")
                            if uploaded is None:
                                location = ui_url(
                                    "/work",
                                    self.ui_locale,
                                    error="chatgpt-not-selected",
                                )
                                chatgpt_result = None
                            else:
                                chatgpt_result = import_chatgpt_export_bytes(
                                    self.ui_data_root.absolute(),
                                    filename=uploaded.filename,
                                    content=uploaded.content,
                                )
                    except (WorkContextError, OSError, ValueError):
                        location = ui_url(
                            "/work",
                            self.ui_locale,
                            error="chatgpt-import-failed",
                        )
                    else:
                        if chatgpt_result is not None:
                            location = ui_url(
                                "/work",
                                self.ui_locale,
                                notice="chatgpt-added",
                                added=str(
                                    chatgpt_result.imported
                                    + chatgpt_result.updated
                                ),
                            )
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/work/refresh":
            length = int(self.headers.get("Content-Length", "0") or 0)
            form = _parse_form(self.rfile.read(length)) if length else {}
            self._adopt_ui_locale(form)
            try:
                refresh_work_source(
                    self.ui_data_root.absolute(),
                    "local_git",
                    workspace_root=self.workspace_root,
                )
            except (EvidenceHubError, OSError, ValueError):
                location = ui_url(
                    "/work", self.ui_locale, error="refresh-failed"
                )
            else:
                location = ui_url(
                    "/work",
                    self.ui_locale,
                    notice="refreshed",
                )
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/work/github/select":
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > 64 * 1024:
                self.send_error(413, "GitHub selection request is too large")
                return
            try:
                values = urllib.parse.parse_qs(
                    self.rfile.read(length).decode("utf-8"),
                    keep_blank_values=False,
                    strict_parsing=False,
                )
                self._adopt_ui_locale(
                    {key: items[0] for key, items in values.items() if items}
                )
                repository_ids = [
                    int(value) for value in values.get("repository", [])
                ]
                if any(repository_id <= 0 for repository_id in repository_ids):
                    raise ValueError("GitHub repository selection is invalid")
                GitHubConnectionStore(
                    self.ui_data_root.absolute()
                ).save_selection(repository_ids)
            except (GitHubConnectError, UnicodeError, ValueError):
                location = ui_url(
                    "/work", self.ui_locale, error="github-selection-invalid"
                )
            else:
                location = ui_url(
                    "/work/github", self.ui_locale, notice="selection-saved"
                )
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/work/github/refresh":
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length < 0 or length > 8 * 1024:
                self.send_error(413, "GitHub refresh request is too large")
                return
            form = _parse_form(self.rfile.read(length)) if length else {}
            self._adopt_ui_locale(form)
            token = github_access_token()
            store = GitHubConnectionStore(self.ui_data_root.absolute())
            try:
                if token is None or self.desktop_session_token is None:
                    raise GitHubConnectError("GitHub is not connected")
                state = store.load()
                if state is None or not state.selected_repositories:
                    raise GitHubConnectError("Select at least one GitHub repository")
                source, items = GitHubReadOnlyClient(token).evidence_snapshot(
                    account_id=state.account_id,
                    account_login=state.account_login,
                    repositories=state.selected_repositories,
                )
                receipt = EvidenceHub(
                    self.ui_data_root.absolute()
                ).sync_source(source, items=items)
                if receipt.status.value != "succeeded":
                    raise EvidenceHubError("GitHub Evidence refresh failed")
                store.mark_evidence_refresh(receipt_id=receipt.receipt_id)
            except (EvidenceHubError, GitHubConnectError, OSError, ValueError):
                try:
                    store.mark_evidence_refresh(
                        receipt_id=None, error_code="github_read_failed"
                    )
                except (GitHubConnectError, OSError, ValueError):
                    pass
                location = ui_url(
                    "/work", self.ui_locale, error="github-read-failed"
                )
            else:
                location = ui_url(
                    "/work", self.ui_locale, notice="github-refreshed"
                )
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/video/prepare":
            length = int(self.headers.get("Content-Length", "0") or 0)
            form = _parse_form(self.rfile.read(length))
            self._adopt_ui_locale(form)
            try:
                request = VideoGenerationRequest(
                    topic=form.get("topic", ""),
                    script=form.get("script", ""),
                    platform=form.get("platform", "Short video"),
                    language=form.get("language", "English"),
                    style=form.get("style", "Cinematic product demo"),
                    content_run_id=form.get("content_run_id", "").strip() or None,
                    evidence_ids=[
                        x.strip() for x in form.get("evidence_ids", "").splitlines() if x.strip()
                    ],
                    evidence_excerpts=[
                        x.strip()
                        for x in form.get("evidence_excerpts", "").splitlines()
                        if x.strip()
                    ],
                )
                job = create_job(self._video_data_root(), request)
            except (VideoGenerationError, ValueError, OSError) as exc:
                self._send_video_page(error=str(exc))
                return
            self.send_response(303)
            self.send_header(
                "Location", ui_url("/video", self.ui_locale, job_id=job.job_id)
            )
            self.end_headers()
            return
        if path == "/video/local/render":
            length = int(self.headers.get("Content-Length", "0") or 0)
            form = _parse_form(self.rfile.read(length))
            self._adopt_ui_locale(form)
            video_story_manager = self.video_story_job_manager
            if video_story_manager is None:
                self.send_error(503, "Local video worker is unavailable")
                return
            content_run_id = form.get("content_run_id", "").strip() or None
            try:
                job_id = video_story_manager.submit(
                    data_root=self.ui_data_root.absolute(),
                    repository_root=self.repo_root,
                    content_run_id=content_run_id,
                )
            except (OSError, ResumeWorkspaceStorageError, VideoStoryError) as exc:
                self._send_video_page(error=str(exc))
                return
            self.send_response(303)
            self.send_header(
                "Location",
                ui_url("/video", self.ui_locale, local_job_id=job_id),
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        submit_match = re.fullmatch(r"/video/submit/(video-[a-f0-9]{12})", path)
        poll_match = re.fullmatch(r"/video/poll/(video-[a-f0-9]{12})", path)
        if submit_match or poll_match:
            matched_video_action = submit_match or poll_match
            assert matched_video_action is not None
            job_id = matched_video_action.group(1)
            form = _parse_form(self.rfile.read(int(self.headers.get("Content-Length", "0") or 0)))
            self._adopt_ui_locale(form)
            try:
                job = load_job(self._video_data_root(), job_id)
                if submit_match:
                    if form.get("confirmation") != "PUBLISH":
                        raise VideoGenerationError("Type PUBLISH to authorize the external request")
                    if job.estimated_cost_usd > 1.0:
                        raise VideoGenerationError(
                            "Estimated cost exceeds the authorized $1.00 limit"
                        )
                    job = GoogleVeoClient().submit(job)
                else:
                    job = GoogleVeoClient().poll(job, data_root=self._video_data_root())
                save_job(self._video_data_root(), job)
            except (VideoGenerationError, OSError, ValueError) as exc:
                self._send_video_page(job_id, str(exc))
                return
            self.send_response(303)
            self.send_header(
                "Location", ui_url("/video", self.ui_locale, job_id=job_id)
            )
            self.end_headers()
            return
        canon_generate_match = re.fullmatch(r"/content/canon/(M1-[0-9]{2})", path)
        if path == "/creator/stories/mine":
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                self.send_error(400, "Invalid Content-Length")
                return
            if length < 0 or length > MAX_UPLOAD_BYTES:
                self.send_error(413, "Story mining request is too large")
                return
            form = _parse_form(self.rfile.read(length))
            self._adopt_ui_locale(form)
            data_root = self.ui_data_root.absolute()
            try:
                hub = EvidenceHub(data_root)
                recent = hub.recent_evidence(limit=50)
            except (EvidenceHubError, OSError, ValueError):
                recent = []
            approved = [
                item
                for item in recent
                if item.verification_status in {"verified", "approved"}
                or item.trust_state in {"verified", "approved"}
            ]
            existing_ids = [
                story.story_id for story in load_month_one_canon().stories
            ] + [candidate.candidate_id for candidate in load_story_candidates(data_root)]
            mined: tuple[StoryCandidate, ...] = ()
            mining_notice = "empty"
            try:
                mined = mine_story_candidates(
                    data_root,
                    evidence=approved,
                    existing_story_ids=existing_ids,
                    limit=5,
                )
                mining_notice = "ok" if mined else "empty"
            except StoryMiningError:
                mining_notice = "error"
            self.send_response(303)
            self.send_header(
                "Location",
                ui_url(
                    "/creator/stories",
                    self.ui_locale,
                    mining_notice=mining_notice,
                ),
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/creator/production/start":
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                self.send_error(400, "Invalid Content-Length")
                return
            if length < 0 or length > MAX_UPLOAD_BYTES:
                self.send_error(413, "Creator production request is too large")
                return
            form = _parse_form(self.rfile.read(length))
            self._adopt_ui_locale(form)
            manager = self.creator_production_job_manager
            if manager is None:
                self.send_error(503, "Creator production is unavailable")
                return
            action = form.get("production_action", "article")
            outputs: list[Literal["ARTICLE", "VIDEO"]]
            add_to_queue = action == "queue"
            if action == "article":
                outputs = ["ARTICLE"]
            elif action == "video":
                outputs = ["VIDEO"]
            elif action == "queue":
                outputs = ["ARTICLE", "VIDEO"]
            else:
                self.send_error(400, "Creator production action is invalid")
                return
            source_kind = form.get("source_kind", "")
            language: Literal["English", "中文"] = (
                "中文" if form.get("language") == "中文" else "English"
            )
            preference = _load_ai_provider_preference(self.ui_data_root.absolute())
            generation_mode = _creator_generation_mode(preference, form, source_kind)
            ai_editorial = generation_mode != "template"
            gateway = _gateway_from_preference(preference) if ai_editorial else None
            provider, model = _creator_job_provider_metadata(preference, generation_mode)
            data_root = self.ui_data_root.absolute()
            if source_kind == "STORY":
                story_id = form.get("source_story_id", "")
                if re.fullmatch(
                    r"(?:M1-[0-9]{2}|story-candidate-[a-f0-9]{12})", story_id
                ) is None:
                    self.send_error(400, "Creator story is invalid")
                    return
                if story_id.startswith("story-candidate-"):

                    def runner() -> str:
                        result = run_story_candidate(
                            story_id,
                            data_root,
                            language=language,
                            gateway=gateway,
                        )
                        if result.run_id is None:
                            if ai_editorial:
                                raise CreatorProductionError("AI_NOT_EXECUTED")
                            raise CreatorProductionError(
                                result.error or "Content generation failed"
                            )
                        return result.run_id
                else:

                    def runner() -> str:
                        result = run_month_one_story(
                            story_id,
                            data_root,
                            language=language,
                            gateway=gateway,
                        )
                        if result.run_id is None:
                            if ai_editorial:
                                raise CreatorProductionError("AI_NOT_EXECUTED")
                            raise CreatorProductionError(
                                result.error or "Content generation failed"
                            )
                        return result.run_id

                source_story_id: str | None = story_id
            elif source_kind == "CREATE":
                self.latest_content_form = dict(form)
                if ai_editorial:
                    _apply_ai_provider_preference(form, data_root)

                def runner() -> str:
                    result = run_content_form(form, data_root, gateway=gateway)
                    if result.run_id is None:
                        if ai_editorial:
                            raise CreatorProductionError("AI_NOT_EXECUTED")
                        raise CreatorProductionError(result.error or "Content generation failed")
                    return result.run_id

                source_story_id = None
            else:
                self.send_error(400, "Creator production source is invalid")
                return
            request = CreatorProductionRequest(
                source_kind=cast(Literal["STORY", "CREATE"], source_kind),
                source_story_id=source_story_id,
                outputs=outputs,
                language=language,
                ai_editorial=ai_editorial,
                add_to_queue=add_to_queue,
            )
            job = manager.submit(
                data_root=data_root,
                request=request,
                runner=runner,
                renderer=(
                    lambda run_id: render_creator_video(
                        data_root=data_root,
                        run_id=run_id,
                        repository_root=self.creator_video_root,
                    )
                )
                if "VIDEO" in outputs
                else None,
                provider=provider,
                model=model,
            )
            self.send_response(303)
            self.send_header(
                "Location",
                ui_url(
                    "/creator/create",
                    self.ui_locale,
                    creator_job=job.job_id,
                ),
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/content/reference-video":
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                self.send_error(400, "Invalid Content-Length")
                return
            max_request_bytes = (
                MAX_REFERENCE_VIDEO_BYTES + _REFERENCE_VIDEO_FORM_OVERHEAD_BYTES
            )
            if length < 0 or length > max_request_bytes:
                self.send_error(413, "Reference video is too large")
                return
            try:
                submission = _parse_submission(
                    self.rfile.read(length),
                    self.headers.get("Content-Type", ""),
                    max_bytes=max_request_bytes,
                )
                self._adopt_ui_locale(submission.fields)
                upload = submission.files.get("reference_video")
                if upload is None:
                    raise ReferenceVideoError("Choose one local reference MP4")
                analyzed = analyze_reference_video(
                    data_root=self.ui_data_root.absolute(),
                    resource_root=self.creator_video_root,
                    filename=upload.filename,
                    content=upload.content,
                    title=submission.fields.get("reference_title", ""),
                    author=submission.fields.get("reference_author", ""),
                )
            except (OSError, ReferenceVideoError, ValueError) as exc:
                self._send_content_page(error=str(exc))
                return
            self.send_response(303)
            self.send_header(
                "Location",
                ui_url(
                    "/content",
                    self.ui_locale,
                    reference_id=analyzed.asset.reference_id,
                    reference="analyzed",
                ),
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if canon_generate_match is not None:
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                self.send_error(400, "Invalid Content-Length")
                return
            if length < 0 or length > MAX_UPLOAD_BYTES:
                self.send_error(413, "Content request is too large")
                return
            form = _parse_form(self.rfile.read(length))
            self._adopt_ui_locale(form)
            preference = _load_ai_provider_preference(self.ui_data_root.absolute())
            content_result = run_month_one_story(
                canon_generate_match.group(1),
                self.ui_data_root.absolute(),
                language="中文" if form.get("language") == "中文" else "English",
                gateway=_gateway_from_preference(preference),
            )
            if content_result.run_id is None:
                self._send_content_page(result=content_result)
                return
            self.send_response(303)
            self.send_header(
                "Location",
                ui_url(
                    "/content#results",
                    self.ui_locale,
                    run_id=content_result.run_id,
                ),
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/content/generate":
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                self.send_error(400, "Invalid Content-Length")
                return
            if length < 0 or length > MAX_UPLOAD_BYTES:
                self.send_error(413, "Content brief is too large")
                return
            self.latest_content_form = _parse_form(self.rfile.read(length))
            self._adopt_ui_locale(self.latest_content_form)
            creator_view = self.latest_content_form.get("creator_view") == "create"
            content_gateway: ModelGateway | None = None
            if self.latest_content_form.get("generation_mode") != "template":
                _apply_ai_provider_preference(
                    self.latest_content_form, self.ui_data_root.absolute()
                )
                content_gateway = _gateway_from_preference(
                    _load_ai_provider_preference(self.ui_data_root.absolute())
                )
            content_result = run_content_form(
                self.latest_content_form,
                self.ui_data_root.absolute(),
                gateway=content_gateway,
            )
            if content_result.run_id is None:
                self._send_content_page(
                    result=content_result,
                    workspace_view="create" if creator_view else "all",
                )
                return
            self.send_response(303)
            self.send_header(
                "Location",
                ui_url(
                    "/creator/create#results" if creator_view else "/content#results",
                    self.ui_locale,
                    run_id=content_result.run_id,
                ),
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        content_review_match = re.fullmatch(
            r"/content/review/(content-[^/]+)", path
        )
        if content_review_match is not None:
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                self.send_error(400, "Invalid Content-Length")
                return
            if length < 0 or length > MAX_UPLOAD_BYTES:
                self.send_error(413, "Content review is too large")
                return
            form = _parse_form(self.rfile.read(length))
            self._adopt_ui_locale(form)
            run_id = content_review_match.group(1)
            action = form.get("review_action", "save")
            decision = ContentReviewDecision.DRAFT
            regenerate_target: str | None = None
            review_state = "saved"
            if action == "approve":
                decision = ContentReviewDecision.APPROVED
                review_state = "approved"
            elif action == "reject":
                decision = ContentReviewDecision.REJECTED
                review_state = "rejected"
            elif action.startswith("regenerate:"):
                regenerate_target = action.partition(":")[2]
                review_state = "regenerated"
            elif action != "save":
                self.send_error(400, "Unknown content review action")
                return
            updates = {
                key: form.get(key, "")
                for key in (
                    "canonical_story",
                    "linkedin",
                    "x_thread",
                    "x_post",
                    "blog",
                    "youtube_script",
                    "video_script",
                )
            }
            try:
                save_content_review(
                    data_root=self.ui_data_root.absolute(),
                    run_id=run_id,
                    updates=updates,
                    decision=decision,
                    regenerate_target=regenerate_target,
                )
            except (ContentWorkspaceError, OSError, ValueError) as exc:
                self._send_content_page(run_id=run_id, error=str(exc))
                return
            self.send_response(303)
            self.send_header(
                "Location",
                ui_url(
                    "/content#results",
                    self.ui_locale,
                    run_id=run_id,
                    review=review_state,
                ),
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        presenter_asset_match = re.fullmatch(
            r"/content/presenter-asset/(content-[^/]+)", path
        )
        if presenter_asset_match is not None:
            run_id = presenter_asset_match.group(1)
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                self.send_error(400, "Invalid Content-Length")
                return
            max_request_bytes = (
                MAX_PRESENTER_ASSET_BYTES + _PRESENTER_ASSET_FORM_OVERHEAD_BYTES
            )
            if length < 0 or length > max_request_bytes:
                self.send_error(413, "Presenter asset is too large")
                return
            try:
                submission = _parse_submission(
                    self.rfile.read(length),
                    self.headers.get("Content-Type", ""),
                    max_bytes=max_request_bytes,
                )
                self._adopt_ui_locale(submission.fields)
                upload = submission.files.get("presenter_asset")
                if upload is None:
                    raise PresenterAssetError("Choose one presenter MP4")
                import_presenter_asset(
                    data_root=self.ui_data_root.absolute(),
                    display_name=submission.fields.get("display_name", ""),
                    category=PresenterAssetCategory(
                        submission.fields.get("category", "")
                    ),
                    source_kind=PresenterAssetKind(
                        submission.fields.get("source_kind", "")
                    ),
                    layout=PresenterLayout(submission.fields.get("layout", "")),
                    duration_seconds=float(
                        submission.fields.get("duration_seconds", "0")
                    ),
                    source_filename=upload.filename,
                    content=upload.content,
                    locale=submission.fields.get("locale") or None,
                )
            except (OSError, PresenterAssetError, ValueError) as exc:
                self._send_content_page(run_id=run_id, error=str(exc))
                return
            self.send_response(303)
            self.send_header(
                "Location", ui_url("/content#results", self.ui_locale, run_id=run_id)
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        presenter_plan_match = re.fullmatch(
            r"/content/presenter-plan/(content-[^/]+)", path
        )
        if presenter_plan_match is not None:
            run_id = presenter_plan_match.group(1)
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > 64 * 1024:
                self.send_error(413, "Presenter plan request is too large")
                return
            try:
                values = urllib.parse.parse_qs(
                    self.rfile.read(length).decode("utf-8"),
                    keep_blank_values=False,
                    strict_parsing=False,
                )
                self._adopt_ui_locale(
                    {key: items[0] for key, items in values.items() if items}
                )
                save_presenter_preferences(
                    data_root=self.ui_data_root.absolute(),
                    run_id=run_id,
                    evidence_visual_scene_ids=set(
                        values.get("evidence_visual_scene", [])
                    ),
                )
            except (OSError, PresenterAssetError, UnicodeError, ValueError) as exc:
                self._send_content_page(run_id=run_id, error=str(exc))
                return
            self.send_response(303)
            self.send_header(
                "Location", ui_url("/content#results", self.ui_locale, run_id=run_id)
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        avatar_handoff_match = re.fullmatch(
            r"/content/avatar-handoff/(content-[^/]+)", path
        )
        if avatar_handoff_match is not None:
            run_id = avatar_handoff_match.group(1)
            try:
                prepare_heygen_handoff(
                    data_root=self.ui_data_root.absolute(),
                    run_id=run_id,
                )
            except (
                ContentWorkspaceError,
                CreatorVideoError,
                OSError,
                PresenterAssetError,
            ) as exc:
                self._send_content_page(run_id=run_id, error=str(exc))
                return
            self.send_response(303)
            self.send_header(
                "Location", ui_url("/content#results", self.ui_locale, run_id=run_id)
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        avatar_import_match = re.fullmatch(
            r"/content/avatar-import/(content-[^/]+)", path
        )
        if avatar_import_match is not None:
            run_id = avatar_import_match.group(1)
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                self.send_error(400, "Invalid Content-Length")
                return
            if length < 0 or length > MAX_UPLOAD_BYTES:
                self.send_error(413, "Avatar segment is too large")
                return
            try:
                submission = _parse_submission(
                    self.rfile.read(length),
                    self.headers.get("Content-Type", ""),
                )
                self._adopt_ui_locale(submission.fields)
                clip = submission.files.get("avatar_clip")
                if clip is None:
                    raise CreatorVideoError("Choose one Avatar MP4")
                import_avatar_segment(
                    data_root=self.ui_data_root.absolute(),
                    run_id=run_id,
                    scene_id=submission.fields.get("scene_id", ""),
                    source_filename=clip.filename,
                    content=clip.content,
                )
            except (ContentWorkspaceError, CreatorVideoError, OSError, ValueError) as exc:
                self._send_content_page(run_id=run_id, error=str(exc))
                return
            self.send_response(303)
            self.send_header(
                "Location", ui_url("/content#results", self.ui_locale, run_id=run_id)
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        content_render_match = re.fullmatch(r"/content/render/(content-[^/]+)", path)
        if content_render_match is not None:
            if not creator_video_runtime_available(self.creator_video_root):
                self.send_error(404, "Experimental Creator Video runtime is unavailable")
                return
            run_id = content_render_match.group(1)
            creator_manager = self.creator_video_job_manager
            if creator_manager is None:
                self.send_error(503, "Creator Video background service is unavailable")
                return
            try:
                creator_manager.start(
                    data_root=self.ui_data_root.absolute(),
                    run_id=run_id,
                    repository_root=self.creator_video_root,
                )
            except (ContentWorkspaceError, CreatorVideoError, OSError):
                self.send_error(422, "Creator Video render could not start")
                return
            self.send_response(303)
            location = ui_url(
                "/content#results", self.ui_locale, run_id=run_id
            )
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        media_quality_match = re.fullmatch(
            r"/content/media-quality/(content-[^/]+)", path
        )
        if media_quality_match is not None:
            run_id = media_quality_match.group(1)
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > 16 * 1024:
                self.send_error(413, "Media-quality review is too large")
                return
            form = _parse_form(self.rfile.read(length))
            self._adopt_ui_locale(form)
            try:
                save_media_quality_review(
                    data_root=self.ui_data_root.absolute(),
                    run_id=run_id,
                    checklist=MediaQualityChecklist(
                        voice_natural=form.get("voice_natural") == "on",
                        pacing_natural=form.get("pacing_natural") == "on",
                        no_static_visual_too_long=(
                            form.get("no_static_visual_too_long") == "on"
                        ),
                        presenter_adds_value=(
                            form.get("presenter_adds_value") == "on"
                        ),
                        language_natural=form.get("language_natural") == "on",
                        claims_evidence_backed=(
                            form.get("claims_evidence_backed") == "on"
                        ),
                        reference_influenced_without_copying=(
                            form.get("reference_influenced_without_copying") == "on"
                        ),
                        would_publish=form.get("would_publish") == "on",
                    ),
                    notes=form.get("notes", ""),
                )
            except (ContentWorkspaceError, MediaQualityError, OSError, ValueError) as exc:
                self._send_content_page(run_id=run_id, error=str(exc))
                return
            self.send_response(303)
            self.send_header(
                "Location", ui_url("/content#results", self.ui_locale, run_id=run_id)
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        distribution_match = re.fullmatch(
            r"/content/distribution/(content-[^/]+)", path
        )
        if distribution_match is not None:
            run_id = distribution_match.group(1)
            try:
                prepare_distribution_package(
                    data_root=self.ui_data_root.absolute(),
                    run_id=run_id,
                )
            except (ContentDistributionError, ContentWorkspaceError, OSError) as exc:
                self._send_content_page(run_id=run_id, error=str(exc))
                return
            self.send_response(303)
            self.send_header(
                "Location", ui_url("/content#results", self.ui_locale, run_id=run_id)
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/creator/publish/queue":
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > 16 * 1024:
                self.send_error(413, "Publish Queue request is too large")
                return
            form = _parse_form(self.rfile.read(length))
            self._adopt_ui_locale(form)
            try:
                item = assign_artifact_to_account(
                    data_root=self.ui_data_root.absolute(),
                    artifact_id=form.get("artifact_id", ""),
                    channel_account_id=form.get("channel_account_id", ""),
                )
            except (CreatorProductionError, OSError) as exc:
                body = editorial_publishing_page(
                    data_root=self.ui_data_root.absolute(),
                    error=str(exc),
                    locale=self.ui_locale,
                    creator_mode=True,
                ).encode("utf-8")
                self.send_response(422)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(303)
            self.send_header(
                "Location",
                ui_url(
                    "/creator/publish",
                    self.ui_locale,
                    queue_item=item.queue_item_id,
                ),
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/publishing/editorial/preview":
            length = int(self.headers.get("Content-Length", "0") or 0)
            form = _parse_form(self.rfile.read(length))
            self._adopt_ui_locale(form)
            channel = form.get("channel", "")
            try:
                if channel not in {"linkedin", "x"}:
                    raise EditorialPublishingError("select LinkedIn or X")
                preview_editorial_day(
                    data_root=self.ui_data_root.absolute(),
                    day_directory=Path(form.get("day_directory", "")),
                    channel=cast(EditorialChannel, channel),
                )
            except (EditorialPublishingError, OSError):
                self.send_error(422, "Editorial package preview failed")
                return
            self.send_response(303)
            self.send_header("Location", ui_url("/creator/publish", self.ui_locale))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/publishing/youtube/upload":
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > 16 * 1024:
                self.send_error(413, "YouTube upload request is too large")
                return
            form = _parse_form(self.rfile.read(length))
            self._adopt_ui_locale(form)
            manager = self.youtube_job_manager
            try:
                if form.get("confirmation") != "UPLOAD":
                    raise YouTubePublishingError(
                        "Type UPLOAD to authorize this exact external upload",
                        code="UPLOAD_CONFIRMATION_REQUIRED",
                    )
                if manager is None:
                    raise YouTubePublishingError(
                        "YouTube background worker is unavailable",
                        code="WORKER_UNAVAILABLE",
                    )
                request = normalize_upload_request(
                    run_id=form.get("run_id", ""),
                    channel_id=form.get("channel_id", ""),
                    title=form.get("title", ""),
                    description=form.get("description", ""),
                    tags=form.get("tags", ""),
                    privacy_status=form.get("privacy_status", "private"),
                )
                job = manager.start_upload(
                    data_root=self.ui_data_root.absolute(), request=request
                )
            except (OSError, YouTubePublishingError) as exc:
                body = editorial_publishing_page(
                    data_root=self.ui_data_root.absolute(),
                    error=str(exc),
                    locale=self.ui_locale,
                    creator_mode=True,
                ).encode("utf-8")
                self.send_response(422)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "private, no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(303)
            self.send_header(
                "Location",
                ui_url(
                    "/creator/publish", self.ui_locale, youtube_job=job.job_id
                ),
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        editorial_publish_match = re.fullmatch(r"/publishing/editorial/(linkedin|x)/publish", path)
        if editorial_publish_match is not None:
            length = int(self.headers.get("Content-Length", "0") or 0)
            form = _parse_form(self.rfile.read(length))
            self._adopt_ui_locale(form)
            try:
                publish_editorial_preview(
                    data_root=self.ui_data_root.absolute(),
                    channel=cast(EditorialChannel, editorial_publish_match.group(1)),
                    confirmation=form.get("confirmation", ""),
                )
            except (EditorialPublishingError, OSError):
                self.send_error(422, "Editorial publication failed")
                return
            self.send_response(303)
            self.send_header("Location", ui_url("/creator/publish", self.ui_locale))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/applications/status":
            length = int(self.headers.get("Content-Length", "0") or 0)
            application_form = _parse_form(self.rfile.read(length))
            self._adopt_ui_locale(application_form)
            library_value = application_form.get("resume_library_root", "").strip()
            library_root = Path(
                library_value
                or (
                    self.ui_data_root.absolute() / "resume-applications"
                    if self.desktop_session_token is not None
                    else Path.home() / "Documents" / "Resume Applications"
                )
            )
            directory_name = application_form.get("directory_name", "").strip()
            try:
                status = ApplicationStatus(application_form.get("status", "").strip())
                application_dir = library_root / "applications" / directory_name
                if not application_dir.is_dir() or application_dir.is_symlink():
                    raise ValueError("application directory not found")
                update_application_status(
                    application_dir=application_dir,
                    status=status,
                    note=application_form.get("note", ""),
                    next_action=application_form.get("next_action", ""),
                )
            except (OSError, ValueError) as exc:
                self.send_error(422, str(exc))
                return
            self.send_response(303)
            self.send_header(
                "Location", ui_url("/resume#applications", self.ui_locale)
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/learning/case/create":
            length = int(self.headers.get("Content-Length", "0") or 0)
            case_form = _parse_form(self.rfile.read(length))
            self._adopt_ui_locale(case_form)
            self.latest_learning_form = dict(case_form)
            self._send_learning_page(
                _create_learning_case_ui(
                    case_form,
                    self.ui_data_root.absolute(),
                    self.workspace_root,
                )
            )
            return
        if path == "/learning/practice/create":
            length = int(self.headers.get("Content-Length", "0") or 0)
            practice_form = _parse_form(self.rfile.read(length))
            self._adopt_ui_locale(practice_form)
            self.latest_learning_form = dict(practice_form)
            self._send_learning_page(
                _create_practice_exercise_ui(
                    practice_form, self.ui_data_root.absolute()
                )
            )
            return
        if path == "/learning/practice/open":
            length = int(self.headers.get("Content-Length", "0") or 0)
            practice_form = _parse_form(self.rfile.read(length))
            self._adopt_ui_locale(practice_form)
            self.latest_learning_form = dict(practice_form)
            self._send_learning_page(
                _open_practice_exercise_ui(
                    practice_form, self.ui_data_root.absolute()
                )
            )
            return
        if path == "/learning/practice/complete":
            length = int(self.headers.get("Content-Length", "0") or 0)
            body = self.rfile.read(length)
            try:
                practice_submission = _parse_submission(
                    body, self.headers.get("Content-Type", "")
                )
            except (UnicodeError, ValueError) as exc:
                result = UIActionResult(
                    "learning-practice", "complete practice", 1, "", str(exc), 0
                )
                self._send_learning_page(result)
                return
            self._adopt_ui_locale(practice_submission.fields)
            self.latest_learning_form = dict(practice_submission.fields)
            self._send_learning_page(
                _complete_practice_exercise_ui(
                    practice_submission.fields,
                    practice_submission.files,
                    self.ui_data_root.absolute(),
                )
            )
            return
        if path == "/learning/run":
            length = int(self.headers.get("Content-Length", "0") or 0)
            body = self.rfile.read(length)
            self.latest_learning_form = _parse_form(body)
            self._adopt_ui_locale(self.latest_learning_form)
            if self.workspace_root is None:
                self._send_learning_page(
                    UIActionResult(
                        "learning-traceability",
                        "local deterministic traceability build",
                        1,
                        "",
                        ui_text(
                            self.ui_locale,
                            "这个练习需要一个已连接的代码项目。请先选择项目。",
                            "This exercise needs a connected code project. Choose a project first.",
                        ),
                        0,
                    )
                )
                return
            self._send_learning_page(
                _run_learning_workspace(
                    self.latest_learning_form,
                    self.ui_data_root.absolute(),
                    self.workspace_root,
                )
            )
            return
        if path == "/learning/respond":
            length = int(self.headers.get("Content-Length", "0") or 0)
            response_form = _parse_form(self.rfile.read(length))
            self._adopt_ui_locale(response_form)
            self.latest_learning_form = {
                "target_requirement": response_form.get(
                    "target_requirement",
                    self.latest_learning_form.get("target_requirement", DEFAULT_TARGET_REQUIREMENT),
                )
            }
            learning_result = _save_learning_response(response_form, self.ui_data_root.absolute())
            if learning_result.return_code == 0:
                self.send_response(303)
                self.send_header(
                    "Location",
                    ui_url(
                        _learning_response_location(response_form.get("stage", "")),
                        self.ui_locale,
                    ),
                )
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send_learning_page(learning_result)
            return
        if path == "/resume/interview-defense/map":
            length = int(self.headers.get("Content-Length", "0") or 0)
            mapping_form = _parse_form(self.rfile.read(length))
            self._adopt_ui_locale(mapping_form)
            try:
                if self.workspace_root is None:
                    raise ValueError("select a connected project first")
                map_interview_defense_bullet(
                    data_root=self.ui_data_root.absolute(),
                    repository_root=self.workspace_root,
                    resume_run_id=mapping_form.get("resume_run_id", ""),
                    bullet_id=mapping_form.get("bullet_id", ""),
                    learning_run_id=mapping_form.get("learning_run_id", ""),
                )
            except (ResumeWorkspaceStorageError, LearningTraceabilityError, OSError, ValueError):
                self.send_error(400, "Interview Defense mapping could not be validated")
                return
            self.send_response(303)
            self.send_header(
                "Location",
                ui_url(
                    "/learning#interview-defense",
                    self.ui_locale,
                    run_id=mapping_form.get("learning_run_id", ""),
                    resume_run_id=mapping_form.get("resume_run_id", ""),
                    bullet_id=mapping_form.get("bullet_id", ""),
                ),
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/resume/template-preview":
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                self.send_error(400, "Invalid Content-Length")
                return
            if length < 0 or length > MAX_UPLOAD_BYTES:
                self.send_error(413, "Upload is too large")
                return
            body = self.rfile.read(length)
            try:
                submission = _parse_submission(
                    body, self.headers.get("Content-Type", "")
                )
                self._adopt_ui_locale(submission.fields)
                template_preview_receipt = _prepare_resume_template_preview(submission)
            except (OSError, UnicodeError, ValueError) as exc:
                self.latest_user_form["template_preview_error"] = str(exc)
                location = ui_url("/resume", self.ui_locale)
            else:
                self.latest_resume_template_receipt = template_preview_receipt
                self.latest_user_form["resume_template_preview_id"] = (
                    template_preview_receipt.preview_id
                )
                self.latest_user_form.pop("template_preview_error", None)
                location = ui_url(
                    f"/resume?template_preview={template_preview_receipt.preview_id}",
                    self.ui_locale,
                )
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if path not in {"/run", "/generate"}:
            self.send_error(404, "Not found")
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            self.send_error(400, "Invalid Content-Length")
            return
        if length < 0 or length > MAX_UPLOAD_BYTES:
            self.send_error(413, "Upload is too large")
            return
        body = self.rfile.read(length)
        if path == "/generate":
            self.latest_user_form = {}
            try:
                submission = _parse_submission(body, self.headers.get("Content-Type", ""))
            except (UnicodeError, ValueError) as exc:
                result = UIActionResult(
                    "tailored-resume", "local resume generation", 2, "", str(exc), 0
                )
                self._send_user_page(result)
                return
            self._adopt_ui_locale(submission.fields)
            data_root = self.ui_data_root.absolute()
            resume_gateway: ModelGateway | None = None
            expert_gateway: ModelGateway | None = None
            if submission.fields.get("generation_mode") != "template":
                _apply_ai_provider_preference(submission.fields, data_root)
                resume_gateway = _gateway_from_preference(
                    _load_ai_provider_preference(data_root)
                )
            if submission.fields.get("expert_review_mode") == "openai_sol":
                expert_gateway = model_gateway_for(
                    ModelProviderId.OPENAI_COMPATIBLE,
                    model=_OPENAI_EXPERT_REVIEW_MODEL,
                    openai_endpoint=_OPENAI_CHAT_COMPLETIONS_URL,
                    openai_api_key=openai_api_key(),
                )
            resume_manager = self.resume_job_manager
            if resume_manager is None:
                self.send_error(503, "Resume background worker is unavailable")
                return
            post_response_ms = (
                int((time.perf_counter() - resume_post_started) * 1000)
                if resume_post_started is not None
                else 0
            )
            try:
                job_id = resume_manager.submit(
                    form=submission.fields,
                    files=submission.files,
                    data_root=data_root,
                    repo_root=self.repo_root,
                    evidence_repository_root=self.workspace_root,
                    gateway=resume_gateway,
                    expert_gateway=expert_gateway,
                    template_receipt=self.latest_resume_template_receipt,
                    initial_timings_ms={"post_response_ms": post_response_ms},
                )
            except RuntimeError:
                self.send_error(503, "Resume background worker is unavailable")
                return
            self.latest_user_form = {
                key: submission.fields.get(key, "")
                for key in (
                    "generation_mode",
                    "provider_model",
                    "expert_review_mode",
                    "resume_output_language",
                    "resume_template_preview_id",
                )
            }
            self.latest_user_form["ui_locale"] = self.ui_locale
            self.send_response(303)
            self.send_header(
                "Location",
                ui_url(f"/resume/jobs/{job_id}", self.ui_locale),
            )
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        self.latest_form = _parse_form(body)
        self._adopt_ui_locale(self.latest_form)
        data_root = (
            Path(self.latest_form.get("data_root", str(self.ui_data_root))).expanduser().absolute()
        )
        advanced_result = _run_action(self.latest_form, data_root, self.repo_root)
        self._send_advanced_page(advanced_result)


def main() -> None:
    parser = argparse.ArgumentParser(description="SoloScale minimal local UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--data-root",
        default=None,
        help="SoloScale private data root (existing legacy root, otherwise Application Support)",
    )
    parser.add_argument("--resource-root", help="Bundled resource root")
    parser.add_argument("--repository-root", help="Repository root for repository-aware workflows")
    parser.add_argument("--workspace-root", help="Operator workspace root")
    parser.add_argument("--desktop-mode", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--readiness-file", help="Write a JSON readiness record after binding")
    args = parser.parse_args()
    desktop_token: str | None = None
    if args.desktop_mode:
        if args.host != "127.0.0.1":
            parser.error("desktop mode must bind exactly to 127.0.0.1")
        desktop_token = os.environ.get("SOLOSCALE_DESKTOP_SESSION_TOKEN", "")
        if _DESKTOP_TOKEN_RE.fullmatch(desktop_token) is None:
            parser.error("desktop mode requires a strong SOLOSCALE_DESKTOP_SESSION_TOKEN")
        try:
            configure_desktop_credentials_from_stdin()
        except DesktopCredentialError:
            parser.error("desktop credential handoff is invalid")
    selected_data_root = (
        args.data_root
        if args.data_root is not None or args.desktop_mode
        else source_data_root()
    )
    paths = resolve_runtime_paths(
        data_root=selected_data_root,
        resource_root=args.resource_root,
        repository_root=args.repository_root,
        workspace_root=args.workspace_root,
    )

    handler = SoloScaleLocalUIHandler
    handler.ui_data_root = paths.data_root
    handler.repo_root = paths.repository_root
    handler.creator_video_root = paths.resource_root
    handler.workspace_root = (
        paths.workspace_root
        if args.workspace_root is not None
        or bool(os.environ.get("SOLOSCALE_WORKSPACE_ROOT"))
        else None
    )
    pending_chatgpt_export: Path | None = None
    if args.desktop_mode:
        raw_pending_export = os.environ.get("SOLOSCALE_PENDING_CHATGPT_EXPORT", "")
        candidate = Path(raw_pending_export).expanduser() if raw_pending_export else None
        if (
            candidate is not None
            and candidate.is_file()
            and not candidate.is_symlink()
            and candidate.suffix.casefold() in {".json", ".zip"}
        ):
            pending_chatgpt_export = candidate.absolute()
    handler.pending_chatgpt_export = pending_chatgpt_export
    handler.desktop_session_token = desktop_token
    handler.desktop_session_cookie = None
    handler.desktop_bootstrap_consumed = False
    resume_job_manager = ResumeJobManager()
    handler.resume_job_manager = resume_job_manager
    video_story_job_manager = LocalVideoJobManager()
    handler.video_story_job_manager = video_story_job_manager
    creator_video_job_manager = CreatorVideoJobManager()
    handler.creator_video_job_manager = creator_video_job_manager
    codex_import_job_manager = CodexImportJobManager()
    handler.codex_import_job_manager = codex_import_job_manager
    youtube_job_manager = YouTubePublishingJobManager()
    handler.youtube_job_manager = youtube_job_manager
    creator_production_job_manager = CreatorProductionJobManager()
    handler.creator_production_job_manager = creator_production_job_manager

    server = HTTPServer((args.host, args.port), handler)
    raw_host, port = server.server_address[:2]
    host = raw_host.decode("ascii") if isinstance(raw_host, bytes) else raw_host
    handler.desktop_expected_host = f"{host}:{port}" if args.desktop_mode else None
    readiness = {
        "schema_version": "1.0",
        "url": f"http://{host}:{port}",
        "pid": os.getpid(),
    }
    if desktop_token is not None:
        handler.desktop_origin = cast(str, readiness["url"])
        handler.desktop_pid = cast(int, readiness["pid"])
        readiness["proof"] = _desktop_readiness_proof(
            token=desktop_token,
            url=cast(str, readiness["url"]),
            pid=cast(int, readiness["pid"]),
        )
    else:
        handler.desktop_origin = None
        handler.desktop_pid = None
    readiness_path = Path(args.readiness_file).expanduser() if args.readiness_file else None
    if readiness_path is not None:
        _write_readiness_file(readiness_path, readiness)
    print(f"SoloScale local UI: {readiness['url']}")
    public_readiness = {key: readiness[key] for key in ("schema_version", "url", "pid")}
    print(json.dumps(public_readiness, separators=(",", ":")), flush=True)

    def request_shutdown(*_: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    previous_sigterm = signal.signal(signal.SIGTERM, request_shutdown)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        server.server_close()
        resume_job_manager.shutdown()
        handler.resume_job_manager = None
        video_story_job_manager.shutdown()
        handler.video_story_job_manager = None
        creator_video_job_manager.shutdown()
        handler.creator_video_job_manager = None
        codex_import_job_manager.shutdown()
        handler.codex_import_job_manager = None
        youtube_job_manager.shutdown()
        handler.youtube_job_manager = None
        creator_production_job_manager.shutdown()
        handler.creator_production_job_manager = None
        if readiness_path is not None:
            try:
                readiness_path.unlink()
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    main()
