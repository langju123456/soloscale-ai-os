from __future__ import annotations

import sys
from types import SimpleNamespace

from pytest import MonkeyPatch

import scripts.evaluate_agent_live as live_evaluation


def test_fixture_expands_utf8_repeat_and_keeps_three_synthetic_cases() -> None:
    cases = live_evaluation._cases()
    long_case = next(case for case in cases if case["id"] == "long_utf8")

    sources = live_evaluation._sources_for(long_case)

    assert {case["id"] for case in cases} == {"noanswer", "conflict", "long_utf8"}
    assert "LIVE_UTF8_CRITICAL_773 使用持久队列和幂等键。" in sources[0].chunks[0].text
    assert len(sources[0].chunks[0].text.encode("utf-8")) > 700


def test_missing_model_rejects_before_inference(monkeypatch: MonkeyPatch) -> None:
    calls = 0

    def no_models(_endpoint: str, _timeout: float) -> dict[str, str]:
        return {}

    def unexpected_case(*_: object, **__: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise AssertionError("inference should not run")

    monkeypatch.setattr(live_evaluation, "_tags", no_models)
    monkeypatch.setattr(live_evaluation, "_case_run", unexpected_case)
    monkeypatch.setattr(sys, "argv", ["evaluate_agent_live.py"])

    assert live_evaluation.main() == 2
    assert calls == 0


def test_structural_gates_reject_missing_conflict_reference_and_utf8_budget() -> None:
    cases = {case["id"]: case for case in live_evaluation._cases()}
    incomplete_conflict = SimpleNamespace(
        context_chunk_ids=["live-conflict-complete", "live-conflict-incomplete"],
        refs=[SimpleNamespace(chunk_id="live-conflict-complete", excerpt="complete")],
        unsupported=["Unresolved."],
        open_questions=[],
    )
    oversized_utf8 = SimpleNamespace(
        context_bytes_used=701,
        refs=[
            SimpleNamespace(
                chunk_id="live-utf8-critical",
                excerpt="LIVE_UTF8_CRITICAL_773 使用持久队列和幂等键。",
            )
        ],
    )

    assert not live_evaluation._structural_passed(cases["conflict"], incomplete_conflict)
    assert not live_evaluation._structural_passed(cases["long_utf8"], oversized_utf8)
