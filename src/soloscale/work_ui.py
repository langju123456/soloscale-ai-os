# ruff: noqa: E501
"""User-facing Work Context over existing private SoloScale stores."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import stat
import tempfile
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from soloscale.conversation_intake import (
    ConversationIntakeError,
    discover_buildlog_runs,
    discover_codex_sources,
    parse_buildlog_run,
    parse_chatgpt_export,
    parse_codex_session,
)
from soloscale.evidence_hub import (
    EvidenceHub,
    EvidenceHubError,
    inspect_git_repository,
)
from soloscale.evidence_ui import refresh_local_project_evidence
from soloscale.github_connect import GitHubConnectionState, GitHubConnectionStore
from soloscale.knowledge_models import ParsedSource, SyncReport
from soloscale.knowledge_store import KnowledgeStore, KnowledgeStoreError
from soloscale.ui_shell import (
    DEFAULT_UI_LOCALE,
    SourceState,
    UILocale,
    render_app_shell,
    render_source_state,
    ui_text,
    ui_url,
)


class WorkContextError(ValueError):
    """Raised when an explicitly approved work source cannot be imported safely."""


CodexImportPhase = Literal[
    "QUEUED",
    "DISCOVERING",
    "PROCESSING",
    "READY",
    "FAILED",
]

_CODEX_IMPORT_JOB_ID = re.compile(
    r"codex-import-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{10}"
)
_TERMINAL_CODEX_IMPORT_PHASES = {"READY", "FAILED"}
_SHA256 = re.compile(r"[a-f0-9]{64}")


@dataclass(frozen=True)
class WorkContextSnapshot:
    """Safe counts for product UI; private bodies and locators are deliberately absent."""

    resume_runs: int = 0
    codex_sessions: int = 0
    chatgpt_exports: int = 0
    buildlog_runs: int = 0
    knowledge_documents: int = 0
    reusable_items: int = 0
    project_connected: bool = False
    project_name: str | None = None
    project_authorization_state: SourceState = "NOT_CONNECTED"
    project_freshness_state: SourceState = "UNAVAILABLE"
    project_head: str | None = None
    project_branch: str | None = None
    project_dirty_count: int = 0
    project_new_commits: int | None = None
    project_indexed_at: str | None = None
    project_fact_count: int = 0
    github_connected: bool = False
    github_account_login: str | None = None
    github_selected_repositories: int = 0
    github_authorization_state: SourceState = "NOT_CONNECTED"
    github_freshness_state: SourceState = "UNAVAILABLE"
    codex_freshness_state: SourceState = "UNAVAILABLE"
    chatgpt_freshness_state: SourceState = "UNAVAILABLE"
    codex_folder_available: bool = False
    last_synced: bool = False
    preflight_trace_id: str | None = None
    preflight_at: str | None = None

    @property
    def project_state(self) -> SourceState:
        """Backward-compatible combined view: freshness when authorized."""

        if self.project_authorization_state != "READY":
            return self.project_authorization_state
        return self.project_freshness_state

    @property
    def github_state(self) -> SourceState:
        """Backward-compatible combined view: freshness when authorized."""

        if self.github_authorization_state != "READY":
            return self.github_authorization_state
        return self.github_freshness_state

    @property
    def ai_conversations(self) -> int:
        return self.codex_sessions + self.chatgpt_exports

    @property
    def has_work(self) -> bool:
        return bool(
            self.resume_runs
            or self.knowledge_documents
            or self.project_connected
            or (self.github_connected and self.github_selected_repositories)
            or self.reusable_items
        )


@dataclass(frozen=True)
class WorkImportResult:
    """Sanitized import totals suitable for a user-facing success decision."""

    discovered: int
    imported: int
    updated: int
    skipped: int
    failed: int
    trace_id: str | None = None


WorkSourceKind = Literal["local_git", "codex", "github", "chatgpt_export"]

_WORK_SOURCE_KINDS: tuple[WorkSourceKind, ...] = (
    "local_git",
    "codex",
    "github",
    "chatgpt_export",
)


def _work_trace_id(prefix: str) -> str:
    """Return a short private trace id for a work-source control-plane action."""

    return f"{prefix}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:10]}"


@dataclass(frozen=True)
class WorkSourcePreflight:
    """One canonical work source with authorization and freshness kept separate."""

    kind: WorkSourceKind
    authorization_state: SourceState
    freshness_state: SourceState
    action_required: bool
    last_refreshed_at: str | None = None


@dataclass(frozen=True)
class WorkSourcesPreflight:
    """Read-only canonical freshness preflight over every explicit work source."""

    trace_id: str
    preflighted_at: str
    sources: tuple[WorkSourcePreflight, ...]
    knowledge_documents: int = 0
    codex_sessions: int = 0
    chatgpt_exports: int = 0
    buildlog_runs: int = 0
    last_synced: bool = False
    codex_folder_available: bool = False
    project_head: str | None = None
    project_branch: str | None = None
    project_dirty_count: int = 0
    project_new_commits: int | None = None
    project_indexed_at: str | None = None
    project_fact_count: int = 0
    github_account_login: str | None = None
    github_selected_repositories: int = 0
    github_evidence_refreshed_at: str | None = None

    @property
    def stale_kinds(self) -> tuple[WorkSourceKind, ...]:
        return tuple(source.kind for source in self.sources if source.action_required)

    @property
    def action_required(self) -> bool:
        return bool(self.stale_kinds)

    def by_kind(self, kind: WorkSourceKind) -> WorkSourcePreflight:
        for source in self.sources:
            if source.kind == kind:
                return source
        raise KeyError(kind)


@dataclass(frozen=True)
class CodexImportJobSnapshot:
    """Body-free persisted progress for one explicit Codex history import."""

    job_id: str
    phase: CodexImportPhase
    total: int
    processed: int
    running: int
    imported: int
    updated: int
    skipped: int
    failed: int
    failure_codes: tuple[str, ...]
    created_at: str
    updated_at: str


@dataclass
class _CodexImportJobRecord:
    job_id: str
    phase: CodexImportPhase
    total: int = 0
    processed: int = 0
    running: int = 0
    imported: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    failure_codes: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


def _codex_import_job_path(data_root: Path, job_id: str) -> Path:
    if _CODEX_IMPORT_JOB_ID.fullmatch(job_id) is None:
        raise WorkContextError("Codex import job ID is invalid.")
    return Path(data_root) / "knowledge" / "import-jobs" / f"{job_id}.json"


def _codex_inventory_path(data_root: Path) -> Path:
    return Path(data_root) / "knowledge" / "codex-source-inventory.json"


def _codex_source_fingerprint(path: Path) -> tuple[str, str]:
    """Return body-free path identity and change fingerprint for one regular source."""

    try:
        source_stat = path.lstat()
    except OSError as exc:
        raise WorkContextError("Codex source metadata is unavailable.") from exc
    if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
        raise WorkContextError("Codex source is not a regular file.")
    path_key = hashlib.sha256(str(path.absolute()).encode("utf-8")).hexdigest()
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "device": source_stat.st_dev,
                "inode": source_stat.st_ino,
                "size": source_stat.st_size,
                "modified_ns": source_stat.st_mtime_ns,
                "changed_ns": source_stat.st_ctime_ns,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return path_key, fingerprint


def _load_codex_inventory(data_root: Path) -> dict[str, str]:
    path = _codex_inventory_path(data_root)
    if not path.exists():
        return {}
    try:
        if path.is_symlink() or not path.is_file():
            raise WorkContextError("Codex source inventory is unavailable.")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise WorkContextError("Codex source inventory is unavailable.") from exc
    if not isinstance(payload, dict):
        raise WorkContextError("Codex source inventory is unavailable.")
    inventory = payload.get("sources")
    if not isinstance(inventory, dict):
        raise WorkContextError("Codex source inventory is unavailable.")
    normalized: dict[str, str] = {}
    for key, value in inventory.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, str)
            or _SHA256.fullmatch(key) is None
            or _SHA256.fullmatch(value) is None
        ):
            raise WorkContextError("Codex source inventory is unavailable.")
        normalized[key] = value
    return normalized


def _persist_codex_inventory(data_root: Path, inventory: dict[str, str]) -> None:
    path = _codex_inventory_path(data_root)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise WorkContextError("Codex source inventory storage is unsafe.")
    os.chmod(directory, 0o700)
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=".codex-source-inventory-",
        suffix=".tmp",
        dir=directory,
    )
    temporary = Path(raw_temporary)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(
                {"schema_version": "1.0", "sources": inventory},
                stream,
                ensure_ascii=True,
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _persist_codex_import_job(data_root: Path, record: _CodexImportJobRecord) -> None:
    path = _codex_import_job_path(data_root, record.job_id)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise WorkContextError("Codex import job storage is unsafe.")
    os.chmod(directory, 0o700)
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{record.job_id}-",
        suffix=".tmp",
        dir=directory,
    )
    temporary = Path(raw_temporary)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(record.__dict__, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _load_codex_import_job(data_root: Path, job_id: str) -> _CodexImportJobRecord:
    path = _codex_import_job_path(data_root, job_id)
    try:
        if path.is_symlink() or not path.is_file():
            raise WorkContextError("Codex import job is unavailable.")
        payload = json.loads(path.read_text(encoding="utf-8"))
        record = _CodexImportJobRecord(**payload)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WorkContextError("Codex import job is unavailable.") from exc
    if record.job_id != job_id or record.phase not in {
        "QUEUED",
        "DISCOVERING",
        "PROCESSING",
        "READY",
        "FAILED",
    }:
        raise WorkContextError("Codex import job is unavailable.")
    return record


class CodexImportJobManager:
    """Import Codex history off the HTTP thread with bounded parse concurrency."""

    def __init__(self, *, parse_workers: int = 4) -> None:
        self._parse_workers = max(1, min(4, parse_workers))
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="soloscale-codex-import",
        )
        self._lock = threading.Lock()
        self._jobs: dict[str, _CodexImportJobRecord] = {}

    def submit(self, *, data_root: Path, codex_home: Path | None = None) -> str:
        with self._lock:
            for existing in self._jobs.values():
                if existing.phase not in _TERMINAL_CODEX_IMPORT_PHASES:
                    return existing.job_id
            now = datetime.now(UTC)
            job_id = (
                f"codex-import-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:10]}"
            )
            record = _CodexImportJobRecord(job_id=job_id, phase="QUEUED")
            self._jobs[job_id] = record
            _persist_codex_import_job(data_root, record)
        self._executor.submit(self._execute, Path(data_root), codex_home, job_id)
        return job_id

    def get(self, data_root: Path, job_id: str) -> CodexImportJobSnapshot | None:
        if _CODEX_IMPORT_JOB_ID.fullmatch(job_id) is None:
            return None
        with self._lock:
            record = self._jobs.get(job_id)
            if record is not None:
                return self._snapshot(record)
        try:
            record = _load_codex_import_job(data_root, job_id)
        except WorkContextError:
            return None
        if record.phase not in _TERMINAL_CODEX_IMPORT_PHASES:
            record.phase = "FAILED"
            record.running = 0
            record.failure_codes.append("INTERRUPTED")
            record.updated_at = datetime.now(UTC).isoformat()
            _persist_codex_import_job(data_root, record)
        return self._snapshot(record)

    def latest(self, data_root: Path) -> CodexImportJobSnapshot | None:
        directory = Path(data_root) / "knowledge" / "import-jobs"
        try:
            candidates = sorted(
                (
                    path.stem
                    for path in directory.glob("codex-import-*.json")
                    if not path.is_symlink()
                    and _CODEX_IMPORT_JOB_ID.fullmatch(path.stem) is not None
                ),
                reverse=True,
            )
        except OSError:
            return None
        for job_id in candidates:
            snapshot = self.get(data_root, job_id)
            if snapshot is not None:
                return snapshot
        return None

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _execute(
        self,
        data_root: Path,
        codex_home: Path | None,
        job_id: str,
    ) -> None:
        selected_home = (
            Path(codex_home) if codex_home is not None else Path.home() / ".codex"
        )
        try:
            self._update(data_root, job_id, phase="DISCOVERING")
            paths = discover_codex_sources(selected_home)
            if not paths:
                self._finish_failed(data_root, job_id, "NO_SUPPORTED_HISTORY")
                return
            self._update(data_root, job_id, phase="PROCESSING", total=len(paths))
            store = KnowledgeStore(data_root)
            previous_inventory = _load_codex_inventory(data_root)
            next_inventory: dict[str, str] = {}
            pending: list[tuple[Path, str, str]] = []
            for path in paths:
                try:
                    path_key, source_fingerprint = _codex_source_fingerprint(path)
                except WorkContextError:
                    self._increment(
                        data_root,
                        job_id,
                        processed=1,
                        failed=1,
                        failure_code="SOURCE_METADATA_FAILED",
                    )
                    continue
                previous_fingerprint = previous_inventory.get(path_key)
                if previous_fingerprint == source_fingerprint:
                    next_inventory[path_key] = source_fingerprint
                    self._increment(
                        data_root,
                        job_id,
                        processed=1,
                        skipped=1,
                    )
                    continue
                if previous_fingerprint is not None:
                    next_inventory[path_key] = previous_fingerprint
                pending.append((path, path_key, source_fingerprint))

            def parse_one(
                item: tuple[Path, str, str],
            ) -> tuple[str, str, ParsedSource]:
                path, path_key, source_fingerprint = item
                self._increment(data_root, job_id, running=1)
                try:
                    return path_key, source_fingerprint, parse_codex_session(path)
                finally:
                    self._increment(data_root, job_id, running=-1)

            with ThreadPoolExecutor(
                max_workers=self._parse_workers,
                thread_name_prefix="soloscale-codex-parse",
            ) as parser:
                futures = [parser.submit(parse_one, item) for item in pending]
                for future in as_completed(futures):
                    try:
                        path_key, source_fingerprint, parsed = future.result()
                    except (ConversationIntakeError, OSError, ValueError):
                        self._increment(
                            data_root,
                            job_id,
                            processed=1,
                            failed=1,
                            failure_code="SESSION_PARSE_FAILED",
                        )
                        continue
                    try:
                        report = store.sync([parsed])
                    except (KnowledgeStoreError, OSError, ValueError):
                        self._increment(
                            data_root,
                            job_id,
                            processed=1,
                            failed=1,
                            failure_code="SESSION_SYNC_FAILED",
                        )
                        continue
                    if report.failed == 0:
                        next_inventory[path_key] = source_fingerprint
                    self._increment(
                        data_root,
                        job_id,
                        processed=1,
                        imported=report.imported,
                        updated=report.updated,
                        skipped=report.skipped,
                        failed=report.failed,
                        failure_code=(
                            "SESSION_SYNC_REJECTED" if report.failed else None
                        ),
                    )
            _persist_codex_inventory(data_root, next_inventory)
            snapshot = self.get(data_root, job_id)
            if snapshot is None or not (
                snapshot.imported + snapshot.updated + snapshot.skipped
            ):
                self._finish_failed(data_root, job_id, "NO_SESSIONS_IMPORTED")
                return
            self._update(data_root, job_id, phase="READY", running=0)
        except (ConversationIntakeError, KnowledgeStoreError, OSError, ValueError):
            self._finish_failed(data_root, job_id, "IMPORT_FAILED")

    def _finish_failed(self, data_root: Path, job_id: str, code: str) -> None:
        self._increment(data_root, job_id, failure_code=code)
        self._update(data_root, job_id, phase="FAILED", running=0)

    def _increment(
        self,
        data_root: Path,
        job_id: str,
        *,
        processed: int = 0,
        running: int = 0,
        imported: int = 0,
        updated: int = 0,
        skipped: int = 0,
        failed: int = 0,
        failure_code: str | None = None,
    ) -> None:
        with self._lock:
            record = self._jobs[job_id]
            record.processed += processed
            record.running = max(0, record.running + running)
            record.imported += imported
            record.updated += updated
            record.skipped += skipped
            record.failed += failed
            if failure_code is not None:
                record.failure_codes.append(failure_code)
            record.updated_at = datetime.now(UTC).isoformat()
            _persist_codex_import_job(data_root, record)

    def _update(
        self,
        data_root: Path,
        job_id: str,
        *,
        phase: CodexImportPhase | None = None,
        total: int | None = None,
        running: int | None = None,
    ) -> None:
        with self._lock:
            record = self._jobs[job_id]
            if phase is not None:
                record.phase = phase
            if total is not None:
                record.total = total
            if running is not None:
                record.running = running
            record.updated_at = datetime.now(UTC).isoformat()
            _persist_codex_import_job(data_root, record)

    @staticmethod
    def _snapshot(record: _CodexImportJobRecord) -> CodexImportJobSnapshot:
        return CodexImportJobSnapshot(
            job_id=record.job_id,
            phase=record.phase,
            total=record.total,
            processed=record.processed,
            running=record.running,
            imported=record.imported,
            updated=record.updated,
            skipped=record.skipped,
            failed=record.failed,
            failure_codes=tuple(record.failure_codes),
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


def refresh_selected_knowledge_sources(
    data_root: Path,
    *,
    include_codex: bool,
    codex_home: Path | None = None,
    chatgpt_exports: Sequence[Path] = (),
    buildlog_roots: Sequence[Path] = (),
) -> WorkImportResult:
    """Refresh only explicitly selected knowledge sources inside the packaged app."""

    parsed: list[ParsedSource] = []
    discovered = 0
    parse_failures = 0
    if include_codex:
        selected_home = codex_home or Path.home() / ".codex"
        try:
            codex_paths = discover_codex_sources(selected_home)
        except (ConversationIntakeError, OSError, ValueError):
            codex_paths = []
            parse_failures += 1
        discovered += len(codex_paths)
        for path in codex_paths:
            try:
                parsed.append(parse_codex_session(path))
            except (ConversationIntakeError, OSError, ValueError):
                parse_failures += 1
    for export_path in chatgpt_exports:
        try:
            export_sources = parse_chatgpt_export(export_path)
        except (ConversationIntakeError, OSError, ValueError):
            parse_failures += 1
            continue
        discovered += len(export_sources)
        parsed.extend(export_sources)
    for root in buildlog_roots:
        try:
            run_paths = discover_buildlog_runs(root)
        except (ConversationIntakeError, OSError, ValueError):
            parse_failures += 1
            continue
        discovered += len(run_paths)
        for run_path in run_paths:
            try:
                parsed.append(parse_buildlog_run(run_path))
            except (ConversationIntakeError, OSError, ValueError):
                parse_failures += 1
    if not parsed:
        return WorkImportResult(
            discovered=discovered,
            imported=0,
            updated=0,
            skipped=0,
            failed=parse_failures,
        )
    try:
        report = KnowledgeStore(Path(data_root)).sync(parsed)
    except (KnowledgeStoreError, OSError, ValueError) as exc:
        raise WorkContextError("Selected work sources could not be refreshed.") from exc
    return _import_result(
        report,
        discovered=discovered,
        parse_failures=parse_failures,
    )


def _count_private_runs(root: Path, directory_name: str, prefix: str) -> int:
    directory = root / directory_name
    if not directory.is_dir() or directory.is_symlink():
        return 0
    try:
        return sum(
            1
            for child in directory.iterdir()
            if child.name.startswith(prefix) and child.is_dir() and not child.is_symlink()
        )
    except OSError:
        return 0


def preflight_work_sources(
    data_root: Path,
    *,
    workspace_root: Path | None = None,
    github_connected: bool = False,
    home: Path | None = None,
) -> WorkSourcesPreflight:
    """Canonical read-only freshness preflight for every explicit work source.

    Never writes. Authorization (is this source connected/usable?) and freshness
    (is its indexed evidence current?) are reported as separate states so the UI
    and refresh routes share one source of truth.
    """

    root = Path(data_root)
    trace_id = _work_trace_id("work-preflight")
    preflighted_at = datetime.now(UTC).isoformat()
    knowledge_documents = 0
    codex_sessions = 0
    chatgpt_exports = 0
    buildlog_runs = 0
    last_synced = False
    knowledge_database = root / "knowledge" / "index.sqlite3"
    if knowledge_database.is_file() and not knowledge_database.is_symlink():
        try:
            knowledge_status = KnowledgeStore(root).status()
        except (KnowledgeStoreError, OSError, ValueError):
            knowledge_status = None
        if knowledge_status is not None:
            knowledge_documents = knowledge_status.documents
            codex_sessions = knowledge_status.source_counts.get("codex_session", 0)
            chatgpt_exports = knowledge_status.source_counts.get("chatgpt_export", 0)
            buildlog_runs = knowledge_status.source_counts.get("buildlog_run", 0)
            last_synced = knowledge_status.last_synced_at is not None

    selected_home = home or Path.home()
    codex_home = selected_home / ".codex"
    codex_folder_available = any(
        (codex_home / name).is_dir() and not (codex_home / name).is_symlink()
        for name in ("sessions", "archived_sessions")
    )
    project_connected = bool(
        workspace_root is not None
        and workspace_root.is_dir()
        and not workspace_root.is_symlink()
        and (workspace_root / ".git").exists()
        and not (workspace_root / ".git").is_symlink()
    )
    project_authorization_state: SourceState = (
        "READY" if project_connected else "NOT_CONNECTED"
    )
    project_freshness_state: SourceState = "UNAVAILABLE"
    project_head: str | None = None
    project_branch: str | None = None
    project_dirty_count = 0
    project_new_commits: int | None = None
    project_indexed_at: str | None = None
    project_fact_count = 0
    if project_connected and workspace_root is not None:
        try:
            current_source, current_items = inspect_git_repository(workspace_root)
            project_head = current_source.native_id[:7]
            project_branch = current_source.metadata.get("branch")
            project_dirty_count = int(
                current_source.metadata.get("dirty_count", "0")
            )
            stored_snapshot = (
                EvidenceHub(root).git_repository_snapshot(workspace_root)
                if EvidenceHub.catalog_exists(root)
                else None
            )
        except (EvidenceHubError, OSError, ValueError):
            project_authorization_state = "NEEDS_ATTENTION"
            project_freshness_state = "NEEDS_ATTENTION"
        else:
            if stored_snapshot is None:
                project_freshness_state = "STALE"
            else:
                stored_source, stored_items = stored_snapshot
                project_freshness_state = (
                    "READY"
                    if stored_source.content_sha256 == current_source.content_sha256
                    else "STALE"
                )
                project_indexed_at = stored_source.captured_at.isoformat()
                stored_commits = {
                    item.native_id
                    for item in stored_items
                    if item.evidence_type == "git_commit"
                }
                current_commits = [
                    item.native_id
                    for item in current_items
                    if item.evidence_type == "git_commit"
                ]
                project_new_commits = sum(
                    commit not in stored_commits for commit in current_commits
                )
                project_fact_count = len(stored_commits)
    github_account_login: str | None = None
    github_selected_repositories = 0
    github_authorization_state: SourceState = "NOT_CONNECTED"
    github_freshness_state: SourceState = "UNAVAILABLE"
    github_evidence_refreshed_at: str | None = None
    if github_connected:
        github_authorization_state = "AVAILABLE"
        github_freshness_state = "AVAILABLE"
        try:
            github_connection = GitHubConnectionStore(root).load()
        except (OSError, ValueError):
            github_authorization_state = "NEEDS_ATTENTION"
            github_freshness_state = "NEEDS_ATTENTION"
        else:
            if github_connection is not None:
                github_authorization_state = "READY"
                github_account_login = github_connection.account_login
                github_selected_repositories = len(
                    github_connection.selected_repository_ids
                )
                if github_connection.last_error_code:
                    github_authorization_state = "NEEDS_ATTENTION"
                    github_freshness_state = "NEEDS_ATTENTION"
                elif github_selected_repositories and github_connection.evidence_refreshed_at:
                    github_freshness_state = "READY"
                    github_evidence_refreshed_at = (
                        github_connection.evidence_refreshed_at.isoformat()
                    )
                elif github_selected_repositories:
                    github_freshness_state = "STALE"
                else:
                    github_freshness_state = "AVAILABLE"
            else:
                github_freshness_state = "AVAILABLE"
    codex_freshness_state: SourceState = (
        "READY"
        if codex_sessions
        else "NEEDS_ATTENTION"
        if not codex_folder_available
        else "AVAILABLE"
    )
    chatgpt_freshness_state: SourceState = (
        "READY" if chatgpt_exports else "UNAVAILABLE"
    )
    sources = (
        WorkSourcePreflight(
            kind="local_git",
            authorization_state=project_authorization_state,
            freshness_state=project_freshness_state,
            action_required=project_freshness_state in {"STALE", "NEEDS_ATTENTION"},
            last_refreshed_at=project_indexed_at,
        ),
        WorkSourcePreflight(
            kind="codex",
            authorization_state="READY" if codex_sessions or codex_folder_available else "NOT_CONNECTED",
            freshness_state=codex_freshness_state,
            action_required=False,
        ),
        WorkSourcePreflight(
            kind="github",
            authorization_state=github_authorization_state,
            freshness_state=github_freshness_state,
            action_required=github_freshness_state in {"STALE", "NEEDS_ATTENTION"},
            last_refreshed_at=github_evidence_refreshed_at,
        ),
        WorkSourcePreflight(
            kind="chatgpt_export",
            authorization_state="READY" if chatgpt_exports else "NOT_CONNECTED",
            freshness_state=chatgpt_freshness_state,
            action_required=False,
        ),
    )
    return WorkSourcesPreflight(
        trace_id=trace_id,
        preflighted_at=preflighted_at,
        sources=sources,
        knowledge_documents=knowledge_documents,
        codex_sessions=codex_sessions,
        chatgpt_exports=chatgpt_exports,
        buildlog_runs=buildlog_runs,
        last_synced=last_synced,
        codex_folder_available=codex_folder_available,
        project_head=project_head,
        project_branch=project_branch,
        project_dirty_count=project_dirty_count,
        project_new_commits=project_new_commits,
        project_indexed_at=project_indexed_at,
        project_fact_count=project_fact_count,
        github_account_login=github_account_login,
        github_selected_repositories=github_selected_repositories,
        github_evidence_refreshed_at=github_evidence_refreshed_at,
    )


def load_work_context(
    data_root: Path,
    *,
    workspace_root: Path | None = None,
    github_connected: bool = False,
    home: Path | None = None,
) -> WorkContextSnapshot:
    """Load safe product counts without creating a catalog or reading source bodies."""

    root = Path(data_root)
    preflight = preflight_work_sources(
        root,
        workspace_root=workspace_root,
        github_connected=github_connected,
        home=home,
    )
    local_git = preflight.by_kind("local_git")
    github = preflight.by_kind("github")
    codex = preflight.by_kind("codex")
    chatgpt = preflight.by_kind("chatgpt_export")
    project_connected = local_git.authorization_state in {"READY", "NEEDS_ATTENTION"}
    project_name = workspace_root.name if project_connected and workspace_root else None
    reusable_items = 0
    if EvidenceHub.catalog_exists(root):
        try:
            reusable_items = EvidenceHub(root).status().evidence_count
        except (EvidenceHubError, OSError, ValueError):
            reusable_items = 0
    return WorkContextSnapshot(
        resume_runs=_count_private_runs(root, "resume-runs", "resume-"),
        codex_sessions=preflight.codex_sessions,
        chatgpt_exports=preflight.chatgpt_exports,
        buildlog_runs=preflight.buildlog_runs,
        knowledge_documents=preflight.knowledge_documents,
        reusable_items=reusable_items,
        project_connected=project_connected,
        project_name=project_name,
        project_authorization_state=local_git.authorization_state,
        project_freshness_state=local_git.freshness_state,
        project_head=preflight.project_head,
        project_branch=preflight.project_branch,
        project_dirty_count=preflight.project_dirty_count,
        project_new_commits=preflight.project_new_commits,
        project_indexed_at=preflight.project_indexed_at,
        project_fact_count=preflight.project_fact_count,
        github_connected=github_connected,
        github_account_login=preflight.github_account_login,
        github_selected_repositories=preflight.github_selected_repositories,
        github_authorization_state=github.authorization_state,
        github_freshness_state=github.freshness_state,
        codex_freshness_state=codex.freshness_state,
        chatgpt_freshness_state=chatgpt.freshness_state,
        codex_folder_available=preflight.codex_folder_available,
        last_synced=preflight.last_synced,
        preflight_trace_id=preflight.trace_id,
        preflight_at=preflight.preflighted_at,
    )


def _import_result(report: SyncReport, *, discovered: int, parse_failures: int = 0) -> WorkImportResult:
    return WorkImportResult(
        discovered=discovered,
        imported=report.imported,
        updated=report.updated,
        skipped=report.skipped,
        failed=report.failed + parse_failures,
        trace_id=_work_trace_id("work-import"),
    )


def refresh_work_source(
    data_root: Path,
    kind: WorkSourceKind,
    *,
    workspace_root: Path | None = None,
    codex_home: Path | None = None,
    chatgpt_export: Path | None = None,
    github_refresh: Callable[[], None] | None = None,
) -> WorkImportResult:
    """Route one explicit incremental refresh to its canonical source implementation."""

    root = Path(data_root)
    if kind == "local_git":
        if workspace_root is None:
            raise WorkContextError("Select one local Git repository before refreshing it.")
        receipt = refresh_local_project_evidence(root, repository_root=workspace_root)
        if receipt.status.value != "succeeded":
            raise WorkContextError("Local Git project evidence refresh failed.")
        return WorkImportResult(
            discovered=0,
            imported=0,
            updated=0,
            skipped=0,
            failed=0,
            trace_id=_work_trace_id("work-refresh"),
        )
    if kind == "codex":
        return import_codex_history(root, codex_home=codex_home)
    if kind == "chatgpt_export":
        if chatgpt_export is None:
            raise WorkContextError("Choose a ChatGPT export file before importing it.")
        return import_chatgpt_export(root, chatgpt_export)
    if kind == "github":
        if github_refresh is None:
            raise WorkContextError("GitHub refresh requires the Desktop App connection.")
        github_refresh()
        return WorkImportResult(
            discovered=0,
            imported=0,
            updated=0,
            skipped=0,
            failed=0,
            trace_id=_work_trace_id("work-refresh"),
        )
    raise WorkContextError(f"Unknown work source kind: {kind}")


def import_codex_history(
    data_root: Path,
    *,
    codex_home: Path | None = None,
) -> WorkImportResult:
    """Import only the standard Codex source selected by an explicit user action."""

    selected_home = Path(codex_home) if codex_home is not None else Path.home() / ".codex"
    try:
        paths = discover_codex_sources(selected_home)
    except (ConversationIntakeError, OSError, ValueError) as exc:
        raise WorkContextError("Codex history could not be inspected safely.") from exc
    if not paths:
        raise WorkContextError("No supported Codex history was found.")

    parsed: list[ParsedSource] = []
    parse_failures = 0
    for path in paths:
        try:
            parsed.append(parse_codex_session(path))
        except (ConversationIntakeError, OSError, ValueError):
            parse_failures += 1
    if not parsed:
        raise WorkContextError("No supported Codex history could be imported.")
    try:
        report = KnowledgeStore(Path(data_root)).sync(parsed)
    except (KnowledgeStoreError, OSError, ValueError) as exc:
        raise WorkContextError("Codex history could not be saved safely.") from exc
    return _import_result(report, discovered=len(paths), parse_failures=parse_failures)


def import_chatgpt_export(data_root: Path, export_path: Path) -> WorkImportResult:
    """Import one user-selected ChatGPT JSON/ZIP without modifying the source file."""

    selected = Path(export_path)
    if (
        not selected.is_file()
        or selected.is_symlink()
        or selected.suffix.casefold() not in {".json", ".zip"}
    ):
        raise WorkContextError("Choose one regular ChatGPT JSON or ZIP export.")
    try:
        parsed = parse_chatgpt_export(selected)
    except (ConversationIntakeError, OSError, ValueError) as exc:
        raise WorkContextError("The selected ChatGPT export could not be imported safely.") from exc
    if not parsed:
        raise WorkContextError("The selected ChatGPT export contains no supported conversations.")
    try:
        report = KnowledgeStore(Path(data_root)).sync(parsed)
    except (KnowledgeStoreError, OSError, ValueError) as exc:
        raise WorkContextError("The selected ChatGPT export could not be saved safely.") from exc
    return _import_result(report, discovered=len(parsed))


def import_chatgpt_export_bytes(
    data_root: Path,
    *,
    filename: str,
    content: bytes,
) -> WorkImportResult:
    """Import a browser-selected export through a private, short-lived local file."""

    suffix = Path(filename).suffix.casefold()
    if suffix not in {".json", ".zip"} or not content:
        raise WorkContextError("Choose one non-empty ChatGPT JSON or ZIP export.")
    temporary_path: Path | None = None
    descriptor: int | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(prefix="soloscale-chatgpt-", suffix=suffix)
        temporary_path = Path(raw_path)
        os.chmod(temporary_path, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(content)
        return import_chatgpt_export(data_root, temporary_path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def render_work_context_strip(
    snapshot: WorkContextSnapshot,
    locale: UILocale = DEFAULT_UI_LOCALE,
) -> str:
    """Render the compact shared foundation above the three outcome cards."""

    resume = ui_text(locale, "简历 ✓", "Resume ✓") if snapshot.resume_runs else ui_text(locale, "简历待添加", "Add resume")
    conversations = ui_text(locale, f"{snapshot.ai_conversations} 个 AI 对话", f"{snapshot.ai_conversations} AI conversations") if snapshot.ai_conversations else ui_text(locale, "AI 对话待添加", "Add AI conversations")
    if snapshot.project_connected:
        branch = snapshot.project_branch or snapshot.project_head or ui_text(locale, "当前 HEAD", "HEAD")
        project_identity = f"{snapshot.project_name or 'Git'} · {branch}"
        project_line = ui_text(locale, f"当前工作项目：{project_identity}", f"Current work project: {project_identity}")
        context_note = ui_text(
            locale,
            f"{resume} · {conversations} · 工程证据{'需刷新' if snapshot.project_state == 'STALE' else '已更新'}",
            f"{resume} · {conversations} · engineering evidence {'needs refresh' if snapshot.project_state == 'STALE' else 'current'}",
        )
    else:
        project_line = ui_text(locale, "当前工作项目：未选择", "Current work project: not selected")
        context_note = f"{resume} · {conversations}"
    return f"""<section class="work-context-strip" aria-label="{html.escape(ui_text(locale, '我的工作资料', 'Your work'))}">
  <div><span class="kicker">{html.escape(ui_text(locale, '我的工作资料', 'Your work'))}</span>
  <strong>{html.escape(project_line)}</strong><span class="context-note">{html.escape(context_note)}</span></div>
  <p>{html.escape(ui_text(locale, 'Career / Learning 会在适当时复用该项目的工程证据，而不是自动写入每一份简历。', 'Career and Learning reuse this project evidence where appropriate; it is never written into every resume automatically.'))}</p>
  <a href="{ui_url('/work', locale)}">{html.escape(ui_text(locale, '添加或管理资料', 'Add or manage work'))}<span aria-hidden="true">→</span></a>
</section>"""


def render_use_my_work(
    snapshot: WorkContextSnapshot,
    locale: UILocale = DEFAULT_UI_LOCALE,
    *,
    boundary: str,
    return_path: str | None = None,
) -> str:
    """Render a truthful, non-configurable summary for one outcome page."""

    if snapshot.has_work:
        project_status = ""
        if snapshot.project_connected:
            project_status = ui_text(
                locale,
                " 本地 Git 工程证据需要刷新。"
                if snapshot.project_state == "STALE"
                else " 本地 Git 工程证据已更新。",
                " Local Git evidence needs refresh."
                if snapshot.project_state == "STALE"
                else " Local Git evidence is current.",
            )
        project_count = 1 if snapshot.project_connected else 0
        project_label = "local project" if project_count == 1 else "local projects"
        summary = ui_text(
            locale,
            f"已添加 {snapshot.knowledge_documents} 份资料、{snapshot.resume_runs} 份简历记录和 {project_count} 个本地项目。",
            f"Available: {snapshot.knowledge_documents} records, {snapshot.resume_runs} resume runs, and {project_count} {project_label}.",
        ) + project_status
    else:
        summary = ui_text(
            locale,
            "还没有添加工作资料；你仍然可以先完成当前任务。",
            "No work has been added yet. You can still complete this task first.",
        )
    manage_url = (
        ui_url("/work", locale, **{"return_path": return_path})
        if return_path
        else ui_url("/work", locale)
    )
    return f"""<aside class="use-my-work">
  <div><span class="kicker">{html.escape(ui_text(locale, '使用我的工作资料', 'Use my work'))}</span><strong>{html.escape(summary)}</strong></div>
  <p>{html.escape(boundary)}</p>
  <a href="{manage_url}">{html.escape(ui_text(locale, '管理资料', 'Manage work'))} →</a>
</aside>"""


def _source_row(
    *,
    key: str,
    state: SourceState,
    locale: UILocale,
    name: str,
    summary: str,
    detail: str = "",
    actions: str = "",
    kind: str | None = None,
    authorization_state: SourceState | None = None,
) -> str:
    """Render one compact source row with a shared semantic state."""

    detail_html = f"<p>{html.escape(detail)}</p>" if detail else ""
    actions_html = f'<div class="source-actions">{actions}</div>' if actions else ""
    kind_html = f' data-source-kind="{html.escape(kind)}"' if kind else ""
    authorization_html = (
        f' data-authorization-state="{html.escape(authorization_state)}"'
        if authorization_state
        else ""
    )
    return f'''<article class="source-row" data-source="{html.escape(key)}" data-freshness-state="{html.escape(state)}"{kind_html}{authorization_html}>
  {render_source_state(state, locale)}
  <div class="source-copy"><h3>{html.escape(name)}</h3><strong>{html.escape(summary)}</strong>{detail_html}</div>
  {actions_html}
</article>'''


def work_page(
    *,
    data_root: Path,
    workspace_root: Path | None,
    locale: UILocale = DEFAULT_UI_LOCALE,
    desktop_mode: bool,
    github_token_configured: bool = False,
    github_connect_available: bool = False,
    chatgpt_export_selected: bool = False,
    codex_job: CodexImportJobSnapshot | None = None,
    notice: str | None = None,
    error: str | None = None,
    return_path: str | None = None,
) -> str:
    """Render the truthful Work Context onboarding and source setup page."""

    if not return_path or not return_path.startswith("/") or return_path.startswith("//"):
        return_path = "/"
    snapshot = load_work_context(
        data_root,
        workspace_root=workspace_root,
        github_connected=github_token_configured,
    )
    notice_html = f'<div class="notice" role="status">{html.escape(notice)}</div>' if notice else ""
    error_html = f'<div class="error" role="alert">{html.escape(error)}</div>' if error else ""
    codex_job_active = bool(
        codex_job is not None
        and codex_job.phase not in _TERMINAL_CODEX_IMPORT_PHASES
    )
    codex_job_needs_attention = bool(
        codex_job is not None
        and (codex_job.phase == "FAILED" or codex_job.failed > 0)
    )
    stale_sources: list[str] = []
    if snapshot.project_state == "STALE":
        stale_sources.append("Local Git")
    if snapshot.github_state == "STALE":
        stale_sources.append("GitHub")
    preflight_notice = ""
    if stale_sources:
        preflight_notice = ui_text(
            locale,
            f"已自动预检：以下资料需要刷新：{', '.join(stale_sources)}。每次只增量更新所选来源。",
            f"Automatic preflight found sources that need a refresh: {', '.join(stale_sources)}. Refresh each selected source incrementally.",
        )
        preflight_notice = (
            f'<div class="notice preflight-notice" role="status" data-preflight-trace-id="{html.escape(snapshot.preflight_trace_id or "")}" data-preflight-at="{html.escape(snapshot.preflight_at or "")}">{html.escape(preflight_notice)}</div>'
        )
    codex_progress = ""
    codex_job_marker = ""
    if codex_job_active and codex_job is not None:
        codex_progress = ui_text(
            locale,
            f"已处理 {codex_job.processed}/{codex_job.total} · 正在处理 {codex_job.running} · 失败 {codex_job.failed} · 跳过 {codex_job.skipped}",
            f"Processed {codex_job.processed}/{codex_job.total} · running {codex_job.running} · failed {codex_job.failed} · skipped {codex_job.skipped}",
        )
        codex_job_marker = (
            '<div id="codex-import-monitor" hidden '
            f'data-job-id="{html.escape(codex_job.job_id)}"></div>'
        )

    def processing_form_attributes(source: str, copy: str) -> str:
        return (
            f'data-processing-source="{html.escape(source)}" '
            f'data-processing-copy="{html.escape(copy)}" '
            f'data-progress-label="{html.escape(copy)}"'
        )

    codex_name = "Codex"
    codex_processing = ui_text(
        locale,
        "正在建立本地索引 · 完成后本页会自动更新",
        "Building the local index · this page will update when complete",
    )
    codex_action = ""
    if snapshot.codex_folder_available and not codex_job_active:
        codex_action = f'''<form method="post" action="/work/import-codex" {processing_form_attributes(codex_name, codex_processing)}>
  <input type="hidden" name="ui_locale" value="{locale}" />
  <label class="consent"><input type="checkbox" name="approve" value="yes" required />{html.escape(ui_text(locale, "我批准这次读取标准 Codex 历史目录。", "I approve reading the standard Codex history folder for this import."))}</label>
  <button class="source-control" type="submit" data-loading-label="{html.escape(ui_text(locale, '开始处理…', 'Starting…'))}">{html.escape(ui_text(locale, "更新 Codex 历史" if snapshot.codex_sessions else "添加 Codex 历史", "Update Codex history" if snapshot.codex_sessions else "Add Codex history"))}</button>
</form>'''

    chatgpt_name = "ChatGPT Export"
    chatgpt_processing = ui_text(
        locale,
        "正在导入你选择的文件 · 完成后本页会自动更新",
        "Importing the file you selected · this page will update when complete",
    )
    if desktop_mode and chatgpt_export_selected:
        chatgpt_action = f'''<form method="post" action="/work/import-chatgpt" {processing_form_attributes(chatgpt_name, chatgpt_processing)}>
  <input type="hidden" name="ui_locale" value="{locale}" />
  <label class="consent"><input type="checkbox" name="approve" value="yes" required />{html.escape(ui_text(locale, "我批准读取刚刚选择的导出文件。", "I approve reading the export I just selected."))}</label>
  <button class="source-control" type="submit" data-loading-label="{html.escape(ui_text(locale, '开始导入…', 'Starting import…'))}">{html.escape(ui_text(locale, "导入已选择的文件", "Import selected export"))}</button>
</form>'''
    elif desktop_mode:
        chatgpt_action = f'<a class="source-control" href="soloscale://choose-chatgpt-export">{html.escape(ui_text(locale, "选择导出文件", "Choose export file"))}</a>'
    else:
        chatgpt_action = f'''<form method="post" action="/work/import-chatgpt" enctype="multipart/form-data" {processing_form_attributes(chatgpt_name, chatgpt_processing)}>
  <input type="hidden" name="ui_locale" value="{locale}" />
  <input type="file" name="chatgpt_export" accept=".json,.zip,application/json,application/zip" required />
  <label class="consent"><input type="checkbox" name="approve" value="yes" required />{html.escape(ui_text(locale, "我批准读取这个导出文件。", "I approve reading this export."))}</label>
  <button class="source-control" type="submit" data-loading-label="{html.escape(ui_text(locale, '开始导入…', 'Starting import…'))}">{html.escape(ui_text(locale, "导入 ChatGPT 记录", "Import ChatGPT history"))}</button>
</form>'''

    connected_rows: list[str] = []
    add_more_rows: list[str] = []

    resume_actions = f'<a class="source-control" href="{ui_url("/resume#resume-form", locale)}">{html.escape(ui_text(locale, "上传或更新简历", "Upload or update resume"))}</a>'
    if snapshot.resume_runs:
        connected_rows.append(
            _source_row(
                key="resume-library",
                state="READY",
                locale=locale,
                name="Resume Library",
                summary=ui_text(locale, f"{snapshot.resume_runs} 份简历草稿", f"{snapshot.resume_runs} resume drafts"),
                detail=ui_text(locale, "简历草稿用于生成与复核；不会自动变成投递记录。", "Resume drafts are for generation and review; they never become application records automatically."),
                actions=resume_actions,
                kind="resume-library",
            )
        )
    else:
        add_more_rows.append(
            _source_row(
                key="resume-library",
                state="AVAILABLE",
                locale=locale,
                name="Resume Library",
                summary=ui_text(locale, "还没有简历草稿", "No resume drafts yet"),
                detail=ui_text(locale, "上传现有简历后即可开始。", "Upload your current resume to get started."),
                actions=f'<a class="source-control" href="{ui_url("/resume#resume-form", locale)}">{html.escape(ui_text(locale, "上传简历", "Upload resume"))}</a>',
                kind="resume-library",
            )
        )

    if snapshot.project_connected:
        project_processing = ui_text(
            locale,
            "正在准备本地项目快照 · 完成后本页会自动更新",
            "Preparing the local project snapshot · this page will update when complete",
        )
        change_project = (
            f'<a class="source-control" href="soloscale://choose-work-repository">{html.escape(ui_text(locale, "更换项目", "Change project"))}</a>'
            if desktop_mode
            else f'<span class="source-help">{html.escape(ui_text(locale, "可在 macOS App 中更换项目。", "Change the project from the macOS app."))}</span>'
        )
        prepare_project = f'''<form method="post" action="/work/refresh" {processing_form_attributes("Local Git", project_processing)}>
  <input type="hidden" name="ui_locale" value="{locale}" />
  <button class="source-control" type="submit" data-loading-label="{html.escape(ui_text(locale, '正在刷新…', 'Refreshing…'))}">{html.escape(ui_text(locale, "刷新工程证据", "Refresh project evidence"))}</button>
</form>'''
        freshness_summary = ui_text(
            locale,
            f"{snapshot.project_name or 'Git'} · 工程证据已更新",
            f"{snapshot.project_name or 'Git'} · evidence current",
        )
        if snapshot.project_state == "STALE":
            freshness_summary = ui_text(
                locale,
                f"{snapshot.project_name or 'Git'} · 工程证据已过期",
                f"{snapshot.project_name or 'Git'} · evidence stale",
            )
        elif snapshot.project_state == "NEEDS_ATTENTION":
            freshness_summary = ui_text(
                locale,
                f"{snapshot.project_name or 'Git'} · 无法读取当前状态",
                f"{snapshot.project_name or 'Git'} · current state unavailable",
            )
        state_details = [
            value
            for value in (
                f"HEAD {snapshot.project_head}" if snapshot.project_head else None,
                snapshot.project_branch,
                ui_text(
                    locale,
                    f"{snapshot.project_dirty_count} 个未提交变更",
                    f"{snapshot.project_dirty_count} uncommitted changes",
                ),
                (
                    ui_text(
                        locale,
                        f"{snapshot.project_new_commits} 个新提交待索引",
                        f"{snapshot.project_new_commits} new commits to index",
                    )
                    if snapshot.project_new_commits
                    else None
                ),
            )
            if value
        ]
        connected_rows.append(
            _source_row(
                key="local-git",
                state=snapshot.project_state,
                locale=locale,
                name="Local Git",
                summary=freshness_summary,
                detail=" · ".join(state_details)
                + ui_text(
                    locale,
                    "。只保存提交摘要与变更指纹，不保存或上传项目源码。",
                    ". Only commit summaries and change fingerprints are stored; project source is neither stored nor uploaded.",
                ),
                actions=change_project + prepare_project,
                kind="local-git",
                authorization_state=snapshot.project_authorization_state,
            )
        )
    else:
        project_action = (
            f'<a class="source-control" href="soloscale://choose-work-repository">{html.escape(ui_text(locale, "选择本地 Git 项目", "Choose a local Git project"))}</a>'
            if desktop_mode
            else f'<span class="source-help">{html.escape(ui_text(locale, "请在 macOS App 中选择目录。", "Choose a folder from the macOS app."))}</span>'
        )
        add_more_rows.append(
            _source_row(
                key="local-git",
                state="AVAILABLE",
                locale=locale,
                name="Local Git",
                summary=ui_text(locale, "还没有选择项目", "No project selected"),
                detail=ui_text(locale, "只使用你通过系统选择器明确授权的 Git 项目。", "Only a Git project you explicitly choose through the system picker is used."),
                actions=project_action,
                kind="local-git",
                authorization_state=snapshot.project_authorization_state,
            )
        )

    if codex_job_active:
        connected_rows.append(
            _source_row(
                key="codex",
                state="PROCESSING",
                locale=locale,
                name=codex_name,
                summary=codex_progress,
                detail=ui_text(
                    locale,
                    "后台增量处理；你可以继续使用其他页面。单个会话失败不会停止其余会话。",
                    "Incremental processing continues in the background. You can keep using other pages, and one failed session does not stop the rest.",
                ),
                kind="codex",
            )
        )
    elif codex_job_needs_attention and codex_job is not None:
        failed_target = connected_rows if snapshot.codex_sessions else add_more_rows
        incomplete = codex_job.phase == "FAILED"
        failed_target.append(
            _source_row(
                key="codex",
                state="NEEDS_ATTENTION",
                locale=locale,
                name=codex_name,
                summary=ui_text(
                    locale,
                    (
                        f"处理未完成 · 已处理 {codex_job.processed}/{codex_job.total} · 失败 {codex_job.failed}"
                        if incomplete
                        else f"更新完成，但 {codex_job.failed} 个会话需要处理"
                    ),
                    (
                        f"Import incomplete · processed {codex_job.processed}/{codex_job.total} · failed {codex_job.failed}"
                        if incomplete
                        else f"Update completed with {codex_job.failed} sessions needing attention"
                    ),
                ),
                detail=ui_text(
                    locale,
                    "已存在的资料保持不变；可以重新更新。",
                    "Existing work remains unchanged; you can run the update again.",
                ),
                actions=codex_action,
                kind="codex",
            )
        )
    elif snapshot.codex_sessions:
        connected_rows.append(
            _source_row(
                key="codex",
                state="READY",
                locale=locale,
                name=codex_name,
                summary=ui_text(locale, f"已导入 {snapshot.codex_sessions} 个会话", f"{snapshot.codex_sessions} sessions imported"),
                detail=ui_text(locale, "只有你明确批准导入时才会读取标准历史目录。", "The standard history folder is read only after your explicit import approval."),
                actions=codex_action,
                kind="codex",
            )
        )
    elif snapshot.codex_folder_available:
        add_more_rows.append(
            _source_row(
                key="codex",
                state="AVAILABLE",
                locale=locale,
                name=codex_name,
                summary=ui_text(locale, "这台 Mac 上有可添加的 Codex 资料", "Codex data is available to add on this Mac"),
                detail=ui_text(locale, "导入前只确认标准目录存在，不读取正文。", "Before approval, SoloScale checks only that the standard folder exists, not its contents."),
                actions=codex_action,
                kind="codex",
            )
        )
    else:
        add_more_rows.append(
            _source_row(
                key="codex",
                state="NEEDS_ATTENTION",
                locale=locale,
                name=codex_name,
                summary=ui_text(locale, "未找到标准 Codex 历史目录", "No standard Codex history folder found"),
                detail=ui_text(locale, "现有资料不受影响；此页不会扫描其他文件夹。", "Existing work is unaffected, and this page does not scan other folders."),
                kind="codex",
            )
        )

    if snapshot.chatgpt_exports:
        connected_rows.append(
            _source_row(
                key="chatgpt-export",
                state="READY",
                locale=locale,
                name=chatgpt_name,
                summary=ui_text(locale, f"已导入 {snapshot.chatgpt_exports} 个对话", f"{snapshot.chatgpt_exports} conversations imported"),
                detail=ui_text(locale, "只读取你主动选择并批准导入的官方导出文件。", "Only an official export you explicitly choose and approve is read."),
                actions=chatgpt_action,
                kind="chatgpt-export",
            )
        )
    else:
        add_more_rows.append(
            _source_row(
                key="chatgpt-export",
                state="NEEDS_ATTENTION" if chatgpt_export_selected else "AVAILABLE",
                locale=locale,
                name=chatgpt_name,
                summary=ui_text(locale, "已选择文件，等待你批准导入", "File selected; waiting for your import approval") if chatgpt_export_selected else ui_text(locale, "导入 ChatGPT 官方导出的 JSON 或 ZIP", "Import an official ChatGPT JSON or ZIP export"),
                detail=ui_text(locale, "SoloScale 只读取你主动选择的文件。", "SoloScale reads only the file you explicitly choose."),
                actions=chatgpt_action,
                kind="chatgpt-export",
            )
        )

    if snapshot.github_connected:
        github_actions = (
            f'<a class="source-control" href="{ui_url("/work/github", locale)}">{html.escape(ui_text(locale, "管理仓库", "Manage repositories"))}</a>'
        )
        if snapshot.github_selected_repositories:
            github_actions += f'''<form method="post" action="/work/github/refresh" {processing_form_attributes("GitHub", ui_text(locale, "正在读取已选仓库的只读元数据", "Reading read-only metadata for selected repositories"))}>
  <input type="hidden" name="ui_locale" value="{locale}" />
  <button class="source-control" type="submit" data-loading-label="{html.escape(ui_text(locale, '正在刷新…', 'Refreshing…'))}">{html.escape(ui_text(locale, "刷新", "Refresh"))}</button>
</form>'''
        github_actions += f'<a class="source-control danger-control" href="soloscale://disconnect-github">{html.escape(ui_text(locale, "断开连接", "Disconnect"))}</a>'
        if snapshot.github_account_login:
            github_summary = ui_text(
                locale,
                f"{snapshot.github_account_login} · 已选择 {snapshot.github_selected_repositories} 个仓库",
                f"{snapshot.github_account_login} · {snapshot.github_selected_repositories} repositories selected",
            )
        else:
            github_summary = ui_text(locale, "已连接；请选择仓库", "Connected; choose repositories")
        connected_rows.append(
            _source_row(
                key="github",
                state=snapshot.github_state,
                locale=locale,
                name="GitHub",
                summary=github_summary,
                detail=ui_text(
                    locale,
                    "只读取你授权并在 SoloScale 中明确选择的仓库元数据；不会修改 GitHub。",
                    "Reads metadata only from repositories authorized on GitHub and explicitly selected in SoloScale; GitHub is never modified.",
                ),
                actions=github_actions,
                kind="github",
                authorization_state=snapshot.github_authorization_state,
            )
        )
    else:
        github_action = ""
        github_detail = ui_text(
            locale,
            "这个 App 包尚未配置 GitHub App Client ID。",
            "This app build does not yet include a GitHub App client ID.",
        )
        if desktop_mode and github_connect_available:
            github_action = f'<a class="source-control" href="soloscale://connect-github">{html.escape(ui_text(locale, "连接 GitHub", "Connect GitHub"))}</a>'
            github_detail = ui_text(
                locale,
                "通过细粒度只读 GitHub App 授权；访问令牌只保存在 macOS Keychain。",
                "Uses a fine-grained read-only GitHub App; the access token is stored only in macOS Keychain.",
            )
        add_more_rows.append(
            _source_row(
                key="github",
                state="NOT_CONNECTED",
                locale=locale,
                name="GitHub",
                summary=ui_text(locale, "尚未连接 GitHub", "GitHub is not connected"),
                detail=github_detail,
                actions=github_action,
                kind="github",
                authorization_state=snapshot.github_authorization_state,
            )
        )

    connected_html = "".join(connected_rows) or f'<p class="section-empty">{html.escape(ui_text(locale, "还没有已连接的工作资料。你可以从下方添加。", "No work sources are connected yet. Add one below when you are ready."))}</p>'
    add_more_html = "".join(add_more_rows)
    unavailable_html = _source_row(
        key="additional-files",
        state="UNAVAILABLE",
        locale=locale,
        name=ui_text(locale, "其他文件", "Additional files"),
        summary=ui_text(locale, "更多文件来源即将支持。", "More file sources are coming later."),
        detail=ui_text(locale, "当前不扫描任意文件夹，也不显示无法执行的操作。", "SoloScale does not scan arbitrary folders or show actions that cannot run."),
    )

    body = f"""{notice_html}{error_html}{codex_job_marker}{preflight_notice}
<section class="privacy-note" aria-labelledby="privacy-heading">
  <div><strong id="privacy-heading">{html.escape(ui_text(locale, '只读取你明确选择的资料。本地优先，不扫描整台 Mac。', 'Only work you explicitly choose is read. Local first; no whole-Mac scanning.'))}</strong>
  <details><summary>{html.escape(ui_text(locale, '了解数据边界', 'Understand the data boundary'))}</summary>
    <p>{html.escape(ui_text(locale, 'SoloScale 只检查标准位置是否存在，以及你主动选择了什么。点击导入前不会读取对话正文；不会自动扫描其他项目或文件夹，也不会因为打开此页而上传资料或调用模型。', 'SoloScale checks only whether standard locations exist and what you explicitly selected. It does not read conversation bodies before import, scan other projects or folders automatically, upload your work, or call a model merely because this page opens.'))}</p>
  </details></div>
</section>
<section id="work-processing" class="source-section processing-section" aria-labelledby="processing-heading" hidden>
  <div class="section-heading"><h2 id="processing-heading">{html.escape(ui_text(locale, '处理中', 'Processing'))}</h2></div>
  <article class="source-row processing-row" aria-live="polite">
    {render_source_state("PROCESSING", locale)}
    <div class="source-copy"><h3 id="processing-source">{html.escape(ui_text(locale, '工作资料', 'Work source'))}</h3><strong id="processing-copy"></strong></div>
  </article>
</section>
<div class="source-sections">
  <section class="source-section" aria-labelledby="connected-heading">
    <div class="section-heading"><h2 id="connected-heading">{html.escape(ui_text(locale, '已连接', 'Connected'))}</h2><span>{len(connected_rows)}</span></div>
    <div class="source-list">{connected_html}</div>
  </section>
  <section class="source-section" aria-labelledby="add-more-heading">
    <div class="section-heading"><h2 id="add-more-heading">{html.escape(ui_text(locale, '添加更多', 'Add More'))}</h2></div>
    <div class="source-list">{add_more_html}</div>
  </section>
  <section class="source-section unavailable-section" aria-labelledby="unavailable-heading">
    <div class="section-heading"><h2 id="unavailable-heading">{html.escape(ui_text(locale, '暂不可用', 'Not Available Yet'))}</h2></div>
    <div class="source-list">{unavailable_html}</div>
  </section>
</div>
<footer class="work-actions">
  <a class="quiet-action" href="{ui_url(return_path, locale)}">{html.escape(ui_text(locale, '稍后再添加', 'Add more later'))}</a>
  <a class="primary" href="{ui_url(return_path, locale)}">{html.escape(ui_text(locale, '完成并继续', 'Continue'))}</a>
</footer>"""
    return render_app_shell(
        active="work",
        locale=locale,
        current_url="/work",
        title=f"SoloScale · {ui_text(locale, '你的工作资料', 'Your Work Context')}",
        eyebrow=ui_text(locale, "工作资料", "Work Context"),
        heading=ui_text(locale, "你的工作资料", "Your Work Context"),
        description=ui_text(
            locale,
            "选择 SoloScale 可以使用的资料。以后随时可以修改。",
            "Choose the work sources SoloScale can use. You can change them anytime.",
        ),
        body=body,
        compact_hero=True,
        extra_css="""
#main-content{display:grid;gap:22px}.privacy-note{padding:14px 16px;border:1px solid #cfe3da;border-radius:14px;background:linear-gradient(135deg,#f2faf6,#fff)}.privacy-note>div{display:grid;gap:5px}.privacy-note strong{color:var(--success)}.privacy-note details{color:var(--text-muted);font-size:13px}.privacy-note summary{width:max-content;color:var(--brand);font-weight:800;cursor:pointer}.privacy-note p{max-width:880px;margin:8px 0 0}.source-sections{display:grid;gap:26px}.source-section{display:grid;gap:10px}.source-section[hidden]{display:none!important}.section-heading{display:flex;align-items:center;gap:10px;padding-bottom:8px;border-bottom:1px solid var(--border)}.section-heading h2{margin:0;font-size:18px;letter-spacing:-.015em}.section-heading span{min-width:25px;height:25px;display:grid;place-items:center;border-radius:999px;background:var(--surface-subtle);color:var(--text-muted);font-size:12px;font-weight:800}.source-list{display:grid;gap:9px}.source-row{display:grid;grid-template-columns:116px minmax(210px,1fr) minmax(220px,auto);align-items:center;gap:18px;padding:16px 18px;border:1px solid var(--border);border-radius:16px;background:rgba(255,255,255,.84)}.source-copy{min-width:0}.source-copy h3{margin:0 0 2px;font-size:16px}.source-copy strong{display:block;font-size:14px}.source-copy p{margin:3px 0 0;color:var(--text-muted);font-size:12px}.source-actions{display:flex;align-items:center;justify-content:flex-end;gap:9px;flex-wrap:wrap}.source-actions form{display:flex;align-items:center;justify-content:flex-end;gap:9px;flex-wrap:wrap}.source-control{display:inline-flex;align-items:center;justify-content:center;min-height:36px;padding:7px 11px;border:1px solid var(--border);border-radius:10px;background:var(--surface);color:var(--brand);font:inherit;font-size:12px;font-weight:800;text-decoration:none;white-space:nowrap}.source-control:hover{border-color:var(--brand);background:var(--brand-soft)}button.source-control{color:var(--brand)}.consent{display:flex;max-width:255px;flex-direction:row;align-items:flex-start;gap:7px;color:var(--text-muted);font-size:11px;font-weight:500}.consent input{margin-top:1px}.source-help{max-width:250px;color:var(--text-muted);font-size:12px}.processing-section{padding:14px;border:1px solid #cfd6f6;border-radius:16px;background:var(--brand-soft)}.processing-section .section-heading{border-color:#cfd6f6}.processing-row{border:0;background:rgba(255,255,255,.7)}.source-row.is-processing .source-actions{opacity:.42;pointer-events:none}.section-empty{margin:0;padding:16px 18px;border:1px dashed var(--border);border-radius:14px;color:var(--text-muted)}.unavailable-section .source-row{background:var(--surface-subtle);box-shadow:none}.work-actions{display:flex;justify-content:flex-end;align-items:center;gap:12px;padding-top:3px}.work-actions .primary{min-width:150px}.quiet-action{padding:10px 12px;color:var(--text-muted);font-weight:750;text-decoration:none}
@media(max-width:940px){.source-row{grid-template-columns:105px 1fr}.source-actions{grid-column:2;justify-content:flex-start}.source-actions form{justify-content:flex-start}}
@media(max-width:620px){.source-row{grid-template-columns:1fr;gap:9px}.source-actions{grid-column:1;justify-content:flex-start}.source-actions form{align-items:flex-start;justify-content:flex-start;flex-direction:column}.consent{max-width:none}.work-actions{align-items:stretch;flex-direction:column-reverse}.work-actions a{width:100%;text-align:center}}
""",
        script="""
(() => {
  const section = document.getElementById("work-processing");
  const source = document.getElementById("processing-source");
  const copy = document.getElementById("processing-copy");
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.dataset.processingSource) return;
    source.textContent = form.dataset.processingSource;
    copy.textContent = form.dataset.processingCopy || "";
    section.hidden = false;
    form.closest(".source-row")?.classList.add("is-processing");
  }, true);
  const monitor = document.getElementById("codex-import-monitor");
  if (!monitor) return;
  const jobId = monitor.dataset.jobId;
  const summary = document.querySelector('[data-source="codex"] .source-copy strong');
  const isZh = document.documentElement.lang.toLowerCase().startsWith("zh");
  const poll = async () => {
    try {
      const response = await fetch(`/work/import-codex/jobs/${jobId}`, {
        cache: "no-store",
        headers: {"Accept": "application/json"},
      });
      if (!response.ok) throw new Error("job unavailable");
      const job = await response.json();
      if (summary) {
        summary.textContent = isZh
          ? `已处理 ${job.processed}/${job.total} · 正在处理 ${job.running} · 失败 ${job.failed} · 跳过 ${job.skipped}`
          : `Processed ${job.processed}/${job.total} · running ${job.running} · failed ${job.failed} · skipped ${job.skipped}`;
      }
      if (job.phase === "READY") {
        const added = Number(job.imported || 0) + Number(job.updated || 0);
        const notice = Number(job.failed || 0) > 0 ? "codex-partial" : "codex-added";
        window.location.replace(`/work?notice=${notice}&added=${added}&codex_job=${jobId}&lang=${encodeURIComponent(document.documentElement.lang)}`);
        return;
      }
      if (job.phase === "FAILED") {
        window.location.replace(`/work?error=codex-import-failed&lang=${encodeURIComponent(document.documentElement.lang)}`);
        return;
      }
    } catch (_) {
      // A transient local fetch failure should not cancel the persisted background job.
    }
    window.setTimeout(poll, 750);
  };
  window.setTimeout(poll, 250);
})();
""",
    )


def github_repositories_page(
    state: GitHubConnectionState,
    *,
    locale: UILocale = DEFAULT_UI_LOCALE,
    notice: str | None = None,
) -> str:
    """Render a searchable explicit selection over the GitHub App inventory."""

    selected = set(state.selected_repository_ids)
    repository_rows = "".join(
        f'''<label class="github-repository" data-repository-name="{html.escape(repository.full_name.casefold())}">
  <input type="checkbox" name="repository" value="{repository.repository_id}" {"checked" if repository.repository_id in selected else ""} />
  <span><strong>{html.escape(repository.full_name)}</strong><small>{html.escape(ui_text(locale, "私有" if repository.private else "公开", "Private" if repository.private else "Public"))} · {html.escape(repository.default_branch)}</small></span>
</label>'''
        for repository in state.repositories
    )
    if not repository_rows:
        repository_rows = f'<p class="section-empty">{html.escape(ui_text(locale, "GitHub App 当前没有可访问的仓库。请先在 GitHub 安装页面授权仓库，然后重新加载。", "The GitHub App cannot access any repositories yet. Grant repository access on GitHub, then reload this page."))}</p>'
    notice_html = (
        f'<div class="notice" role="status">{html.escape(notice)}</div>'
        if notice
        else ""
    )
    body = f"""{notice_html}
<section class="privacy-note"><strong>{html.escape(ui_text(locale, '只读连接', 'Read-only connection'))}</strong><p>{html.escape(ui_text(locale, 'SoloScale 只调用 GitHub GET API，不创建或修改仓库、Issue、PR、Actions 或代码。', 'SoloScale calls GitHub GET APIs only. It does not create or modify repositories, issues, pull requests, Actions, or code.'))}</p></section>
<section class="github-selection">
  <div class="section-heading"><div><span class="kicker">GitHub</span><h2>{html.escape(state.account_login)}</h2></div><span>{len(state.repositories)}</span></div>
  <label class="search-field">{html.escape(ui_text(locale, '搜索仓库', 'Search repositories'))}<input id="github-repository-search" type="search" autocomplete="off" placeholder="owner/repository" /></label>
  <form method="post" action="/work/github/select">
    <input type="hidden" name="ui_locale" value="{locale}" />
    <div id="github-repository-list" class="github-repository-list">{repository_rows}</div>
    <p class="source-help">{html.escape(ui_text(locale, '最多选择 20 个仓库。保存后再手动刷新 Evidence。', 'Select up to 20 repositories. Save the selection, then refresh Evidence manually.'))}</p>
    <div class="form-actions"><button class="primary" type="submit">{html.escape(ui_text(locale, '保存选择', 'Save selection'))}</button><a class="quiet-action" href="{ui_url('/work', locale)}">{html.escape(ui_text(locale, '返回工作资料', 'Back to Work Context'))}</a></div>
  </form>
</section>"""
    return render_app_shell(
        active="work",
        locale=locale,
        current_url="/work/github",
        title=f"SoloScale · {ui_text(locale, '选择 GitHub 仓库', 'Choose GitHub repositories')}",
        eyebrow="GitHub",
        heading=ui_text(locale, "选择允许 SoloScale 使用的仓库", "Choose repositories SoloScale may use"),
        description=ui_text(locale, "GitHub 授权范围和 SoloScale 内部选择必须同时允许。", "Both GitHub access and the SoloScale selection must allow a repository."),
        body=body,
        compact_hero=True,
        extra_css="""
.privacy-note{padding:14px 16px;border:1px solid #cfe3da;border-radius:14px;background:#f2faf6}.privacy-note p{margin:4px 0 0;color:var(--text-muted)}.github-selection{display:grid;gap:16px}.section-heading{display:flex;align-items:end;justify-content:space-between;border-bottom:1px solid var(--border);padding-bottom:10px}.section-heading h2{margin:3px 0 0}.search-field{display:grid;gap:6px;font-weight:800}.search-field input{max-width:520px}.github-repository-list{display:grid;gap:8px;max-height:480px;overflow:auto}.github-repository{display:flex;gap:12px;align-items:center;padding:12px 14px;border:1px solid var(--border);border-radius:13px;background:#fff}.github-repository span{display:grid;gap:2px}.github-repository small{color:var(--text-muted)}.form-actions{display:flex;gap:12px;align-items:center}
""",
        script="""
const search=document.getElementById('github-repository-search');
if(search) search.addEventListener('input',()=>{const value=search.value.trim().toLowerCase();document.querySelectorAll('.github-repository').forEach(row=>{row.hidden=value!==''&&!row.dataset.repositoryName.includes(value);});});
""",
    )
