#!/usr/bin/env python3
"""Opt-in structural evaluation against an already-installed local Ollama model."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from soloscale.evidence_agent import (
    BoundedEvidenceAgent,
    OllamaCallProfile,
    OllamaReasoner,
    _NoRedirectHandler,
)
from soloscale.knowledge_store import KnowledgeStore
from soloscale.retrieval_evaluation import _sources

_MODE_DIR = 0o700
_MODE_FILE = 0o600
_REPO = Path(__file__).resolve().parents[1]
_FIXTURE = _REPO / "tests/fixtures/agent_live_eval.json"


class LiveEvaluationError(ValueError):
    pass


def _private_dir(path: Path) -> None:
    if path.is_symlink():
        raise LiveEvaluationError("unsafe private output location")
    try:
        if path.exists():
            if not path.is_dir():
                raise LiveEvaluationError("unsafe private output location")
        else:
            path.mkdir(mode=_MODE_DIR)
        if path.is_symlink() or not path.is_dir():
            raise LiveEvaluationError("unsafe private output location")
        os.chmod(path, _MODE_DIR)
    except LiveEvaluationError:
        raise
    except OSError:
        raise LiveEvaluationError("private output location could not be prepared") from None


def _hash(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _cases() -> list[dict[str, Any]]:
    try:
        value = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LiveEvaluationError("invalid live evaluation fixture") from error
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise LiveEvaluationError("invalid live evaluation fixture")
    cases = value.get("cases")
    expected_ids = {"noanswer", "conflict", "long_utf8"}
    if (
        not isinstance(cases, list)
        or len(cases) != 3
        or not all(isinstance(case, dict) for case in cases)
        or {case.get("id") for case in cases} != expected_ids
    ):
        raise LiveEvaluationError("invalid live evaluation cases")
    return [copy.deepcopy(case) for case in cases if isinstance(case, dict)]


def _sources_for(case: dict[str, Any]) -> list[Any]:
    fixture = {"schema_version": 1, "documents": copy.deepcopy(case["documents"])}
    for document in fixture["documents"]:
        for chunk in document["chunks"]:
            repeat = chunk.pop("text_repeat", 1)
            suffix = chunk.pop("text_suffix", "")
            if not isinstance(repeat, int) or repeat < 1 or not isinstance(suffix, str):
                raise LiveEvaluationError("invalid live evaluation text repeat")
            if repeat != 1:
                chunk["text"] = str(chunk["text"]) * repeat + suffix
    return _sources(fixture)


def _tags(endpoint: str, timeout: float, opener: Any = None) -> dict[str, str]:
    OllamaReasoner(endpoint=endpoint, timeout=timeout)
    request = urllib.request.Request(f"{endpoint.rstrip('/')}/api/tags", method="GET")
    open_request = (
        opener
        or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler()).open
    )
    try:
        with open_request(request, timeout=timeout) as response:
            payload = json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError, UnicodeDecodeError):
        raise LiveEvaluationError("local model inventory check failed") from None
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise LiveEvaluationError("local model inventory check failed")
    return {
        item["name"]: item.get("digest", "unknown")
        for item in models
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


class _ProfileReasoner:
    def __init__(self, inner: OllamaReasoner) -> None:
        self.inner = inner
        self.model = inner.model
        self.profiles: list[dict[str, Any]] = []
        self.attempts = 0

    def complete(self, schema: Any, *, system: str, user: str) -> Any:
        self.attempts += 1
        try:
            return self.inner.complete(schema, system=system, user=user)
        finally:
            profile: OllamaCallProfile | None = self.inner.last_call_profile
            if profile is not None:
                self.profiles.append(profile.model_dump(mode="json"))


def _structural_passed(case: dict[str, Any], result: Any) -> bool:
    expected = case["expect"]
    if case["id"] == "noanswer":
        return not result.claims and not result.refs
    if case["id"] == "conflict":
        return (
            set(expected["context_ids"]).issubset(result.context_chunk_ids)
            and set(expected["ref_ids"]).issubset({ref.chunk_id for ref in result.refs})
            and bool(result.unsupported or result.open_questions)
        )
    literal = expected["critical_literal"]
    return result.context_bytes_used <= expected["context_bytes_at_most"] and any(
        literal in ref.excerpt for ref in result.refs
    )


def _write(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.chmod(path, _MODE_FILE)


def _git(*args: str) -> tuple[str | None, str | None]:
    completed = subprocess.run(
        ["git", *args], cwd=_REPO, check=False, capture_output=True, text=True
    )
    if completed.returncode != 0:
        return None, f"git {' '.join(args)} failed"
    return completed.stdout.strip(), None


def _report_markdown(report: dict[str, Any], selected_count: int) -> str:
    status = "PASS" if report["passed"] else "PARTIAL" if not report["full_suite"] else "FAIL"
    lines = [
        "# Agent live evaluation",
        "",
        f"Status: {status}",
        f"Model: `{report['model']['name']}` (`{report['model']['digest']}`)",
        f"Scope: {len(report['cases'])}/{selected_count} selected synthetic cases",
        f"Full suite: {report['full_suite']}; full-suite pass: {report['full_suite_passed']}",
        "",
        "| Case | Structural | Semantic review | Profiles |",
        "| --- | --- | --- | --- |",
    ]
    for case in report["cases"]:
        lines.append(
            f"| {case['id']} | {case['structural_passed']} | {case['semantic_review']} | "
            f"{len(case.get('call_profiles', []))} |"
        )
    lines.append("\nStructural checks only; semantic review is pending human review.\n")
    return "\n".join(lines)


def _case_run(
    case: dict[str, Any], run_dir: Path, endpoint: str, model: str, timeout: float
) -> dict[str, Any]:
    record: dict[str, Any] = {"id": case["id"], "semantic_review": "PENDING_HUMAN_REVIEW"}
    reasoner: _ProfileReasoner | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="soloscale-agent-live-") as temp:
            store = KnowledgeStore(Path(temp) / ".soloscale")
            sources = _sources_for(case)
            if store.sync(sources).failed:
                raise LiveEvaluationError("synthetic case index sync failed")
            reasoner = _ProfileReasoner(
                OllamaReasoner(endpoint=endpoint, model=model, timeout=timeout)
            )
            agent = BoundedEvidenceAgent(
                store,
                reasoner,
                run_dir / case["id"],
                max_rounds=1,
                max_queries_per_round=2,
                max_hits=4,
                excerpt_byte_budget=case.get("excerpt_byte_budget", 1500),
                context_byte_budget=case.get("context_byte_budget", 16000),
            )
            result = agent.run(case["question"])
        payload = result.model_dump(mode="json")
        passed = _structural_passed(case, result)
        record.update(
            {
                "structural_passed": passed,
                "candidate": payload,
                "reasoner_attempts": reasoner.attempts,
                "call_profiles": reasoner.profiles,
            }
        )
    except Exception as error:
        record.update(
            {
                "structural_passed": False,
                "error_type": type(error).__name__,
                "error": "live case failed safely",
            }
        )
        if reasoner is not None:
            record.update(
                {"reasoner_attempts": reasoner.attempts, "call_profiles": reasoner.profiles}
            )
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--case", choices=["noanswer", "conflict", "long_utf8"])
    args = parser.parse_args()
    try:
        cases = _cases()
        inventory = _tags(args.endpoint, args.timeout)
        if args.model not in inventory:
            raise LiveEvaluationError("requested local model is not installed")
        root = _REPO / ".soloscale"
        output = root / "agent-live-evals"
        _private_dir(root)
        _private_dir(output)
    except LiveEvaluationError as error:
        print(f"live evaluation unavailable: {error}", file=sys.stderr)
        return 2
    run_dir = output / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:12]}"
    run_dir.mkdir(mode=_MODE_DIR)
    selected = [case for case in cases if args.case is None or case["id"] == args.case]
    full_suite = args.case is None
    hashes = {
        "fixture": _hash(_FIXTURE),
        "script": _hash(Path(__file__)),
        "evidence_agent": _hash(_REPO / "src/soloscale/evidence_agent.py"),
    }
    head, head_error = _git("rev-parse", "HEAD")
    dirty_text, dirty_error = _git("status", "--porcelain")
    source_errors = [error for error in (head_error, dirty_error) if error is not None]
    source_errors.extend(label for label, digest in hashes.items() if digest is None)
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "fixture_provenance": "public synthetic development cases, not a heldout benchmark",
        "model": {
            "name": args.model,
            "digest": inventory[args.model],
            "endpoint": args.endpoint,
            "timeout_seconds": args.timeout,
        },
        "source": {
            "head": head,
            "dirty": None if dirty_text is None else bool(dirty_text),
            "hashes": hashes,
            "errors": source_errors,
        },
        "not_evaluated": [
            "semantic accuracy",
            "contradiction detection",
            "load",
            "P95",
            "production stability",
        ],
        "cases": [],
        "selected_case_ids": [case["id"] for case in selected],
        "full_suite": full_suite,
        "selected_structural_passed": False,
        "full_suite_passed": False,
        "passed": False,
    }
    for case in selected:
        report["cases"].append(_case_run(case, run_dir, args.endpoint, args.model, args.timeout))
        report["passed"] = False
        _write(run_dir / "report.json", report)
        (run_dir / "report.md").write_text(
            _report_markdown(report, len(selected)), encoding="utf-8"
        )
        os.chmod(run_dir / "report.md", _MODE_FILE)
    report["selected_structural_passed"] = (
        len(report["cases"]) == len(selected)
        and head is not None
        and not source_errors
        and all(value is not None for value in hashes.values())
        and bool(report["cases"])
        and all(case["structural_passed"] for case in report["cases"])
    )
    report["full_suite_passed"] = full_suite and report["selected_structural_passed"]
    report["passed"] = report["full_suite_passed"]
    _write(run_dir / "report.json", report)
    (run_dir / "report.md").write_text(_report_markdown(report, len(selected)), encoding="utf-8")
    os.chmod(run_dir / "report.md", _MODE_FILE)
    print(run_dir)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
