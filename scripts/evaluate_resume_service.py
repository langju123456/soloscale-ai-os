#!/usr/bin/env python3
"""Opt-in synthetic HTTP/multi-process proof against an empty local database.

Only the interruption child wraps the template generator and shortens its lease.
Production code, database timestamps, and paid-call behavior are not modified.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import hashlib
import io
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from soloscale.evidence_agent import _NoRedirectHandler

_REPO = Path(__file__).resolve().parents[1]
_BODY_LIMIT = 8 * 1024 * 1024
_SOURCE_PATHS = (
    "scripts/evaluate_resume_service.py",
    "scripts/migrate_resume_cloud.py",
    "src/soloscale/resume_cloud_api.py",
    "src/soloscale/resume_cloud_service.py",
    "src/soloscale/resume_cloud_repository.py",
    "src/soloscale/resume_cloud_worker.py",
    "src/soloscale/resume_docx.py",
    "migrations/resume_cloud/0001_init.sql",
)


class ResumeServiceEvaluationError(ValueError):
    """Safe failures must never contain connection credentials."""


def _safe_loopback_postgres(dsn: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(dsn)
        port = parsed.port
        return (
            parsed.scheme in {"postgres", "postgresql"}
            and parsed.hostname in {"127.0.0.1", "::1"}
            and (port is None or 1 <= port <= 65535)
            and not parsed.query
            and not parsed.fragment
            and re.fullmatch(r"/resume_service_eval(?:_[a-zA-Z0-9]+)?", parsed.path) is not None
        )
    except ValueError:
        return False


def _private_directory(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ResumeServiceEvaluationError("unsafe private output directory")
    path.mkdir(mode=0o700, exist_ok=True)
    path.chmod(0o700)


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        temporary.chmod(0o600)
        json.dump(value, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")
    temporary.replace(path)


def _payload(index: int) -> dict[str, object]:
    text = f"Synthetic Candidate {index}\nPROJECT HIGHLIGHTS\n- Built bounded software.\n"
    return {
        "resume": {
            "filename": "synthetic.txt",
            "content_base64": base64.b64encode(text.encode()).decode(),
        },
        "job_description": f"Python Docker role SYNTH-{index}",
        "mode": "template",
        "approve_external_model_call": False,
    }


def _request(
    api_url: str,
    token: str,
    method: str,
    path: str,
    payload: object | None = None,
    key: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    parsed = urllib.parse.urlsplit(api_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ResumeServiceEvaluationError("HTTP target must be a loopback origin")
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(api_url + path, data=body, method=method)
    request.add_header("Authorization", "Bearer " + token)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if key is not None:
        request.add_header("Idempotency-Key", key)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler())
    try:
        response = opener.open(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        content = response.read(_BODY_LIMIT + 1)
        if len(content) > _BODY_LIMIT:
            raise ResumeServiceEvaluationError("HTTP response exceeded the evaluation budget")
        return response.status, {k.lower(): v for k, v in response.headers.items()}, content


def _docx_ok(content: bytes, expected_hash: str, expected_text: str | None = None) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            xml = archive.read("word/document.xml")
        tree = ET.fromstring(xml)
        paragraphs = [
            "".join(p.itertext())
            for p in tree.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p")
        ]
        return hashlib.sha256(content).hexdigest() == expected_hash and (
            expected_text is None or expected_text in paragraphs
        )
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        return False


def _empty_database(dsn: str) -> bool:
    with psycopg.connect(dsn, connect_timeout=3) as connection:
        row = connection.execute("""SELECT count(*) FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
              AND c.relkind IN ('r','p','v','m','S','f')""").fetchone()
        return row is not None and row[0] == 0


def _rows(dsn: str) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=3) as connection:
        return list(
            connection.execute("""SELECT id::text, state, mode, attempt_count,
            model_call_attempted, output_sha256, failure_code, created_at, started_at,
            completed_at, lease_expires_at, updated_at,
            resume_docx IS NULL AS input_cleared, output_docx IS NULL AS output_absent,
            clock_timestamp() AS observed_at
            FROM resume_cloud_tasks ORDER BY created_at""").fetchall()
        )


def _active_claim(row: dict[str, Any]) -> bool:
    return (
        row["state"] == "RUNNING"
        and row["lease_expires_at"] > row["observed_at"]
        and row["attempt_count"] == 1
        and not row["model_call_attempted"]
        and row["output_absent"]
    )


def _source_provenance() -> dict[str, Any]:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=_REPO, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=_REPO, text=True)
    return {
        "head": head,
        "dirty": bool(dirty),
        "hashes": {
            path: hashlib.sha256((_REPO / path).read_bytes()).hexdigest() for path in _SOURCE_PATHS
        },
    }


def _harness_worker_main(marker: Path, lease_seconds: int) -> None:
    from soloscale import resume_cloud_service, resume_cloud_worker

    original_settings = resume_cloud_service.CloudSettings.from_environment
    original_tailor = resume_cloud_service.tailor_resume_docx

    def settings(*_: Any, **__: Any) -> Any:
        return dataclasses.replace(
            original_settings(require_bearer=False), lease_seconds=lease_seconds
        )

    def paused_tailor(*args: Any, **kwargs: Any) -> Any:
        # process_one already committed its actual repository claim at this boundary.
        _write_json(marker, {"pid": os.getpid(), "boundary": "after_claim_before_generation"})
        time.sleep(60)
        return original_tailor(*args, **kwargs)

    resume_cloud_service.CloudSettings.from_environment = settings
    resume_cloud_service.tailor_resume_docx = paused_tailor
    try:
        sys.argv = ["resume_cloud_worker.py", "--once"]
        resume_cloud_worker.main()
    finally:
        resume_cloud_service.CloudSettings.from_environment = original_settings
        resume_cloud_service.tailor_resume_docx = original_tailor


def _safe_marker(marker: Path) -> bool:
    output = _REPO / ".soloscale" / "resume-service-evals"
    return (
        marker.is_absolute()
        and marker.name == "post-claim-marker.json"
        and marker.parent.parent == output
        and re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}", marker.parent.name) is not None
        and not any(path.is_symlink() for path in (output.parent, output, marker.parent, marker))
        and marker.parent.is_dir()
        and (marker.parent / "report.json").is_file()
        and not marker.exists()
    )


def _stop(process: subprocess.Popen[bytes], *, kill: bool = False) -> int:
    if process.poll() is None:
        process.kill() if kill else process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    return process.returncode


def _wait(predicate: Any, deadline: float, description: str) -> Any:
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise ResumeServiceEvaluationError(description + " timed out")


def _run(dsn: str, count: int, lease: int, timeout: int, run: Path) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "status": "INCOMPLETE",
        "passed": False,
        "started_utc": datetime.now(UTC).isoformat(),
        "source": _source_provenance(),
        "scope": "synthetic template-only local HTTP/multi-process development proof",
        "task_count": count,
        "worker_count": 2,
        "model_calls": None,
        "harness_injections": {
            "lease_seconds": lease,
            "pause": "child-only wrapper after actual claim, before template generation",
            "database_timestamps_modified": False,
            "production_source_modified": False,
        },
        "not_evaluated": [
            "production throughput",
            "P95",
            "HA",
            "model semantic quality",
            "paid-call recovery",
            "AWS deployment",
            "database backup restoration",
        ],
        "processes": [],
        "checks": {},
    }
    environment = {
        **os.environ,
        "RESUME_CLOUD_DATABASE_URL": dsn,
        "RESUME_CLOUD_BEARER_TOKEN": secrets.token_hex(32),
        "RESUME_CLOUD_MODEL_PROVIDER": "",
        "RESUME_CLOUD_MODEL": "",
    }
    token = environment["RESUME_CLOUD_BEARER_TOKEN"]
    processes: list[subprocess.Popen[bytes]] = []
    handles: list[Any] = []
    deadline = time.monotonic() + timeout

    def save() -> None:
        _write_json(run / "report.json", report)

    def spawn(label: str, arguments: list[str]) -> subprocess.Popen[bytes]:
        handle = (run / (label + ".log")).open("xb")
        (run / (label + ".log")).chmod(0o600)
        handles.append(handle)
        process = subprocess.Popen(
            [sys.executable, *arguments],
            cwd=_REPO,
            env=environment,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        processes.append(process)
        report["processes"].append(
            {"label": label, "pid": process.pid, "arguments": arguments, "exit_code": None}
        )
        return process

    try:
        save()
        with (run / "migration.log").open("xb") as migration_log:
            (run / "migration.log").chmod(0o600)
            completed = subprocess.run(
                [sys.executable, "scripts/migrate_resume_cloud.py"],
                cwd=_REPO,
                env=environment,
                stdout=migration_log,
                stderr=subprocess.STDOUT,
                timeout=20,
                check=False,
            )
        if completed.returncode != 0:
            raise ResumeServiceEvaluationError("dedicated schema initialization failed")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        api = f"http://127.0.0.1:{port}"
        api_process = spawn(
            "api",
            [
                "-m",
                "uvicorn",
                "soloscale.resume_cloud_api:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-access-log",
            ],
        )

        def ready() -> bool:
            if api_process.poll() is not None:
                raise ResumeServiceEvaluationError("API process exited during startup")
            try:
                return _request(api, token, "GET", "/health/ready")[0] == 200
            except OSError:
                return False

        _wait(ready, min(deadline, time.monotonic() + 20), "API readiness")
        report["checks"]["authenticated_readiness"] = True
        report["checks"]["unauthorized_readiness_rejected"] = (
            _request(api, "not-authorized", "GET", "/health/ready")[0] == 401
        )
        ids: list[str] = []
        for index in range(count):
            key = "synthetic-" + uuid.uuid4().hex
            status, _, body = _request(api, token, "POST", "/tasks", _payload(index), key)
            if status != 201:
                raise ResumeServiceEvaluationError("synthetic HTTP creation rejected")
            task_id = json.loads(body)["id"]
            ids.append(task_id)
            if index == 0:
                replay = _request(api, token, "POST", "/tasks", _payload(index), key)
                report["checks"]["idempotent_replay"] = (
                    replay[0] == 201 and json.loads(replay[2])["id"] == task_id
                )
                report["checks"]["conflicting_replay_rejected"] = (
                    _request(api, token, "POST", "/tasks", _payload(999), key)[0] == 409
                )
            if _request(api, token, "POST", f"/tasks/{task_id}/process")[0] != 200:
                raise ResumeServiceEvaluationError("synthetic queue request rejected")
        start = time.monotonic()
        workers = [
            spawn(f"worker-{i}", ["-m", "soloscale.resume_cloud_worker", "--poll-seconds", "0.1"])
            for i in range(2)
        ]

        def batch_done() -> list[dict[str, Any]] | None:
            if any(worker.poll() is not None for worker in workers):
                raise ResumeServiceEvaluationError("batch worker exited unexpectedly")
            rows = _rows(dsn)
            return (
                rows
                if len(rows) == count and all(row["state"] == "SUCCEEDED" for row in rows)
                else None
            )

        batch_rows = _wait(batch_done, deadline, "batch completion")
        elapsed = time.monotonic() - start
        for worker in workers:
            _stop(worker)
        downloads: list[dict[str, Any]] = []
        for index, task_id in enumerate(ids):
            status, headers, content = _request(api, token, "GET", f"/tasks/{task_id}/download")
            valid = status == 200 and _docx_ok(
                content,
                headers.get("x-soloscale-output-sha256", ""),
                f"Synthetic Candidate {index}",
            )
            downloads.append(
                {
                    "task_id": task_id,
                    "http_status": status,
                    "docx_and_hash_and_identity_valid": valid,
                    "bytes": len(content),
                }
            )
        latencies = [
            (row["completed_at"] - row["created_at"]).total_seconds() for row in batch_rows
        ]
        participation = []
        for index, worker in enumerate(workers):
            log = (run / f"worker-{index}.log").read_text()
            participation.append(
                {
                    "pid": worker.pid,
                    "completed_task_ids": re.findall(r"resume task completed id=([a-f0-9-]+)", log),
                }
            )
        reported_ids = [task for item in participation for task in item["completed_task_ids"]]
        report["batch"] = {
            "rows": batch_rows,
            "downloads": downloads,
            "workers": participation,
            "worker_start_to_all_success_seconds": elapsed,
            "synthetic_tasks_per_second": count / elapsed,
            "created_to_completed_seconds": latencies,
        }
        report["checks"].update(
            {
                "all_batch_outputs_valid": all(
                    item["docx_and_hash_and_identity_valid"] for item in downloads
                ),
                "single_attempt_each": all(row["attempt_count"] == 1 for row in batch_rows),
                "both_worker_processes_used": all(
                    item["completed_task_ids"] for item in participation
                ),
                "unique_logged_completions": len(reported_ids) == count
                and set(reported_ids) == set(ids),
            }
        )
        save()
        _, _, body = _request(api, token, "POST", "/tasks", _payload(1000), uuid.uuid4().hex)
        interrupted_id = json.loads(body)["id"]
        _request(api, token, "POST", f"/tasks/{interrupted_id}/process")
        marker = run / "post-claim-marker.json"
        fault_worker = spawn(
            "interrupted-worker",
            [
                str(Path(__file__).resolve()),
                "--harness-worker",
                "--claimed-marker",
                str(marker),
                "--lease-seconds",
                str(lease),
            ],
        )
        _wait(marker.exists, min(deadline, time.monotonic() + 20), "post-claim marker")
        before = next(row for row in _rows(dsn) if row["id"] == interrupted_id)
        marker_record = json.loads(marker.read_text())
        if not _active_claim(before) or marker_record["pid"] != fault_worker.pid:
            raise ResumeServiceEvaluationError("post-claim observation mismatch")
        interrupted_exit = _stop(fault_worker, kill=True)
        stopped_at = time.monotonic()
        killed = next(row for row in _rows(dsn) if row["id"] == interrupted_id)
        report["checks"]["killed_before_lease_expiry"] = (
            _active_claim(killed) and interrupted_exit != 0
        )
        replacement = spawn(
            "replacement-worker", ["-m", "soloscale.resume_cloud_worker", "--poll-seconds", "0.1"]
        )

        def recovered() -> dict[str, Any] | None:
            if replacement.poll() is not None:
                raise ResumeServiceEvaluationError("replacement worker exited unexpectedly")
            row = next(item for item in _rows(dsn) if item["id"] == interrupted_id)
            return row if row["state"] == "NEEDS_REVIEW" else None

        after = _wait(recovered, deadline, "natural lease recovery")
        report["recovery"] = {
            "before": before,
            "after_kill": killed,
            "after": after,
            "marker": marker_record,
            "interrupted_pid": fault_worker.pid,
            "interrupted_exit": interrupted_exit,
            "replacement_pid": replacement.pid,
            "kill_to_recovery_seconds": time.monotonic() - stopped_at,
        }
        report["checks"]["interrupted_task_needs_review"] = (
            after["attempt_count"] == 1
            and after["failure_code"] == "LEASE_EXPIRED"
            and after["input_cleared"]
            and after["output_absent"]
            and after["updated_at"] >= after["lease_expires_at"]
        )
        _, _, body = _request(api, token, "POST", "/tasks", _payload(1001), uuid.uuid4().hex)
        fresh_id = json.loads(body)["id"]
        _request(api, token, "POST", f"/tasks/{fresh_id}/process")

        def continued() -> dict[str, Any] | None:
            row = next(item for item in _rows(dsn) if item["id"] == fresh_id)
            return row if row["state"] == "SUCCEEDED" else None

        fresh = _wait(continued, deadline, "post-restart continuation")
        status, headers, content = _request(api, token, "GET", f"/tasks/{fresh_id}/download")
        report["recovery"]["new_task"] = fresh
        report["checks"]["replacement_completed_new_task"] = (
            status == 200
            and fresh["attempt_count"] == 1
            and _docx_ok(
                content, headers.get("x-soloscale-output-sha256", ""), "Synthetic Candidate 1001"
            )
        )
        _, _, body = _request(api, token, "POST", f"/tasks/{interrupted_id}/process")
        final_rows = _rows(dsn)
        final_interrupted = next(row for row in final_rows if row["id"] == interrupted_id)
        report["checks"]["interrupted_task_not_requeued"] = (
            json.loads(body)["state"] == "NEEDS_REVIEW"
            and final_interrupted["state"] == "NEEDS_REVIEW"
            and final_interrupted["attempt_count"] == 1
        )
        report["final_rows"] = final_rows
        report["model_calls"] = sum(int(row["model_call_attempted"]) for row in final_rows)
        report["checks"]["zero_model_calls"] = report["model_calls"] == 0
        report["checks"]["expected_total_task_count"] = len(final_rows) == count + 2
        report["passed"] = all(report["checks"].values())
        report["status"] = "PASS" if report["passed"] else "FAIL"
    except Exception as error:
        report["status"] = "FAIL"
        report["error_type"] = type(error).__name__
        report["error"] = "Evaluation failed; inspect private process logs."
    finally:
        for process, record in zip(processes, report["processes"], strict=True):
            record["exit_code"] = _stop(process)
            record["reaped"] = process.poll() is not None
        for handle in handles:
            handle.close()
        report["finished_utc"] = datetime.now(UTC).isoformat()
        save()
        markdown = (
            f"# Local Resume service evaluation\n\nStatus: {report['status']}\n\n"
            f"Synthetic tasks: {count}; batch workers: 2; model calls: {report['model_calls']}\n\n"
            "This small template-only experiment does not establish production scalability, "
            "paid-model behavior, P95, HA, or AWS deployment.\n\n"
            + "\n".join(f"- {key}: {value}" for key, value in report["checks"].items())
            + "\n"
        )
        (run / "report.md").write_text(markdown)
        (run / "report.md").chmod(0o600)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approve-disposable-database", action="store_true")
    parser.add_argument("--task-count", type=int, default=20)
    parser.add_argument("--lease-seconds", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=int, default=90)
    parser.add_argument("--harness-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--claimed-marker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    dsn = os.environ.get("RESUME_CLOUD_DATABASE_URL", "")
    if not _safe_loopback_postgres(dsn):
        print("Requires a disposable loopback resume_service_eval database.", file=sys.stderr)
        return 2
    if not (
        3 <= args.lease_seconds <= 10
        and 2 <= args.task_count <= 50
        and 20 <= args.timeout_seconds <= 180
    ):
        print("Evaluation budget is outside the allowed bounds.", file=sys.stderr)
        return 2
    if args.harness_worker:
        if args.claimed_marker is None or not _safe_marker(args.claimed_marker):
            return 2
        _harness_worker_main(args.claimed_marker, args.lease_seconds)
        return 0
    if not args.approve_disposable_database:
        print("Explicit disposable database approval is required.", file=sys.stderr)
        return 2
    try:
        if not _empty_database(dsn):
            raise ResumeServiceEvaluationError("database is not empty")
        root = _REPO / ".soloscale"
        _private_directory(root)
        _private_directory(root / "resume-service-evals")
        run = (
            root
            / "resume-service-evals"
            / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12])
        )
        run.mkdir(mode=0o700)
        report = _run(dsn, args.task_count, args.lease_seconds, args.timeout_seconds, run)
    except Exception:
        print("Preflight failed safely; no evaluation PASS recorded.", file=sys.stderr)
        return 2
    print(run)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
