from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import soloscale.retrieval_evaluation as retrieval_evaluation
from soloscale.retrieval_evaluation import (
    RetrievalEvaluationError,
    _rank_metrics,
    compare_reports,
    run_evaluations,
)


def test_rank_metrics_excludes_empty_and_handles_missing_multi_relevant() -> None:
    metrics = _rank_metrics(
        [
            {"expected_ids": ["a", "b"], "ranked_ids": ["b"], "expect_empty": False},
            {"expected_ids": ["c"], "ranked_ids": [], "expect_empty": False},
            {"expected_ids": [], "ranked_ids": [], "expect_empty": True},
        ],
        top_k=5,
    )
    assert metrics == {"evaluated_queries": 2, "recall_at_5": 0.25, "mrr_at_5": 0.5}


def test_unknown_conversation_query_label_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "conversation_rag_eval.json").read_text()
    )
    fixture["queries"][0]["relevant_chunk_ids"] = ["unknown-id"]
    monkeypatch.setattr(retrieval_evaluation, "_fixture", lambda _name: (fixture, "hash"))
    with pytest.raises(RetrievalEvaluationError, match="labels"):
        retrieval_evaluation._conversation_report()


def test_unknown_resume_query_label_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "resume_retrieval_eval.json").read_text()
    )
    fixture[0]["relevant_evidence_ids"] = ["unknown-id"]
    monkeypatch.setattr(retrieval_evaluation, "_fixture", lambda _name: (fixture, "hash"))
    with pytest.raises(RetrievalEvaluationError, match="unknown relevant"):
        retrieval_evaluation._resume_report()


def test_comparison_rejects_different_retrieval_config() -> None:
    current = {"dataset": "d", "dataset_sha256": "a", "corpus_sha256": "b", "top_k": 5}
    previous = {**current, "top_k": 10}
    with pytest.raises(RetrievalEvaluationError, match="top_k"):
        compare_reports(current, previous)


def test_comparison_includes_nested_integer_and_float_metric_deltas() -> None:
    current = {
        "dataset": "d",
        "dataset_sha256": "a",
        "corpus_sha256": "b",
        "top_k": 5,
        "queries": [],
        "metrics": {
            "evaluated_queries": 3,
            "recall_at_5": 0.75,
            "latency_ms": {"n": 3, "median": 2.5, "enabled": True},
        },
    }
    previous = {
        **current,
        "metrics": {
            "evaluated_queries": 2,
            "recall_at_5": 0.5,
            "latency_ms": {"n": 2, "median": 1.25, "enabled": False},
        },
    }

    assert compare_reports(current, previous)["metric_deltas"] == {
        "evaluated_queries": 1,
        "latency_ms": {"median": 1.25, "n": 1},
        "recall_at_5": 0.25,
    }


def test_compare_cli_rejects_missing_queries_without_traceback(tmp_path: Path) -> None:
    baseline = run_evaluations(tmp_path)
    report_path = baseline / "report.json"
    report = json.loads(report_path.read_text())
    del report["reports"][0]["queries"]
    report_path.write_text(json.dumps(report))

    result = subprocess.run(
        [
            sys.executable,
            "scripts/evaluate_retrieval.py",
            "--output-root",
            str(tmp_path / "output"),
            "--compare",
            str(baseline),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "invalid comparison report" in result.stderr
    assert "Traceback" not in result.stderr


def test_runs_are_non_overwriting_private_subdirectories(tmp_path: Path) -> None:
    first = run_evaluations(tmp_path)
    first_bytes = (first / "report.json").read_bytes()
    second = run_evaluations(tmp_path)
    assert first != second
    assert (first / "report.json").read_bytes() == first_bytes
    assert (second / "report.json").is_file()
    assert (second / "report.md").is_file()
    assert first.stat().st_mode & 0o777 == 0o700
    assert (second / "report.json").stat().st_mode & 0o777 == 0o600


def test_failed_gate_is_persisted_before_cli_can_return_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = retrieval_evaluation._conversation_report

    def failing_report() -> dict[str, object]:
        report = original()
        report["gates"] = {"forced_regression": False}
        return report

    monkeypatch.setattr(retrieval_evaluation, "_conversation_report", failing_report)
    run = run_evaluations(tmp_path)
    saved = json.loads((run / "report.json").read_text())
    assert saved["passed"] is False
    assert saved["reports"][0]["gates"] == {"forced_regression": False}
