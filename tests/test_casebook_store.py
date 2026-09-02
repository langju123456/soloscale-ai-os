from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import TypedDict

import pytest

import soloscale.casebook_store as casebook_store_module
from soloscale.casebook_models import (
    AttemptOutcome,
    DerivedCaseStatus,
    EvidenceKind,
    LearningCase,
    MasterySnapshot,
    PracticeStage,
)
from soloscale.casebook_store import (
    AttemptRecordResult,
    CasebookStore,
    CorruptCasebookError,
    DuplicateCaseError,
    InvalidReceiptSourceError,
    SequentialPracticeError,
    UnsafeCasebookPathError,
    derive_mastery,
)
from soloscale.interview_packet import render_interview_packet


class _CaseFacts(TypedDict):
    case_id: str
    title: str
    project: str
    problem: str
    expected_behavior: str
    actual_behavior: str
    root_cause: str
    resolution: str
    verification: list[str]
    concepts: list[str]
    repository: str
    alternatives_considered: list[str]
    trade_offs: list[str]
    unknowns: list[str]


def _case_facts(case_id: str = "cache-invalidation-case") -> _CaseFacts:
    return {
        "case_id": case_id,
        "title": "Cache invalidation failure",
        "project": "SoloScale AI OS",
        "problem": "A stale response survived a completed mutation.",
        "expected_behavior": "The mutation exposes the current response.",
        "actual_behavior": "The stale response remained visible.",
        "root_cause": "The mutation omitted cache-tag invalidation.",
        "resolution": "Invalidate the tag after the transaction commits.",
        "verification": ["The focused regression test passes."],
        "concepts": ["cache invalidation", "transaction boundaries"],
        "repository": "example/soloscale",
        "alternatives_considered": ["Disable the cache."],
        "trade_offs": ["Fresh reads add invalidation work."],
        "unknowns": ["Cross-region propagation remains unmeasured."],
    }


def _create_case(
    tmp_path: Path,
    *,
    case_id: str = "cache-invalidation-case",
) -> tuple[CasebookStore, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "raw session (private).txt"
    source.write_bytes(b"private evidence bytes\x00\xff")
    store = CasebookStore(tmp_path / ".soloscale")
    store.create_case(
        **_case_facts(case_id),
        evidence_sources=[(EvidenceKind.TEST, source)],
    )
    return store, source


def _receipt(tmp_path: Path, name: str, content: str = "practice receipt") -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _outside_snapshot(directory: Path) -> tuple[int, tuple[tuple[str, bytes, int], ...]]:
    files = tuple(
        (path.name, path.read_bytes(), _mode(path))
        for path in sorted(directory.iterdir())
        if path.is_file()
    )
    return _mode(directory), files


def _outside_target(tmp_path: Path, name: str) -> tuple[Path, object]:
    directory = tmp_path / name
    directory.mkdir()
    sentinel = directory / "do-not-touch.txt"
    sentinel.write_bytes(b"outside bytes must remain unchanged")
    sentinel.chmod(0o640)
    directory.chmod(0o751)
    return directory, _outside_snapshot(directory)


def test_create_case_archives_bytes_and_publishes_complete_case_atomically(
    tmp_path: Path,
) -> None:
    store, source = _create_case(tmp_path)

    case = store.load_case("cache-invalidation-case")
    case_dir = store.root / "cases" / case.id
    assert store.list_cases() == [case]
    assert case.repository == "example/soloscale"
    assert len(case.evidence) == 1
    receipt = case.evidence[0]
    archived = store.root / receipt.archived_path
    assert archived.parent == case_dir / "evidence"
    assert archived.name == receipt.sha256
    assert source.name not in archived.name
    assert archived.read_bytes() == source.read_bytes()
    assert receipt.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert receipt.byte_size == len(source.read_bytes())
    assert receipt.source_path == str(source)
    assert (case_dir / "attempts.jsonl").read_bytes() == b""
    assert (case_dir / "interview-packet.md").is_file()
    assert not list((store.root / "cases").glob(".cache-invalidation-case-*"))


def test_archive_names_are_opaque_and_duplicate_evidence_keeps_unique_locators(
    tmp_path: Path,
) -> None:
    first_source = tmp_path / "customer-acme-layoff-plan.md"
    second_source = tmp_path / "another-sensitive-name.txt"
    first_source.write_bytes(b"same private evidence")
    second_source.write_bytes(first_source.read_bytes())
    store = CasebookStore(tmp_path / ".soloscale")

    case = store.create_case(
        **_case_facts(),
        evidence_sources=[
            (EvidenceKind.DOCUMENT, first_source),
            (EvidenceKind.TEST, second_source),
        ],
    )

    digest = hashlib.sha256(first_source.read_bytes()).hexdigest()
    archive_names = [Path(receipt.archived_path).name for receipt in case.evidence]
    assert archive_names == [digest, f"{digest}-2"]
    assert all(first_source.name not in name for name in archive_names)
    assert all(second_source.name not in name for name in archive_names)


def test_identical_practice_receipts_reuse_opaque_archive(tmp_path: Path) -> None:
    store, _source = _create_case(tmp_path)
    first_source = _receipt(
        tmp_path,
        "private-customer-practice.md",
        "identical practice bytes",
    )
    second_source = _receipt(
        tmp_path,
        "different-sensitive-name.md",
        "identical practice bytes",
    )

    first = store.record_attempt(
        "cache-invalidation-case",
        PracticeStage.EXPLAIN,
        AttemptOutcome.PASS,
        first_source,
    ).attempt
    second = store.record_attempt(
        "cache-invalidation-case",
        PracticeStage.EXPLAIN,
        AttemptOutcome.NEEDS_WORK,
        second_source,
        note="Repeat practice still needs work.",
    ).attempt

    assert first.receipt is not None
    assert second.receipt is not None
    assert first.receipt.archived_path == second.receipt.archived_path
    assert Path(first.receipt.archived_path).name == first.receipt.sha256
    receipt_dir = store.cases_root / "cache-invalidation-case" / "practice-receipts"
    assert [path.name for path in receipt_dir.iterdir()] == [first.receipt.sha256]


def test_existing_manifest_with_legacy_archive_locator_remains_readable(
    tmp_path: Path,
) -> None:
    store, _source = _create_case(tmp_path)
    case = store.load_case("cache-invalidation-case")
    receipt = case.evidence[0]
    archive = store.root / receipt.archived_path
    legacy_name = f"{receipt.sha256}-raw-session-private.txt"
    legacy_archive = archive.with_name(legacy_name)
    archive.rename(legacy_archive)
    legacy_receipt = receipt.model_copy(
        update={
            "archived_path": (
                Path("cases") / case.id / "evidence" / legacy_name
            ).as_posix()
        }
    )
    legacy_case = case.model_copy(update={"evidence": [legacy_receipt]})
    case_file = store.cases_root / case.id / "case.json"
    case_file.write_text(legacy_case.model_dump_json(indent=2) + "\n", encoding="utf-8")
    case_file.chmod(0o600)

    reopened = CasebookStore(store.root)

    assert reopened.load_case(case.id) == legacy_case
    assert reopened.verify_integrity(case.id).ok is True


def test_casebook_storage_is_private_under_a_permissive_umask(tmp_path: Path) -> None:
    source = tmp_path / "private-evidence.txt"
    source.write_text("private evidence", encoding="utf-8")
    store = CasebookStore(tmp_path / ".soloscale")

    previous_umask = os.umask(0)
    try:
        case = store.create_case(
            **_case_facts(),
            evidence_sources=[(EvidenceKind.TEST, source)],
        )
        result = store.record_attempt(
            case.id,
            PracticeStage.EXPLAIN,
            AttemptOutcome.PASS,
            _receipt(tmp_path, "private-practice.md"),
        )
    finally:
        os.umask(previous_umask)

    case_dir = store.cases_root / case.id
    assert result.attempt.receipt is not None
    private_directories = [
        store.root,
        store.cases_root,
        case_dir,
        case_dir / "evidence",
        case_dir / "practice-receipts",
    ]
    private_files = [
        case_dir / "case.json",
        case_dir / "attempts.jsonl",
        case_dir / "interview-packet.md",
        store.root / case.evidence[0].archived_path,
        store.root / result.attempt.receipt.archived_path,
    ]
    assert {_mode(path) for path in private_directories} == {0o700}
    assert {_mode(path) for path in private_files} == {0o600}


def test_existing_casebook_is_tightened_without_changing_its_parent(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "existing-workspace"
    root = parent / ".soloscale"
    case_dir = root / "cases" / "existing-case"
    evidence_dir = case_dir / "evidence"
    practice_dir = case_dir / "practice-receipts"
    evidence_dir.mkdir(parents=True)
    practice_dir.mkdir()
    files = [
        case_dir / "case.json",
        case_dir / "attempts.jsonl",
        case_dir / "interview-packet.md",
        evidence_dir / "archived.txt",
        practice_dir / "receipt.md",
    ]
    for path in files:
        path.write_text("private", encoding="utf-8")
        path.chmod(0o666)
    for path in [root, root / "cases", case_dir, evidence_dir, practice_dir]:
        path.chmod(0o777)
    parent.chmod(0o751)

    CasebookStore(root)

    assert _mode(parent) == 0o751
    assert {_mode(path) for path in [root, root / "cases", case_dir]} == {0o700}
    assert {_mode(path) for path in [evidence_dir, practice_dir]} == {0o700}
    assert {_mode(path) for path in files} == {0o600}


def test_symlinked_casebook_root_fails_without_touching_target(tmp_path: Path) -> None:
    outside, before = _outside_target(tmp_path, "outside-root")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = workspace / ".soloscale"
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafeCasebookPathError, match="must not contain symlinks"):
        CasebookStore(root)

    assert _outside_snapshot(outside) == before


def test_symlinked_cases_directory_fails_without_touching_target(tmp_path: Path) -> None:
    outside, before = _outside_target(tmp_path, "outside-cases")
    root = tmp_path / ".soloscale"
    root.mkdir()
    (root / "cases").symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafeCasebookPathError, match="must not contain symlinks"):
        CasebookStore(root)

    assert _outside_snapshot(outside) == before


def test_symlinked_case_directory_fails_without_touching_target(tmp_path: Path) -> None:
    outside, before = _outside_target(tmp_path, "outside-case")
    root = tmp_path / ".soloscale"
    cases_root = root / "cases"
    cases_root.mkdir(parents=True)
    (cases_root / "cache-invalidation-case").symlink_to(
        outside,
        target_is_directory=True,
    )

    with pytest.raises(UnsafeCasebookPathError, match="must not contain symlinks"):
        CasebookStore(root)

    assert _outside_snapshot(outside) == before


def test_symlinked_evidence_directory_fails_without_touching_target(tmp_path: Path) -> None:
    outside, before = _outside_target(tmp_path, "outside-evidence")
    root = tmp_path / ".soloscale"
    case_dir = root / "cases" / "cache-invalidation-case"
    case_dir.mkdir(parents=True)
    (case_dir / "evidence").symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafeCasebookPathError, match="must not contain symlinks"):
        CasebookStore(root)

    assert _outside_snapshot(outside) == before


def test_symlinked_practice_receipts_fails_without_touching_target(
    tmp_path: Path,
) -> None:
    store, _source = _create_case(tmp_path)
    outside, before = _outside_target(tmp_path, "outside-practice")
    receipt_dir = store.cases_root / "cache-invalidation-case" / "practice-receipts"
    receipt_dir.symlink_to(outside, target_is_directory=True)
    attempts_before = (
        store.cases_root / "cache-invalidation-case" / "attempts.jsonl"
    ).read_bytes()

    with pytest.raises(UnsafeCasebookPathError, match="must not contain symlinks"):
        store.record_attempt(
            "cache-invalidation-case",
            PracticeStage.EXPLAIN,
            AttemptOutcome.PASS,
            _receipt(tmp_path, "must-not-be-archived.md"),
        )

    assert _outside_snapshot(outside) == before
    assert (
        store.cases_root / "cache-invalidation-case" / "attempts.jsonl"
    ).read_bytes() == attempts_before


@pytest.mark.parametrize("invalid_kind", ["missing", "directory", "empty"])
def test_create_case_rejects_invalid_sources_without_a_partial_final_case(
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    valid = tmp_path / "valid.txt"
    valid.write_text("valid", encoding="utf-8")
    invalid = tmp_path / "invalid"
    if invalid_kind == "directory":
        invalid.mkdir()
    elif invalid_kind == "empty":
        invalid.touch()

    store = CasebookStore(tmp_path / ".soloscale")
    with pytest.raises(InvalidReceiptSourceError):
        store.create_case(
            **_case_facts(),
            evidence_sources=[
                (EvidenceKind.TEST, valid),
                (EvidenceKind.CHAT, invalid),
            ],
        )

    assert not (store.root / "cases" / "cache-invalidation-case").exists()


def test_failure_during_packet_render_leaves_no_final_or_temporary_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "evidence.txt"
    source.write_text("evidence", encoding="utf-8")
    store = CasebookStore(tmp_path / ".soloscale")

    def fail_render(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("render failed")

    monkeypatch.setattr(casebook_store_module, "render_interview_packet", fail_render)
    with pytest.raises(RuntimeError, match="render failed"):
        store.create_case(
            **_case_facts(),
            evidence_sources=[(EvidenceKind.TEST, source)],
        )

    cases_root = store.root / "cases"
    assert not (cases_root / "cache-invalidation-case").exists()
    assert list(cases_root.iterdir()) == []


def test_duplicate_case_id_fails_without_modifying_published_case(tmp_path: Path) -> None:
    store, source = _create_case(tmp_path)
    case_file = store.root / "cases" / "cache-invalidation-case" / "case.json"
    original = case_file.read_bytes()

    with pytest.raises(DuplicateCaseError):
        store.create_case(
            **_case_facts(),
            evidence_sources=[(EvidenceKind.TEST, source)],
        )

    assert case_file.read_bytes() == original
    assert len(store.list_cases()) == 1


def test_passing_gates_are_sequential_and_attempt_log_is_append_only(tmp_path: Path) -> None:
    store, _source = _create_case(tmp_path)
    explain_receipt = _receipt(tmp_path, "explain.md")
    trace_receipt = _receipt(tmp_path, "trace.md")

    with pytest.raises(SequentialPracticeError):
        store.record_attempt(
            "cache-invalidation-case",
            PracticeStage.TRACE,
            AttemptOutcome.PASS,
            trace_receipt,
        )

    first_result = store.record_attempt(
        "cache-invalidation-case",
        PracticeStage.EXPLAIN,
        AttemptOutcome.PASS,
        explain_receipt,
    )
    first = first_result.attempt
    first_mastery = first_result.mastery
    attempts_file = store.root / "cases" / "cache-invalidation-case" / "attempts.jsonl"
    first_line = attempts_file.read_text(encoding="utf-8")
    second_result = store.record_attempt(
        "cache-invalidation-case",
        PracticeStage.TRACE,
        AttemptOutcome.NEEDS_WORK,
        note="The data-flow boundary was unclear.",
    )
    second = second_result.attempt
    second_mastery = second_result.mastery

    lines = attempts_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0] + "\n" == first_line
    assert store.read_attempts("cache-invalidation-case") == [first, second]
    assert first_mastery.next_stage is PracticeStage.TRACE
    assert second_mastery.stage_results[PracticeStage.TRACE] is AttemptOutcome.NEEDS_WORK


def test_missing_attempt_log_fails_closed_without_reset_or_recreation(
    tmp_path: Path,
) -> None:
    store, _source = _create_case(tmp_path)
    for stage in PracticeStage:
        store.record_attempt(
            "cache-invalidation-case",
            stage,
            AttemptOutcome.PASS,
            _receipt(tmp_path, f"{stage.value}-mastery.md", f"passed {stage.value}"),
        )
    assert store.mastery("cache-invalidation-case").interview_ready is True
    case_dir = store.cases_root / "cache-invalidation-case"
    attempts_file = case_dir / "attempts.jsonl"
    receipt_dir = case_dir / "practice-receipts"
    archived_receipts = {path.name for path in receipt_dir.iterdir()}
    attempts_file.unlink()

    with pytest.raises(CorruptCasebookError, match="required attempts.jsonl"):
        store.read_attempts("cache-invalidation-case")
    with pytest.raises(CorruptCasebookError, match="required attempts.jsonl"):
        store.mastery("cache-invalidation-case")
    with pytest.raises(CorruptCasebookError, match="required attempts.jsonl"):
        store.verify_integrity("cache-invalidation-case")
    with pytest.raises(CorruptCasebookError, match="required attempts.jsonl"):
        store.record_attempt(
            "cache-invalidation-case",
            PracticeStage.EXPLAIN,
            AttemptOutcome.NEEDS_WORK,
            note="Missing history must not look like a fresh case.",
        )

    assert attempts_file.exists() is False
    assert {path.name for path in receipt_dir.iterdir()} == archived_receipts


def test_attempt_append_retries_legal_short_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _source = _create_case(tmp_path)
    real_write = os.write
    write_sizes: list[int] = []

    def short_write(descriptor: int, value: bytes) -> int:
        chunk_size = min(11, len(value))
        write_sizes.append(chunk_size)
        return real_write(descriptor, value[:chunk_size])

    monkeypatch.setattr(os, "write", short_write)
    result = store.record_attempt(
        "cache-invalidation-case",
        PracticeStage.EXPLAIN,
        AttemptOutcome.NEEDS_WORK,
        note="The explanation omitted the transaction boundary.",
    )

    assert len(write_sizes) > 1
    assert store.read_attempts("cache-invalidation-case") == [result.attempt]
    assert result.packet_refreshed is True
    assert result.packet_error is None


@pytest.mark.parametrize("failure_kind", ["zero", "error"])
def test_attempt_append_rolls_back_partial_bytes_before_propagating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    store, _source = _create_case(tmp_path)
    store.record_attempt(
        "cache-invalidation-case",
        PracticeStage.EXPLAIN,
        AttemptOutcome.NEEDS_WORK,
        note="First intact attempt.",
    )
    attempts_file = store.cases_root / "cache-invalidation-case" / "attempts.jsonl"
    packet_file = store.cases_root / "cache-invalidation-case" / "interview-packet.md"
    original_attempts = attempts_file.read_bytes()
    original_packet = packet_file.read_bytes()
    real_write = os.write
    real_ftruncate = os.ftruncate
    real_fsync = os.fsync
    write_calls = 0
    truncated_to: list[int] = []
    fsync_calls = 0

    def fail_after_partial_write(descriptor: int, value: bytes) -> int:
        nonlocal write_calls
        write_calls += 1
        if write_calls == 1:
            return real_write(descriptor, value[:17])
        if failure_kind == "zero":
            return 0
        raise OSError("simulated append failure")

    def tracked_ftruncate(descriptor: int, length: int) -> None:
        truncated_to.append(length)
        real_ftruncate(descriptor, length)

    def tracked_fsync(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        real_fsync(descriptor)

    monkeypatch.setattr(os, "write", fail_after_partial_write)
    monkeypatch.setattr(os, "ftruncate", tracked_ftruncate)
    monkeypatch.setattr(os, "fsync", tracked_fsync)

    with pytest.raises(OSError):
        store.record_attempt(
            "cache-invalidation-case",
            PracticeStage.EXPLAIN,
            AttemptOutcome.NEEDS_WORK,
            note="This attempt must not leave a partial JSON line.",
        )

    assert attempts_file.read_bytes() == original_attempts
    assert packet_file.read_bytes() == original_packet
    assert truncated_to == [len(original_attempts)]
    assert fsync_calls == 1
    assert len(store.read_attempts("cache-invalidation-case")) == 1


def test_close_failure_after_fsync_returns_committed_result_and_keeps_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _source = _create_case(tmp_path)
    receipt_source = _receipt(tmp_path, "close-failure-receipt.md")
    real_render = render_interview_packet
    real_close = os.close
    close_failed = False

    def close_once_after_real_close(descriptor: int) -> None:
        nonlocal close_failed
        real_close(descriptor)
        if not close_failed:
            close_failed = True
            raise OSError("TOP SECRET close details")

    def render_then_arm_close_failure(
        case: LearningCase,
        mastery: MasterySnapshot,
    ) -> str:
        packet = real_render(case, mastery)
        monkeypatch.setattr(os, "close", close_once_after_real_close)
        return packet

    monkeypatch.setattr(
        casebook_store_module,
        "render_interview_packet",
        render_then_arm_close_failure,
    )
    result = store.record_attempt(
        "cache-invalidation-case",
        PracticeStage.EXPLAIN,
        AttemptOutcome.PASS,
        receipt_source,
    )

    assert close_failed is True
    assert result.commit_warning == (
        "OSError: attempt log close failed after durable commit"
    )
    assert "TOP SECRET" not in result.commit_warning
    assert result.packet_refreshed is True
    assert store.read_attempts("cache-invalidation-case") == [result.attempt]
    assert result.attempt.receipt is not None
    assert (store.root / result.attempt.receipt.archived_path).is_file()


def test_latest_attempt_per_stage_can_remove_readiness(tmp_path: Path) -> None:
    store, _source = _create_case(tmp_path)
    for stage in PracticeStage:
        receipt = _receipt(tmp_path, f"{stage.value}.md", f"receipt for {stage.value}")
        mastery = store.record_attempt(
            "cache-invalidation-case",
            stage,
            AttemptOutcome.PASS,
            receipt,
        ).mastery

    assert mastery.status is DerivedCaseStatus.SELF_ASSESSED_INTERVIEW_READY
    assert mastery.interview_ready is True
    assert mastery.next_stage is None

    regressed = store.record_attempt(
        "cache-invalidation-case",
        PracticeStage.TRACE,
        AttemptOutcome.NEEDS_WORK,
        note="I could not reproduce the trace unaided.",
    ).mastery
    assert regressed.status is DerivedCaseStatus.IN_PRACTICE
    assert regressed.interview_ready is False
    assert regressed.next_stage is PracticeStage.TRACE
    assert PracticeStage.TRACE not in regressed.passed_stages
    assert store.mastery("cache-invalidation-case") == regressed


def test_derive_mastery_is_pure_and_rejects_attempts_for_another_case(
    tmp_path: Path,
) -> None:
    store, _source = _create_case(tmp_path)
    case = store.load_case("cache-invalidation-case")
    attempts_before = store.read_attempts(case.id)

    first = derive_mastery(case, attempts_before)
    second = derive_mastery(case, attempts_before)
    assert first == second
    assert first.status is DerivedCaseStatus.CAPTURED
    assert first.next_stage is PracticeStage.EXPLAIN
    assert store.read_attempts(case.id) == []

    other_store, _ = _create_case(tmp_path / "other", case_id="another-learning-case")
    other_case = other_store.load_case("another-learning-case")
    receipt = _receipt(tmp_path, "other-receipt.md")
    other_attempt = other_store.record_attempt(
        other_case.id,
        PracticeStage.EXPLAIN,
        AttemptOutcome.PASS,
        receipt,
    ).attempt
    with pytest.raises(ValueError, match="cannot derive mastery"):
        derive_mastery(case, [other_attempt])


def test_integrity_verifies_evidence_and_practice_receipts_and_reports_tampering(
    tmp_path: Path,
) -> None:
    store, _source = _create_case(tmp_path)
    practice_source = _receipt(tmp_path, "explain.md")
    attempt = store.record_attempt(
        "cache-invalidation-case",
        PracticeStage.EXPLAIN,
        AttemptOutcome.PASS,
        practice_source,
    ).attempt

    intact = store.verify_integrity("cache-invalidation-case")
    assert intact.ok is True
    assert intact.checked_evidence == 1
    assert intact.checked_practice_receipts == 1
    assert intact.checked_files == 2
    assert intact.failures == ()
    assert intact.evidence_gap is False

    case = store.load_case("cache-invalidation-case")
    evidence_archive = store.root / case.evidence[0].archived_path
    assert attempt.receipt is not None
    practice_archive = store.root / attempt.receipt.archived_path
    evidence_archive.write_bytes(b"x")
    practice_archive.write_bytes(b"x")

    changed = store.verify_integrity("cache-invalidation-case")
    assert changed.ok is False
    assert len(changed.failures) == 2
    assert {failure.scope for failure in changed.failures} == {"evidence", "practice"}
    assert {failure.problem for failure in changed.failures} == {"byte-size-mismatch"}
    assert changed.evidence_gap is True
    assert all("private evidence" not in repr(failure) for failure in changed.failures)


def test_packet_is_refreshed_after_an_attempt(tmp_path: Path) -> None:
    store, _source = _create_case(tmp_path)
    packet = store.root / "cases" / "cache-invalidation-case" / "interview-packet.md"
    before = packet.read_text(encoding="utf-8")
    receipt = _receipt(tmp_path, "explain.md")

    store.record_attempt(
        "cache-invalidation-case",
        PracticeStage.EXPLAIN,
        AttemptOutcome.PASS,
        receipt,
    )

    after = packet.read_text(encoding="utf-8")
    assert after != before
    assert "Latest self-assessed result: `pass`" in after
    assert "Derived status: `in-practice`" in after


def test_packet_replace_failure_returns_committed_attempt_and_preserves_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _source = _create_case(tmp_path)
    packet = store.cases_root / "cache-invalidation-case" / "interview-packet.md"
    packet_before = packet.read_bytes()
    receipt = _receipt(tmp_path, "committed-receipt.md")

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("TOP SECRET destination details")

    monkeypatch.setattr(os, "replace", fail_replace)
    result = store.record_attempt(
        "cache-invalidation-case",
        PracticeStage.EXPLAIN,
        AttemptOutcome.PASS,
        receipt,
    )

    assert isinstance(result, AttemptRecordResult)
    assert result.packet_refreshed is False
    assert result.packet_error == (
        "OSError: interview packet refresh failed after attempt commit"
    )
    assert "TOP SECRET" not in result.packet_error
    assert store.read_attempts("cache-invalidation-case") == [result.attempt]
    assert result.attempt.receipt is not None
    assert (store.root / result.attempt.receipt.archived_path).is_file()
    assert packet.read_bytes() == packet_before
    assert not list(packet.parent.glob(".interview-packet.md-*.tmp"))


def test_packet_render_failure_before_append_rolls_back_new_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _source = _create_case(tmp_path)
    receipt = _receipt(tmp_path, "rolled-back-receipt.md")

    def fail_render(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("render failed before commit")

    monkeypatch.setattr(casebook_store_module, "render_interview_packet", fail_render)
    with pytest.raises(RuntimeError, match="render failed before commit"):
        store.record_attempt(
            "cache-invalidation-case",
            PracticeStage.EXPLAIN,
            AttemptOutcome.PASS,
            receipt,
        )

    assert store.read_attempts("cache-invalidation-case") == []
    receipt_dir = store.cases_root / "cache-invalidation-case" / "practice-receipts"
    assert list(receipt_dir.iterdir()) == []
