from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from soloscale.casebook_models import (
    AttemptOutcome,
    DerivedCaseStatus,
    EvidenceKind,
    EvidenceReceipt,
    LearningCase,
    MasterySnapshot,
    PracticeAttempt,
    PracticeReceipt,
    PracticeStage,
)
from soloscale.interview_packet import render_interview_packet

_COPY_CHUNK_SIZE = 1024 * 1024
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_CASE_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class CasebookError(Exception):
    """Base error for local Casebook persistence failures."""


class DuplicateCaseError(CasebookError, FileExistsError):
    """Raised when a case identifier has already been published."""


class CaseNotFoundError(CasebookError, FileNotFoundError):
    """Raised when a requested case does not exist."""


class InvalidReceiptSourceError(CasebookError, ValueError):
    """Raised when an evidence or practice source cannot be archived safely."""


class SequentialPracticeError(CasebookError, ValueError):
    """Raised when a passing attempt skips an earlier practice gate."""


class UnsafeCasebookPathError(CasebookError, OSError):
    """Raised before a Casebook operation can traverse a managed symlink."""


class CorruptCasebookError(CasebookError, OSError):
    """Raised when a published case is missing a required source-of-truth record."""


IntegrityScope = Literal["evidence", "practice"]
IntegrityProblem = Literal[
    "unsafe-path",
    "missing",
    "not-regular-file",
    "unreadable",
    "byte-size-mismatch",
    "sha256-mismatch",
]


@dataclass(frozen=True, slots=True)
class IntegrityFailure:
    """A metadata-only description of one failed receipt verification."""

    scope: IntegrityScope
    receipt_id: str
    archived_path: str
    problem: IntegrityProblem


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    """Integrity summary that deliberately excludes archived file contents."""

    case_id: str
    checked_evidence: int
    checked_practice_receipts: int
    failures: tuple[IntegrityFailure, ...]

    @property
    def checked_files(self) -> int:
        return self.checked_evidence + self.checked_practice_receipts

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def evidence_gap(self) -> bool:
        """Whether evidence is absent or at least one evidence receipt is invalid."""

        return self.checked_evidence == 0 or any(
            failure.scope == "evidence" for failure in self.failures
        )


@dataclass(frozen=True, slots=True)
class AttemptRecordResult:
    """Committed attempt state plus the status of its derived packet refresh."""

    attempt: PracticeAttempt
    mastery: MasterySnapshot
    packet_refreshed: bool
    packet_error: str | None = None
    commit_warning: str | None = None


def derive_mastery(
    case: LearningCase, attempts: Sequence[PracticeAttempt]
) -> MasterySnapshot:
    """Derive current mastery exclusively from the latest outcome at each stage."""

    stage_results: dict[PracticeStage, AttemptOutcome | None] = {
        stage: None for stage in PracticeStage
    }
    for attempt in attempts:
        if attempt.case_id != case.id:
            raise ValueError(
                f"attempt for case {attempt.case_id!r} cannot derive mastery for {case.id!r}"
            )
        stage_results[attempt.stage] = attempt.outcome

    passed_stages = [
        stage for stage in PracticeStage if stage_results[stage] is AttemptOutcome.PASS
    ]
    next_stage = next(
        (stage for stage in PracticeStage if stage_results[stage] is not AttemptOutcome.PASS),
        None,
    )
    interview_ready = next_stage is None
    if interview_ready:
        status = DerivedCaseStatus.SELF_ASSESSED_INTERVIEW_READY
    elif all(outcome is None for outcome in stage_results.values()):
        status = DerivedCaseStatus.CAPTURED
    else:
        status = DerivedCaseStatus.IN_PRACTICE

    return MasterySnapshot(
        case_id=case.id,
        stage_results=stage_results,
        passed_stages=passed_stages,
        next_stage=next_stage,
        status=status,
        interview_ready=interview_ready,
    )


class CasebookStore:
    """Filesystem-backed Casebook rooted at the repository's ``.soloscale`` path."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.cases_root = self.root / "cases"
        self._tighten_existing_storage()

    def create_case(
        self,
        *,
        title: str,
        project: str,
        problem: str,
        expected_behavior: str,
        actual_behavior: str,
        root_cause: str,
        resolution: str,
        verification: Sequence[str],
        concepts: Sequence[str],
        evidence_sources: Sequence[tuple[EvidenceKind, Path]],
        case_id: str | None = None,
        repository: str | None = None,
        alternatives_considered: Sequence[str] = (),
        trade_offs: Sequence[str] = (),
        unknowns: Sequence[str] = (),
    ) -> LearningCase:
        """Validate, archive, and atomically publish a fully supplied learning case."""

        selected_case_id = case_id or _generated_case_id(title)
        _validate_case_id(selected_case_id)
        self._tighten_existing_storage()
        final_case_dir = self._case_dir(selected_case_id)
        if _lstat_managed_path(self.root, final_case_dir) is not None:
            raise DuplicateCaseError(f"case {selected_case_id!r} already exists")

        normalized_sources = [
            (EvidenceKind(kind), Path(source)) for kind, source in evidence_sources
        ]
        for _, source in normalized_sources:
            _validate_receipt_source(source)

        _ensure_private_directory(self.root, self.root, parents=True)
        _ensure_private_directory(self.root, self.cases_root)
        temp_case_dir = Path(
            tempfile.mkdtemp(prefix=f".{selected_case_id}-", dir=self.cases_root)
        )
        _validate_managed_path(self.root, temp_case_dir)
        temp_case_dir.chmod(_PRIVATE_DIRECTORY_MODE)
        published = False
        try:
            evidence_dir = temp_case_dir / "evidence"
            _ensure_private_directory(self.root, evidence_dir)
            evidence_receipts = [
                self._archive_evidence(
                    case_id=selected_case_id,
                    temp_evidence_dir=evidence_dir,
                    kind=kind,
                    source=source,
                )
                for kind, source in normalized_sources
            ]
            case = LearningCase(
                id=selected_case_id,
                title=title,
                project=project,
                problem=problem,
                expected_behavior=expected_behavior,
                actual_behavior=actual_behavior,
                root_cause=root_cause,
                resolution=resolution,
                verification=list(verification),
                concepts=list(concepts),
                evidence=evidence_receipts,
                repository=repository,
                alternatives_considered=list(alternatives_considered),
                trade_offs=list(trade_offs),
                unknowns=list(unknowns),
            )
            mastery = derive_mastery(case, [])
            _write_text_durably(
                self.root,
                temp_case_dir / "case.json",
                case.model_dump_json(indent=2) + "\n",
            )
            _write_text_durably(self.root, temp_case_dir / "attempts.jsonl", "")
            _write_text_durably(
                self.root,
                temp_case_dir / "interview-packet.md",
                render_interview_packet(case, mastery),
            )

            if _lstat_managed_path(self.root, final_case_dir) is not None:
                raise DuplicateCaseError(f"case {selected_case_id!r} already exists")
            temp_case_dir.rename(final_case_dir)
            published = True
            return case
        finally:
            if not published:
                shutil.rmtree(temp_case_dir, ignore_errors=True)

    def load_case(self, case_id: str) -> LearningCase:
        """Load and validate one persisted case."""

        case_file = self._required_case_dir(case_id) / "case.json"
        metadata = _lstat_managed_path(self.root, case_file)
        if metadata is None or not stat.S_ISREG(metadata.st_mode):
            raise CaseNotFoundError(f"case {case_id!r} has no case.json")
        return LearningCase.model_validate_json(
            _read_managed_text(self.root, case_file)
        )

    def list_cases(self) -> list[LearningCase]:
        """Return all published cases in stable identifier order."""

        self._tighten_existing_storage()
        if _lstat_managed_path(self.root, self.cases_root) is None:
            return []
        case_files = sorted(
            path
            for path in self.cases_root.glob("*/case.json")
            if not path.parent.name.startswith(".")
            and (metadata := _lstat_managed_path(self.root, path)) is not None
            and stat.S_ISREG(metadata.st_mode)
        )
        return [
            LearningCase.model_validate_json(_read_managed_text(self.root, path))
            for path in case_files
        ]

    def read_attempts(self, case_id: str) -> list[PracticeAttempt]:
        """Replay the append-only practice attempt log for one case."""

        attempts_file = self._required_case_dir(case_id) / "attempts.jsonl"
        metadata = _lstat_managed_path(self.root, attempts_file)
        if metadata is None:
            raise CorruptCasebookError(
                f"case {case_id!r} is missing its required attempts.jsonl"
            )
        if not stat.S_ISREG(metadata.st_mode):
            raise UnsafeCasebookPathError(
                f"Casebook attempt log is not a regular file: {attempts_file}"
            )
        attempts: list[PracticeAttempt] = []
        for line in _read_managed_text(self.root, attempts_file).splitlines():
            if stripped := line.strip():
                attempts.append(PracticeAttempt.model_validate_json(stripped))
        return attempts

    def record_attempt(
        self,
        case_id: str,
        stage: PracticeStage,
        outcome: AttemptOutcome,
        receipt_path: Path | None = None,
        note: str | None = None,
    ) -> AttemptRecordResult:
        """Append one practice result and refresh the derived interview packet."""

        case = self.load_case(case_id)
        normalized_stage = PracticeStage(stage)
        normalized_outcome = AttemptOutcome(outcome)
        normalized_note = note.strip() if note is not None else None
        attempts = self.read_attempts(case_id)

        if normalized_outcome is AttemptOutcome.PASS:
            if receipt_path is None:
                raise ValueError("a passing attempt requires a nonempty practice receipt")
            latest = derive_mastery(case, attempts)
            stages = list(PracticeStage)
            stage_index = stages.index(normalized_stage)
            missing_prerequisites = [
                prerequisite
                for prerequisite in stages[:stage_index]
                if latest.stage_results[prerequisite] is not AttemptOutcome.PASS
            ]
            if missing_prerequisites:
                names = ", ".join(stage.value for stage in missing_prerequisites)
                raise SequentialPracticeError(
                    f"cannot pass {normalized_stage.value!r} before passing: {names}"
                )
        elif not normalized_note:
            raise ValueError("a needs-work attempt requires a nonblank note")

        normalized_receipt_path = Path(receipt_path) if receipt_path is not None else None
        if normalized_receipt_path is not None:
            _validate_receipt_source(normalized_receipt_path)

        practice_receipt: PracticeReceipt | None = None
        newly_archived: Path | None = None
        if normalized_receipt_path is not None:
            practice_receipt, newly_archived = self._archive_practice_receipt(
                case_id,
                normalized_receipt_path,
            )

        try:
            attempt = PracticeAttempt(
                case_id=case_id,
                stage=normalized_stage,
                outcome=normalized_outcome,
                note=normalized_note,
                receipt=practice_receipt,
            )
            prospective_attempts = [*attempts, attempt]
            mastery = derive_mastery(case, prospective_attempts)
            packet = render_interview_packet(case, mastery)
            commit_warning = self._append_attempt(case_id, attempt)
        except Exception:
            if newly_archived is not None:
                newly_archived.unlink(missing_ok=True)
            raise

        try:
            _atomic_write_text(
                self.root,
                self._case_dir(case_id) / "interview-packet.md",
                packet,
            )
        except Exception as exc:
            return AttemptRecordResult(
                attempt=attempt,
                mastery=mastery,
                packet_refreshed=False,
                packet_error=_sanitized_packet_error(exc),
                commit_warning=commit_warning,
            )
        return AttemptRecordResult(
            attempt=attempt,
            mastery=mastery,
            packet_refreshed=True,
            commit_warning=commit_warning,
        )

    def mastery(self, case_id: str) -> MasterySnapshot:
        """Return current derived mastery for a persisted case."""

        case = self.load_case(case_id)
        return derive_mastery(case, self.read_attempts(case_id))

    def verify_integrity(self, case_id: str) -> IntegrityReport:
        """Compare receipt hashes and sizes without reading content into output."""

        case = self.load_case(case_id)
        attempts = self.read_attempts(case_id)
        failures: list[IntegrityFailure] = []

        for evidence_receipt in case.evidence:
            failure = self._verify_receipt(
                scope="evidence",
                receipt_id=evidence_receipt.id,
                archived_path=evidence_receipt.archived_path,
                expected_sha256=evidence_receipt.sha256,
                expected_byte_size=evidence_receipt.byte_size,
            )
            if failure is not None:
                failures.append(failure)

        practice_receipts = [
            attempt.receipt for attempt in attempts if attempt.receipt is not None
        ]
        for practice_receipt in practice_receipts:
            failure = self._verify_receipt(
                scope="practice",
                receipt_id=practice_receipt.id,
                archived_path=practice_receipt.archived_path,
                expected_sha256=practice_receipt.sha256,
                expected_byte_size=practice_receipt.byte_size,
            )
            if failure is not None:
                failures.append(failure)

        return IntegrityReport(
            case_id=case_id,
            checked_evidence=len(case.evidence),
            checked_practice_receipts=len(practice_receipts),
            failures=tuple(failures),
        )

    def _case_dir(self, case_id: str) -> Path:
        return self.cases_root / case_id

    def _required_case_dir(self, case_id: str) -> Path:
        self._tighten_existing_storage()
        _validate_case_id(case_id)
        case_dir = self._case_dir(case_id)
        metadata = _lstat_managed_path(self.root, case_dir)
        if metadata is None or not stat.S_ISDIR(metadata.st_mode):
            raise CaseNotFoundError(f"case {case_id!r} does not exist")
        return case_dir

    def _archive_evidence(
        self,
        *,
        case_id: str,
        temp_evidence_dir: Path,
        kind: EvidenceKind,
        source: Path,
    ) -> EvidenceReceipt:
        digest, byte_size, filename = _archive_file(
            self.root,
            source,
            temp_evidence_dir,
        )
        archived_path = Path("cases") / case_id / "evidence" / filename
        return EvidenceReceipt(
            id=f"evidence-{uuid4().hex[:16]}",
            kind=kind,
            source_path=str(source),
            archived_path=archived_path.as_posix(),
            sha256=digest,
            byte_size=byte_size,
        )

    def _archive_practice_receipt(
        self, case_id: str, source: Path
    ) -> tuple[PracticeReceipt, Path | None]:
        receipt_dir = self._required_case_dir(case_id) / "practice-receipts"
        _ensure_private_directory(self.root, receipt_dir)
        digest, byte_size, filename, newly_archived = _archive_file_reusing_identical(
            self.root,
            source,
            receipt_dir,
        )
        archived_file = receipt_dir / filename
        archived_path = Path("cases") / case_id / "practice-receipts" / filename
        receipt = PracticeReceipt(
            id=f"practice-{uuid4().hex[:16]}",
            source_path=str(source),
            archived_path=archived_path.as_posix(),
            sha256=digest,
            byte_size=byte_size,
        )
        return receipt, archived_file if newly_archived else None

    def _append_attempt(self, case_id: str, attempt: PracticeAttempt) -> str | None:
        attempts_file = self._required_case_dir(case_id) / "attempts.jsonl"
        _validate_managed_path(self.root, attempts_file)
        encoded = (attempt.model_dump_json() + "\n").encode("utf-8")
        descriptor = os.open(
            attempts_file,
            os.O_APPEND | os.O_WRONLY | _no_follow_flag(),
            _PRIVATE_FILE_MODE,
        )
        try:
            os.fchmod(descriptor, _PRIVATE_FILE_MODE)
            original_length = os.fstat(descriptor).st_size
            total_written = 0
            try:
                while total_written < len(encoded):
                    written = os.write(descriptor, encoded[total_written:])
                    if written <= 0:
                        raise OSError("practice attempt append made no progress")
                    total_written += written
                os.fsync(descriptor)
            except BaseException:
                if total_written:
                    os.ftruncate(descriptor, original_length)
                    os.fsync(descriptor)
                raise
        except BaseException:
            try:
                os.close(descriptor)
            except Exception:
                pass
            raise
        try:
            os.close(descriptor)
        except Exception as exc:
            return _sanitized_commit_warning(exc)
        return None

    def _tighten_existing_storage(self) -> None:
        """Restrict existing Casebook storage without changing parent directories."""

        if _tighten_existing_directory(self.root, self.root):
            _tighten_existing_tree(self.root, self.cases_root)

    def _verify_receipt(
        self,
        *,
        scope: IntegrityScope,
        receipt_id: str,
        archived_path: str,
        expected_sha256: str,
        expected_byte_size: int,
    ) -> IntegrityFailure | None:
        candidate = Path(archived_path)
        if candidate.is_absolute():
            return IntegrityFailure(scope, receipt_id, archived_path, "unsafe-path")
        try:
            resolved = self.root / candidate
            metadata = _lstat_managed_path(self.root, resolved)
        except (OSError, RuntimeError, UnsafeCasebookPathError):
            return IntegrityFailure(scope, receipt_id, archived_path, "unsafe-path")
        if metadata is None:
            return IntegrityFailure(scope, receipt_id, archived_path, "missing")
        if not stat.S_ISREG(metadata.st_mode):
            return IntegrityFailure(scope, receipt_id, archived_path, "not-regular-file")
        try:
            actual_sha256, actual_byte_size = _hash_managed_file(self.root, resolved)
        except OSError:
            return IntegrityFailure(scope, receipt_id, archived_path, "unreadable")
        if actual_byte_size != expected_byte_size:
            return IntegrityFailure(scope, receipt_id, archived_path, "byte-size-mismatch")
        if actual_sha256 != expected_sha256:
            return IntegrityFailure(scope, receipt_id, archived_path, "sha256-mismatch")
        return None


def _validate_case_id(case_id: str) -> None:
    if not 3 <= len(case_id) <= 80 or not _CASE_ID_PATTERN.fullmatch(case_id):
        raise ValueError(
            "case_id must be 3-80 lowercase letters, digits, or hyphens and cannot "
            "start or end with a hyphen"
        )


def _generated_case_id(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    slug = slug[:60].rstrip("-") or "case"
    return f"{slug}-{uuid4().hex[:10]}"


def _validate_receipt_source(source: Path) -> None:
    try:
        metadata = source.stat()
    except OSError as exc:
        raise InvalidReceiptSourceError(f"receipt source is not readable: {source}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise InvalidReceiptSourceError(f"receipt source is not a regular file: {source}")
    if metadata.st_size <= 0:
        raise InvalidReceiptSourceError(f"receipt source is empty: {source}")
    try:
        with source.open("rb") as handle:
            if not handle.read(1):
                raise InvalidReceiptSourceError(f"receipt source is empty: {source}")
    except OSError as exc:
        raise InvalidReceiptSourceError(f"receipt source is not readable: {source}") from exc


def _sanitized_packet_error(error: Exception) -> str:
    return f"{type(error).__name__}: interview packet refresh failed after attempt commit"


def _sanitized_commit_warning(error: Exception) -> str:
    return f"{type(error).__name__}: attempt log close failed after durable commit"


def _no_follow_flag() -> int:
    return getattr(os, "O_NOFOLLOW", 0)


def _validate_managed_path(root: Path, path: Path) -> Path:
    root_path = Path(os.path.abspath(root))
    managed_path = Path(os.path.abspath(path))
    if managed_path != root_path and not managed_path.is_relative_to(root_path):
        raise UnsafeCasebookPathError(
            f"Casebook path must stay within its configured root: {path}"
        )

    current = root_path
    components = [root_path]
    if managed_path != root_path:
        for part in managed_path.relative_to(root_path).parts:
            current /= part
            components.append(current)
    for component in components:
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(metadata.st_mode):
            raise UnsafeCasebookPathError(
                f"Casebook managed paths must not contain symlinks: {component}"
            )

    resolved_root = root_path.resolve(strict=False)
    resolved_path = managed_path.resolve(strict=False)
    if resolved_path != resolved_root and not resolved_path.is_relative_to(resolved_root):
        raise UnsafeCasebookPathError(
            f"Casebook path resolves outside its configured root: {path}"
        )
    return managed_path


def _lstat_managed_path(root: Path, path: Path) -> os.stat_result | None:
    managed_path = _validate_managed_path(root, path)
    try:
        return managed_path.lstat()
    except FileNotFoundError:
        return None


def _ensure_private_directory(
    root: Path,
    path: Path,
    *,
    parents: bool = False,
) -> None:
    managed_path = _validate_managed_path(root, path)
    managed_path.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=parents, exist_ok=True)
    managed_path = _validate_managed_path(root, managed_path)
    metadata = managed_path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise NotADirectoryError(f"Casebook path is not a directory: {managed_path}")
    managed_path.chmod(_PRIVATE_DIRECTORY_MODE)


def _tighten_existing_directory(root: Path, path: Path) -> bool:
    managed_path = _validate_managed_path(root, path)
    metadata = _lstat_managed_path(root, managed_path)
    if metadata is None:
        return False
    if not stat.S_ISDIR(metadata.st_mode):
        return False
    managed_path.chmod(_PRIVATE_DIRECTORY_MODE)
    return True


def _tighten_existing_tree(root: Path, path: Path) -> None:
    managed_path = _validate_managed_path(root, path)
    if not _tighten_existing_directory(root, managed_path):
        return
    with os.scandir(managed_path) as entries:
        for entry in entries:
            entry_path = Path(entry.path)
            metadata = _lstat_managed_path(root, entry_path)
            if metadata is None:
                continue
            if entry.is_dir(follow_symlinks=False):
                _tighten_existing_tree(root, entry_path)
            elif stat.S_ISREG(metadata.st_mode):
                entry_path.chmod(_PRIVATE_FILE_MODE)


def _archive_file(
    root: Path,
    source: Path,
    destination_dir: Path,
) -> tuple[str, int, str]:
    staging = destination_dir / f".archive-{uuid4().hex}"
    try:
        digest, byte_size = _copy_and_hash(root, source, staging)
        filename = _available_hash_filename(
            root,
            destination_dir,
            digest,
        )
        _validate_managed_path(root, destination_dir / filename)
        staging.rename(destination_dir / filename)
        return digest, byte_size, filename
    finally:
        staging.unlink(missing_ok=True)


def _archive_file_reusing_identical(
    root: Path,
    source: Path,
    destination_dir: Path,
) -> tuple[str, int, str, bool]:
    staging = destination_dir / f".archive-{uuid4().hex}"
    try:
        digest, byte_size = _copy_and_hash(root, source, staging)
        base_filename = digest
        existing = destination_dir / base_filename
        existing_metadata = _lstat_managed_path(root, existing)
        if existing_metadata is not None and stat.S_ISREG(existing_metadata.st_mode):
            existing_digest, existing_size = _hash_managed_file(root, existing)
            if existing_digest == digest and existing_size == byte_size:
                existing.chmod(_PRIVATE_FILE_MODE)
                return digest, byte_size, base_filename, False
        filename = _available_hash_filename(
            root,
            destination_dir,
            digest,
        )
        _validate_managed_path(root, destination_dir / filename)
        staging.rename(destination_dir / filename)
        return digest, byte_size, filename, True
    finally:
        staging.unlink(missing_ok=True)


def _available_hash_filename(root: Path, directory: Path, digest: str) -> str:
    candidate = digest
    counter = 2
    while _lstat_managed_path(root, directory / candidate) is not None:
        candidate = f"{digest}-{counter}"
        counter += 1
    return candidate


def _copy_and_hash(root: Path, source: Path, destination: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    byte_size = 0
    descriptor: int | None = None
    try:
        managed_destination = _validate_managed_path(root, destination)
        descriptor = os.open(
            managed_destination,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | _no_follow_flag(),
            _PRIVATE_FILE_MODE,
        )
        os.fchmod(descriptor, _PRIVATE_FILE_MODE)
        with source.open("rb") as source_handle, os.fdopen(
            descriptor,
            "wb",
            closefd=False,
        ) as destination_handle:
            while chunk := source_handle.read(_COPY_CHUNK_SIZE):
                hasher.update(chunk)
                byte_size += len(chunk)
                destination_handle.write(chunk)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
    except OSError as exc:
        raise InvalidReceiptSourceError(f"could not archive receipt source: {source}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if byte_size <= 0:
        raise InvalidReceiptSourceError(f"receipt source is empty: {source}")
    return hasher.hexdigest(), byte_size


def _hash_managed_file(root: Path, path: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    byte_size = 0
    managed_path = _validate_managed_path(root, path)
    descriptor = os.open(managed_path, os.O_RDONLY | _no_follow_flag())
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise UnsafeCasebookPathError(
                f"Casebook archive is not a regular file: {managed_path}"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            while chunk := handle.read(_COPY_CHUNK_SIZE):
                hasher.update(chunk)
                byte_size += len(chunk)
    finally:
        os.close(descriptor)
    return hasher.hexdigest(), byte_size


def _read_managed_text(root: Path, path: Path) -> str:
    managed_path = _validate_managed_path(root, path)
    descriptor = os.open(managed_path, os.O_RDONLY | _no_follow_flag())
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise UnsafeCasebookPathError(
                f"Casebook record is not a regular file: {managed_path}"
            )
        with os.fdopen(
            descriptor,
            "r",
            encoding="utf-8",
            closefd=False,
        ) as handle:
            return handle.read()
    finally:
        os.close(descriptor)


def _write_text_durably(root: Path, path: Path, value: str) -> None:
    managed_path = _validate_managed_path(root, path)
    descriptor = os.open(
        managed_path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | _no_follow_flag(),
        _PRIVATE_FILE_MODE,
    )
    try:
        os.fchmod(descriptor, _PRIVATE_FILE_MODE)
        with os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
            closefd=False,
        ) as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _atomic_write_text(root: Path, path: Path, value: str) -> None:
    _validate_managed_path(root, path)
    staging = path.with_name(f".{path.name}-{uuid4().hex}.tmp")
    try:
        _write_text_durably(root, staging, value)
        _validate_managed_path(root, path)
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)
