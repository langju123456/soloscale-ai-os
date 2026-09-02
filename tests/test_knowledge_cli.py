from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

from soloscale.cli import app

runner = CliRunner()


def _write_codex_session(codex_home: Path) -> Path:
    session_path = codex_home / "sessions" / "2026" / "08" / "09" / "rollout-thread.jsonl"
    session_path.parent.mkdir(parents=True)
    records = [
        {
            "timestamp": "2026-08-09T01:00:00Z",
            "type": "session_meta",
            "payload": {"id": "thread-cli-001", "title": "SoloScale BuildLog evidence"},
        },
        {
            "timestamp": "2026-08-09T01:01:00Z",
            "type": "response_item",
            "payload": {
                "id": "message-user-001",
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "SoloScale should preserve BuildLog evidence. API_TOKEN=private-value"
                        ),
                    }
                ],
            },
        },
        {
            "timestamp": "2026-08-09T01:02:00Z",
            "type": "response_item",
            "payload": {
                "id": "message-assistant-001",
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": "The evaluator failure needs a citation-backed recovery gate.",
                    }
                ],
            },
        },
    ]
    session_path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    return session_path


class _FakeReasoner:
    model = "fake-local-model"

    def complete(
        self,
        schema: type[BaseModel],
        *,
        system: str,
        user: str,
    ) -> BaseModel:
        del system
        if schema.__name__ == "QueryPlan":
            return schema.model_validate({"queries": ["SoloScale BuildLog"]})
        if schema.__name__ == "CoverageDecision":
            return schema.model_validate(
                {"finish": True, "additional_queries": [], "limitations": []}
            )
        if schema.__name__ == "GroundedDraft":
            payload: dict[str, Any] = json.loads(user)
            evidence_ids = payload["allowed_evidence_chunk_ids"]
            return schema.model_validate(
                {
                    "claims": [
                        {
                            "text": (
                                "The indexed conversation connects SoloScale and BuildLog evidence."
                            ),
                            "evidence_chunk_ids": [evidence_ids[0]],
                        }
                    ],
                    "unsupported": [],
                    "open_questions": ["Human confirmation is still required."],
                    "suggested_case_title": "Conversation evidence recovery",
                    "suggested_outputs": ["interview case"],
                }
            )
        raise AssertionError(f"unexpected schema {schema.__name__}")


def test_knowledge_cli_sync_search_status_and_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    codex_home = tmp_path / "codex-home"
    _write_codex_session(codex_home)

    first_sync = runner.invoke(
        app,
        [
            "knowledge-sync",
            "--codex-home",
            str(codex_home),
        ],
    )
    assert first_sync.exit_code == 0, first_sync.stdout
    assert "Imported" in first_sync.stdout
    assert "1" in first_sync.stdout
    assert "private-value" not in first_sync.stdout

    second_sync = runner.invoke(
        app,
        [
            "knowledge-sync",
            "--codex-home",
            str(codex_home),
        ],
    )
    assert second_sync.exit_code == 0, second_sync.stdout
    assert "Unchanged" in second_sync.stdout

    status = runner.invoke(app, ["knowledge-status"])
    assert status.exit_code == 0, status.stdout
    assert "Documents" in status.stdout
    assert "Chunks" in status.stdout
    assert "codex_session" in status.stdout

    search = runner.invoke(app, ["knowledge-search", "SoloScale BuildLog"])
    assert search.exit_code == 0, search.stdout
    assert "codex_session" in search.stdout
    assert "[REDACTED]" in search.stdout
    assert "private-value" not in search.stdout

    fake_reasoner = _FakeReasoner()
    monkeypatch.setattr(
        "soloscale.cli.OllamaReasoner",
        lambda **_kwargs: fake_reasoner,
    )
    agent = runner.invoke(
        app,
        ["evidence-agent", "What connects SoloScale and BuildLog?"],
    )
    assert agent.exit_code == 0, agent.stdout
    assert "human confirmation required" in agent.stdout
    assert "No Casebook, BuildLog, resume, or publishing record was changed" in agent.stdout
    assert "private-value" not in agent.stdout
    run_results = list(
        (tmp_path / ".soloscale" / "knowledge" / "agent-runs").glob("*/04_result.json")
    )
    assert len(run_results) == 1


def test_knowledge_sync_defers_a_bad_export_without_printing_contents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    private_marker = "PRIVATE EXPORT BODY"
    bad_export = tmp_path / "conversations.json"
    bad_export.write_text(private_marker, encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "knowledge-sync",
            "--no-codex",
            "--chatgpt-export",
            str(bad_export),
        ],
    )

    assert result.exit_code == 1, result.stdout
    assert "Failed" in result.stdout
    assert "failed for every discovered source" in result.stdout
    assert private_marker not in result.stdout
    assert "traceback" not in result.stdout.lower()


def test_knowledge_sync_partial_success_keeps_zero_exit_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    codex_home = tmp_path / "codex-home"
    _write_codex_session(codex_home)
    bad_export = tmp_path / "conversations.json"
    bad_export.write_text("PRIVATE INVALID EXPORT", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "knowledge-sync",
            "--codex-home",
            str(codex_home),
            "--chatgpt-export",
            str(bad_export),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert "Imported" in result.stdout
    assert "Some sources were deferred" in result.stdout
    assert "PRIVATE INVALID EXPORT" not in result.stdout


def test_knowledge_reset_requires_confirmation_and_preserves_agent_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    codex_home = tmp_path / "codex-home"
    _write_codex_session(codex_home)
    synced = runner.invoke(
        app,
        ["knowledge-sync", "--codex-home", str(codex_home)],
    )
    assert synced.exit_code == 0, synced.stdout
    receipt = (
        tmp_path / ".soloscale" / "knowledge" / "agent-runs" / "preserved-run" / "04_result.json"
    )
    receipt.parent.mkdir(parents=True)
    receipt.write_text("{}\n", encoding="utf-8")
    database = tmp_path / ".soloscale" / "knowledge" / "index.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 99")

    refused = runner.invoke(app, ["knowledge-reset"])
    reset = runner.invoke(app, ["knowledge-reset", "--yes"])
    status = runner.invoke(app, ["knowledge-status"])

    assert refused.exit_code == 2
    assert reset.exit_code == 0, reset.stdout
    assert "receipts were preserved" in reset.stdout
    assert receipt.read_text(encoding="utf-8") == "{}\n"
    assert status.exit_code == 0
    assert "Documents" in status.stdout and "0" in status.stdout


def test_knowledge_reset_recovers_non_sqlite_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    knowledge_root = tmp_path / ".soloscale" / "knowledge"
    knowledge_root.mkdir(parents=True)
    (knowledge_root / "index.sqlite3").write_bytes(b"not a sqlite database")

    reset = runner.invoke(app, ["knowledge-reset", "--yes"])
    status = runner.invoke(app, ["knowledge-status"])

    assert reset.exit_code == 0, reset.stdout
    assert status.exit_code == 0, status.stdout
    assert "Documents" in status.stdout and "0" in status.stdout
