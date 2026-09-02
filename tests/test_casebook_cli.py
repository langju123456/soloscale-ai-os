from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from soloscale.casebook_models import (
    AttemptOutcome,
    LearningCase,
    PracticeStage,
)
from soloscale.casebook_store import AttemptRecordResult, CasebookStore
from soloscale.cli import app

runner = CliRunner()


def _create_case(tmp_path: Path, data_root: Path | None = None) -> tuple[Path, str]:
    evidence = tmp_path / "selected-session.md"
    evidence.write_text("PRIVATE RAW MARKER: do not render\n", encoding="utf-8")
    arguments = [
        "case-create",
        "--case-id",
        "structured-output-failure",
        "--title",
        "Structured output failure",
        "--project",
        "BuildLog",
        "--problem",
        "The evaluator stopped on invalid JSON.",
        "--expected",
        "Evaluator output parses before schema validation.",
        "--actual",
        "JSON parsing failed before schema validation.",
        "--root-cause",
        "The rejected response was invalid JSON; exact content is unknown.",
        "--resolution",
        "Preserve evidence and diagnose before selecting recovery work.",
        "--verification",
        "The failure is classified at the evaluator parse boundary.",
        "--concept",
        "Parsing versus schema validation",
        "--unknown",
        "The raw rejected response was not retained.",
        "--evidence",
        f"chat={evidence}",
    ]
    if data_root is not None:
        arguments.extend(["--data-root", str(data_root)])
    result = runner.invoke(
        app,
        arguments,
    )
    assert result.exit_code == 0, result.stdout
    assert "PRIVATE RAW MARKER" not in result.stdout
    return evidence, result.stdout


def test_casebook_cli_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _evidence, create_output = _create_case(tmp_path)

    case_file = (
        tmp_path
        / ".soloscale"
        / "cases"
        / "structured-output-failure"
        / "case.json"
    )
    packet_file = case_file.parent / "interview-packet.md"
    dashboard_file = tmp_path / ".soloscale" / "control-tower" / "index.html"
    assert case_file.is_file()
    assert packet_file.is_file()
    assert dashboard_file.is_file()
    case = LearningCase.model_validate_json(case_file.read_text(encoding="utf-8"))
    assert len(case.evidence) == 1
    assert "PRIVATE RAW MARKER" not in packet_file.read_text(encoding="utf-8")
    assert "PRIVATE RAW MARKER" not in dashboard_file.read_text(encoding="utf-8")
    assert "structured-output-failure" in create_output

    status = runner.invoke(app, ["case-status", "structured-output-failure"])
    assert status.exit_code == 0, status.stdout
    assert "CAPTURED" in status.stdout
    assert "0/5" in status.stdout
    assert "EXPLAIN" in status.stdout
    assert "PASS" in status.stdout

    receipt = tmp_path / "my-explanation.md"
    receipt.write_text("My unaided explanation of the parse boundary.\n", encoding="utf-8")
    out_of_order = runner.invoke(
        app,
        [
            "case-attempt",
            "structured-output-failure",
            "--stage",
            "trace",
            "--outcome",
            "pass",
            "--receipt",
            str(receipt),
        ],
    )
    assert out_of_order.exit_code != 0

    attempt = runner.invoke(
        app,
        [
            "case-attempt",
            "structured-output-failure",
            "--stage",
            "explain",
            "--outcome",
            "pass",
            "--receipt",
            str(receipt),
            "--note",
            "Explained without notes.",
        ],
    )
    assert attempt.exit_code == 0, attempt.stdout

    progressed = runner.invoke(app, ["case-status", "structured-output-failure"])
    assert progressed.exit_code == 0, progressed.stdout
    assert "IN_PRACTICE" in progressed.stdout
    assert "1/5" in progressed.stdout
    assert "TRACE" in progressed.stdout

    regression = runner.invoke(
        app,
        [
            "case-attempt",
            "structured-output-failure",
            "--stage",
            "explain",
            "--outcome",
            "needs-work",
            "--note",
            "Could not explain the missing raw-response limitation.",
        ],
    )
    assert regression.exit_code == 0, regression.stdout
    regressed = runner.invoke(app, ["case-status", "structured-output-failure"])
    assert regressed.exit_code == 0, regressed.stdout
    assert "0/5" in regressed.stdout
    assert "EXPLAIN" in regressed.stdout


def test_case_create_rejects_malformed_evidence_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("evidence\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "case-create",
            "--case-id",
            "bad-evidence-spec",
            "--title",
            "Invalid evidence specification",
            "--project",
            "Casebook",
            "--problem",
            "An evidence option is missing its kind.",
            "--expected",
            "The kind and path are separated with equals.",
            "--actual",
            "Only a path was supplied.",
            "--root-cause",
            "The command input is malformed.",
            "--resolution",
            "Reject the command before creating a case.",
            "--verification",
            "No final case directory exists.",
            "--concept",
            "Boundary validation",
            "--evidence",
            str(evidence),
        ],
    )

    assert result.exit_code != 0
    assert not (tmp_path / ".soloscale" / "cases" / "bad-evidence-spec").exists()


def test_case_file_infers_custom_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    custom_root = tmp_path / "private-casebook"
    _create_case(tmp_path, custom_root)
    case_file = custom_root / "cases" / "structured-output-failure" / "case.json"

    status = runner.invoke(app, ["case-status", str(case_file)])

    assert status.exit_code == 0, status.stdout
    assert "CAPTURED" in status.stdout

    receipt = tmp_path / "custom-root-explanation.md"
    receipt.write_text("A complete explanation from memory.\n", encoding="utf-8")
    attempt = runner.invoke(
        app,
        [
            "case-attempt",
            str(case_file),
            "--stage",
            "explain",
            "--outcome",
            "pass",
            "--receipt",
            str(receipt),
        ],
    )

    assert attempt.exit_code == 0, attempt.stdout
    attempts_file = case_file.parent / "attempts.jsonl"
    assert len(attempts_file.read_text(encoding="utf-8").splitlines()) == 1


def test_case_file_rejects_unrelated_or_malformed_files_without_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    private_marker = "PRIVATE INVALID MANIFEST CONTENT"
    unrelated = tmp_path / "unrelated.json"
    unrelated.write_text(private_marker, encoding="utf-8")

    unrelated_result = runner.invoke(app, ["case-status", str(unrelated)])

    assert unrelated_result.exit_code != 0
    assert "must be named case.json" in unrelated_result.output
    assert private_marker not in unrelated_result.output
    assert "validation error" not in unrelated_result.output.lower()
    assert "traceback" not in unrelated_result.output.lower()

    malformed = tmp_path / "private-casebook" / "cases" / "malformed-case" / "case.json"
    malformed.parent.mkdir(parents=True)
    malformed.write_text(private_marker, encoding="utf-8")

    malformed_result = runner.invoke(app, ["case-status", str(malformed)])

    assert malformed_result.exit_code != 0
    assert "not a valid Casebook manifest" in malformed_result.output
    assert private_marker not in malformed_result.output
    assert "validation error" not in malformed_result.output.lower()
    assert "traceback" not in malformed_result.output.lower()


def test_case_file_rejects_manifest_id_directory_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    custom_root = tmp_path / "private-casebook"
    _create_case(tmp_path, custom_root)
    original_dir = custom_root / "cases" / "structured-output-failure"
    mismatched_dir = custom_root / "cases" / "different-case-id"
    original_dir.rename(mismatched_dir)

    result = runner.invoke(app, ["case-status", str(mismatched_dir / "case.json")])

    assert result.exit_code != 0
    assert "id does not match its case directory" in result.output
    assert "validation error" not in result.output.lower()
    assert "traceback" not in result.output.lower()


def test_case_create_succeeds_when_control_tower_refresh_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    corrupt_case_file = tmp_path / ".soloscale" / "cases" / "corrupt-case" / "case.json"
    corrupt_case_file.parent.mkdir(parents=True)
    corrupt_case_file.write_text("PRIVATE CORRUPT CASE", encoding="utf-8")

    _evidence, output = _create_case(tmp_path)
    normalized_output = " ".join(output.split())

    created_case_file = (
        tmp_path
        / ".soloscale"
        / "cases"
        / "structured-output-failure"
        / "case.json"
    )
    assert created_case_file.is_file()
    assert "Created case structured-output-failure" in normalized_output
    assert "case is committed" in normalized_output
    assert "do not retry case-create" in normalized_output
    assert normalized_output.index("Created case") < normalized_output.index("Warning:")
    assert "PRIVATE CORRUPT CASE" not in output


def test_case_attempt_succeeds_when_control_tower_refresh_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _create_case(tmp_path)
    corrupt_case_file = tmp_path / ".soloscale" / "cases" / "corrupt-case" / "case.json"
    corrupt_case_file.parent.mkdir(parents=True)
    corrupt_case_file.write_text("PRIVATE CORRUPT CASE", encoding="utf-8")
    receipt = tmp_path / "dashboard-failure-explanation.md"
    receipt.write_text("A complete explanation from memory.\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "case-attempt",
            "structured-output-failure",
            "--stage",
            "explain",
            "--outcome",
            "pass",
            "--receipt",
            str(receipt),
        ],
    )
    normalized_output = " ".join(result.stdout.split())

    attempts_file = (
        tmp_path
        / ".soloscale"
        / "cases"
        / "structured-output-failure"
        / "attempts.jsonl"
    )
    assert result.exit_code == 0, result.stdout
    assert len(attempts_file.read_text(encoding="utf-8").splitlines()) == 1
    assert "Recorded EXPLAIN → PASS" in normalized_output
    assert "attempt is committed" in normalized_output
    assert "do not retry case-attempt" in normalized_output
    assert normalized_output.index("Recorded EXPLAIN") < normalized_output.index("Warning:")
    assert "PRIVATE CORRUPT CASE" not in result.stdout


def test_case_attempt_succeeds_when_interview_packet_refresh_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _create_case(tmp_path)
    receipt = tmp_path / "packet-failure-explanation.md"
    receipt.write_text("A complete explanation from memory.\n", encoding="utf-8")

    def fail_packet_refresh(_path: Path, _text: str) -> None:
        raise OSError("PRIVATE PACKET FAILURE")

    monkeypatch.setattr(
        "soloscale.casebook_store._atomic_write_text",
        fail_packet_refresh,
    )
    result = runner.invoke(
        app,
        [
            "case-attempt",
            "structured-output-failure",
            "--stage",
            "explain",
            "--outcome",
            "pass",
            "--receipt",
            str(receipt),
        ],
    )
    normalized_output = " ".join(result.stdout.split())

    attempts_file = (
        tmp_path
        / ".soloscale"
        / "cases"
        / "structured-output-failure"
        / "attempts.jsonl"
    )
    assert result.exit_code == 0, result.stdout
    assert len(attempts_file.read_text(encoding="utf-8").splitlines()) == 1
    assert "Recorded EXPLAIN → PASS" in normalized_output
    assert "attempt is committed" in normalized_output
    assert "interview packet refresh failed" in normalized_output
    assert "do not retry case-attempt" in normalized_output
    assert normalized_output.index("Recorded EXPLAIN") < normalized_output.index("Warning:")
    assert "PRIVATE PACKET FAILURE" not in result.stdout


def test_case_attempt_reports_durable_commit_warning_without_retry_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _create_case(tmp_path)
    receipt = tmp_path / "close-warning-explanation.md"
    receipt.write_text("A complete explanation from memory.\n", encoding="utf-8")
    real_record_attempt = CasebookStore.record_attempt

    def record_with_close_warning(
        store: CasebookStore,
        case_id: str,
        stage: PracticeStage,
        outcome: AttemptOutcome,
        receipt_path: Path | None = None,
        note: str | None = None,
    ) -> AttemptRecordResult:
        result = real_record_attempt(
            store,
            case_id,
            stage,
            outcome,
            receipt_path,
            note,
        )
        return replace(result, commit_warning="OSError: sanitized commit warning")

    monkeypatch.setattr(CasebookStore, "record_attempt", record_with_close_warning)
    result = runner.invoke(
        app,
        [
            "case-attempt",
            "structured-output-failure",
            "--stage",
            "explain",
            "--outcome",
            "pass",
            "--receipt",
            str(receipt),
        ],
    )
    normalized_output = " ".join(result.stdout.split())

    assert result.exit_code == 0, result.stdout
    assert "attempt reached durable commit" in normalized_output
    assert "do not retry case-attempt" in normalized_output
    attempts_file = (
        tmp_path
        / ".soloscale"
        / "cases"
        / "structured-output-failure"
        / "attempts.jsonl"
    )
    assert len(attempts_file.read_text(encoding="utf-8").splitlines()) == 1


def test_control_tower_cli_rejects_source_record_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _create_case(tmp_path)
    case_file = (
        tmp_path
        / ".soloscale"
        / "cases"
        / "structured-output-failure"
        / "case.json"
    )
    before = case_file.read_bytes()

    result = runner.invoke(
        app,
        ["control-tower-build", "--output", str(case_file)],
    )

    assert result.exit_code != 0
    assert "must stay within" in result.output
    assert "traceback" not in result.output.lower()
    assert case_file.read_bytes() == before


def test_cli_rejects_trackable_data_root_inside_git_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    unsafe_root = tmp_path / "casebook-data"

    result = runner.invoke(
        app,
        ["control-tower-build", "--data-root", str(unsafe_root)],
    )

    assert result.exit_code != 0
    normalized_output = " ".join(result.output.split())
    assert "inside a Git worktree" in normalized_output
    assert ".soloscale" in normalized_output
    assert "traceback" not in normalized_output.lower()
    assert not unsafe_root.exists()


def test_cli_allows_nested_data_root_under_worktree_soloscale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr("soloscale.cli._git_ignores_path", lambda *_args: True)
    private_root = tmp_path / ".soloscale" / "casebook-v01"

    result = runner.invoke(
        app,
        ["control-tower-build", "--data-root", str(private_root)],
    )

    assert result.exit_code == 0, result.output
    assert (private_root / "control-tower" / "index.html").is_file()


def test_default_data_root_is_allowed_from_worktree_subdirectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()
    subdirectory = tmp_path / "docs"
    subdirectory.mkdir()
    monkeypatch.chdir(subdirectory)
    monkeypatch.setattr("soloscale.cli._git_ignores_path", lambda *_args: True)

    result = runner.invoke(app, ["control-tower-build"])

    assert result.exit_code == 0, result.output
    assert (subdirectory / ".soloscale" / "control-tower" / "index.html").is_file()


def test_cli_rejects_unignored_soloscale_root_inside_git_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr("soloscale.cli._git_ignores_path", lambda *_args: False)

    result = runner.invoke(app, ["control-tower-build"])
    normalized_output = " ".join(result.output.split())

    assert result.exit_code != 0
    assert "selected .soloscale data root" in normalized_output
    assert "private evidence" in normalized_output
    assert "traceback" not in normalized_output.lower()
    assert not (tmp_path / ".soloscale").exists()


def test_cli_sanitizes_casebook_constructor_symlink_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    outside = tmp_path / "outside-private-data"
    outside.mkdir(mode=0o755)
    outside.chmod(0o755)
    (tmp_path / ".soloscale").symlink_to(outside, target_is_directory=True)

    result = runner.invoke(app, ["control-tower-build"])
    normalized_output = " ".join(result.output.split())

    assert result.exit_code != 0
    assert "symlinks" in normalized_output
    assert "traceback" not in normalized_output.lower()
    assert (outside.stat().st_mode & 0o777) == 0o755
    assert list(outside.iterdir()) == []
