#!/usr/bin/env python3
"""Run synthetic Evidence Agent edge-case checks and retain a private receipt."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
import xml.etree.ElementTree as element_tree
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_CATEGORIES = {
    "empty_evidence_abstention": "test_empty_evidence_abstains_without_grounded_draft_call",
    "conflicting_source_attribution": "test_conflicting_sources_remain_attributed_for_human_review",
    "long_utf8_context": "test_long_utf8_context_keeps_focused_critical_tail",
    "timeouts": "test_timeouts_have_sanitized_receipts_and_no_result",
    "malformed_output": "test_ollama_malformed_output_fails_closed_without_accepted_result",
}


class PrivateOutputLocationError(Exception):
    """Raised before writing through an unsafe private-report path."""


def _ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise PrivateOutputLocationError("private output location is a symlink")
    try:
        if path.exists():
            if not path.is_dir():
                raise PrivateOutputLocationError("private output location is not a directory")
        else:
            path.mkdir(mode=_DIRECTORY_MODE)
        if path.is_symlink() or not path.is_dir():
            raise PrivateOutputLocationError("private output location is unsafe")
        os.chmod(path, _DIRECTORY_MODE)
    except PrivateOutputLocationError:
        raise
    except OSError:
        raise PrivateOutputLocationError("private output location could not be prepared") from None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_hashes(paths: dict[str, Path]) -> tuple[dict[str, str | None], list[str]]:
    hashes: dict[str, str | None] = {}
    errors: list[str] = []
    for label, path in paths.items():
        try:
            hashes[label] = _sha256(path)
        except OSError as error:
            hashes[label] = None
            errors.append(f"{label}: {type(error).__name__}")
    return hashes, errors


def _write_private(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    os.chmod(path, _FILE_MODE)


def _git_output(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, check=False, capture_output=True, text=True
    )
    return completed.stdout.strip() if completed.returncode == 0 else "UNKNOWN"


def _junit_cases(junit_path: Path) -> tuple[list[dict[str, str]], str | None]:
    try:
        root = element_tree.parse(junit_path).getroot()
    except (OSError, element_tree.ParseError) as error:
        return [], f"JUnit report could not be parsed: {type(error).__name__}"

    cases: list[dict[str, str]] = []
    for node in root.iter("testcase"):
        status = "passed"
        if node.find("failure") is not None or node.find("error") is not None:
            status = "failed"
        elif node.find("skipped") is not None:
            status = "skipped"
        cases.append(
            {
                "name": node.attrib.get("name", "UNKNOWN"),
                "classname": node.attrib.get("classname", "UNKNOWN"),
                "status": status,
            }
        )
    if not cases:
        return [], "JUnit report contained no test cases"
    return cases, None


def _category_report(cases: list[dict[str, str]]) -> dict[str, dict[str, object]]:
    report: dict[str, dict[str, object]] = {}
    for category, marker in _CATEGORIES.items():
        matching = [case for case in cases if marker in case["name"]]
        statuses = [str(case["status"]) for case in matching]
        report[category] = {
            "test_cases": matching,
            "present": bool(matching),
            "passed": bool(matching) and all(status == "passed" for status in statuses),
            "skipped": any(status == "skipped" for status in statuses),
        }
    return report


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Evidence Agent edge-case evaluation",
        "",
        f"Status: {'PASS' if report['passed'] else 'FAIL'}",
        "",
        (
            "Synthetic, deterministic control-flow evaluation only. It makes zero live model, "
            "provider, or network calls. It does not evaluate semantic accuracy, load, "
            "P95 latency, "
            "or production stability."
        ),
        "",
        "| Category | Present | Passed | Skipped |",
        "| --- | --- | --- | --- |",
    ]
    for name, category in report["categories"].items():
        lines.append(
            f"| {name} | {category['present']} | {category['passed']} | {category['skipped']} |"
        )
    lines.extend(
        [
            "",
            f"Pytest exit: {report['pytest']['exit_code']}",
            f"Command: `{' '.join(report['pytest']['command'])}`",
            f"Source HEAD: `{report['source']['head']}`",
        ]
    )
    if report["junit_error"]:
        lines.extend(["", f"JUnit error: {report['junit_error']}"])
    return "\n".join(lines) + "\n"


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    private_root = repo / ".soloscale"
    output_root = private_root / "agent-edge-case-evals"
    try:
        _ensure_private_directory(private_root)
        _ensure_private_directory(output_root)
    except PrivateOutputLocationError as error:
        print(f"edge-case evaluation rejected unsafe output location: {error}", file=sys.stderr)
        return 2
    run_dir = output_root / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:12]}"
    run_dir.mkdir(mode=_DIRECTORY_MODE)
    junit_path = run_dir / "pytest-junit.xml"
    log_path = run_dir / "pytest.log"
    test_path = repo / "tests" / "test_agent_edge_cases.py"
    source_path = repo / "src" / "soloscale" / "evidence_agent.py"
    script_path = Path(__file__).resolve()
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        str(test_path),
        f"--junitxml={junit_path}",
    ]
    completed = subprocess.run(command, cwd=repo, check=False, capture_output=True, text=True)
    _write_private(log_path, completed.stdout + completed.stderr)
    if junit_path.exists():
        os.chmod(junit_path, _FILE_MODE)
    cases, junit_error = _junit_cases(junit_path)
    categories = _category_report(cases)
    source_hashes, source_errors = _source_hashes(
        {
            "src/soloscale/evidence_agent.py": source_path,
            "tests/test_agent_edge_cases.py": test_path,
            "scripts/evaluate_agent_edge_cases.py": script_path,
        }
    )
    all_collected_cases_passed = bool(cases) and all(
        case["status"] == "passed" for case in cases
    )
    categories_passed = all(
        bool(category["present"]) and bool(category["passed"]) for category in categories.values()
    )
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "evaluation": "evidence-agent-edge-cases",
        "fixture_provenance": "synthetic scripted/control-flow fault injection",
        "live_model_provider_or_network_calls": False,
        "not_evaluated": ["semantic accuracy", "load", "P95 latency", "production stability"],
        "source": {
            "head": _git_output(repo, "rev-parse", "HEAD"),
            "dirty": bool(_git_output(repo, "status", "--porcelain")),
            "sha256": source_hashes,
            "errors": source_errors,
        },
        "pytest": {"command": command, "exit_code": completed.returncode},
        "artifacts": {"junit": junit_path.name, "log": log_path.name},
        "test_cases": cases,
        "categories": categories,
        "junit_error": junit_error,
        "passed": (
            completed.returncode == 0
            and junit_error is None
            and not source_errors
            and all_collected_cases_passed
            and categories_passed
        ),
    }
    _write_private(run_dir / "report.json", json.dumps(report, indent=2, sort_keys=True) + "\n")
    _write_private(run_dir / "report.md", _markdown(report))
    print(run_dir)
    return 0 if report["passed"] else completed.returncode or 1


if __name__ == "__main__":
    raise SystemExit(main())
