from pathlib import Path

import pytest
from typer.testing import CliRunner

from soloscale.cli import app
from soloscale.models import TaskEnvelope

runner = CliRunner()


def test_demo_runs_outside_repository_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["demo"])

    assert result.exit_code == 0
    assert "Task Envelope" in result.stdout
    assert "Execution Packet" in result.stdout
    assert "feat/source-grounded-citations" in result.stdout


def test_task_create_uses_annotated_cli_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        [
            "task-create",
            "--title",
            "Create a versioned task",
            "--goal",
            "Write a strict Task Envelope through the command line.",
            "--requires-local",
            "--branch",
            "feat/versioned-task",
            "--frozen-decision",
            "Preserve the current API.",
            "--frozen-decision",
            "Keep evidence append-only.",
            "--test-to-run",
            "pytest",
            "--test-to-run",
            "ruff check .",
        ],
    )

    assert result.exit_code == 0
    task_files = list((tmp_path / ".soloscale" / "tasks").glob("*/task.json"))
    assert len(task_files) == 1
    task = TaskEnvelope.model_validate_json(task_files[0].read_text(encoding="utf-8"))
    assert task.requires_local_files
    assert task.requires_terminal
    assert task.branch == "feat/versioned-task"
    assert task.frozen_decisions == [
        "Preserve the current API.",
        "Keep evidence append-only.",
    ]
    assert task.tests_to_run == ["pytest", "ruff check ."]
    assert task.schema_version == "0.1"
