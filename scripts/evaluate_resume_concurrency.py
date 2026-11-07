#!/usr/bin/env python3
"""Real local PostgreSQL/HTTP/process evidence with a synthetic ModelGateway.

Only a fresh loopback resume_service_eval database is accepted. Start PostgreSQL
with compose.resume-cloud.yaml plus compose.resume-cloud-eval.yaml; this command
owns only its API/worker children.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import platform
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel

from soloscale.evidence_agent import _NoRedirectHandler
from soloscale.model_gateway import (
    GatewayConfigurationState,
    GatewayDescriptor,
    GatewayErrorCategory,
    GatewayFailureDetails,
    GatewayTransportScope,
    ModelCallProfile,
    ModelGatewayTimeoutError,
    ModelProviderId,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".soloscale" / "resume-concurrency-evals"
TERMINAL = {"SUCCEEDED", "FAILED", "NEEDS_REVIEW"}
ResponseT = TypeVar("ResponseT", bound=BaseModel)


class EvaluationError(RuntimeError):
    """Messages are fixed diagnostics, never connection or request content."""


def safe_loopback_dsn(dsn: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(dsn)
        return (
            parsed.scheme in {"postgres", "postgresql"}
            and parsed.hostname in {"127.0.0.1", "::1"}
            and (parsed.port is None or 1 <= parsed.port <= 65535)
            and not parsed.query
            and not parsed.fragment
            and re.fullmatch(r"/resume_service_eval(?:_[A-Za-z0-9]+)?", parsed.path) is not None
        )
    except ValueError:
        return False


def empty_database(dsn: str) -> bool:
    with psycopg.connect(dsn, connect_timeout=3) as connection:
        row = connection.execute("""SELECT count(*) FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
              AND c.relkind IN ('r','p','v','m','S','f')""").fetchone()
        return row is not None and row[0] == 0


def write_json(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        temporary.chmod(0o600)
        json.dump(value, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
    temporary.replace(path)


def payload(index: int) -> dict[str, object]:
    text = f"""Synthetic Candidate {index}
AI Engineer
PROJECT HIGHLIGHTS
Synthetic project {index}
- Built a Python retrieval system.
TECHNICAL SKILLS
- Python, Docker
WORK EXPERIENCE
Synthetic company
- Delivered bounded software.
"""
    return {
        "resume": {
            "filename": "synthetic.txt",
            "content_base64": base64.b64encode(text.encode()).decode(),
        },
        "job_description": f"Python and Docker engineering role. Synthetic role {index}.",
        "mode": "ai",
        "approve_external_model_call": True,
    }


class FakeGateway:
    """The real pipeline supplies and validates its own dynamically typed schema."""

    last_call_profile: ModelCallProfile | None = None

    def __init__(
        self, audit: Path, delay: float, error_rate: float, seed: int, control: Path | None = None
    ) -> None:
        self.descriptor = GatewayDescriptor(
            provider=ModelProviderId.OPENAI_COMPATIBLE,
            display_name="Synthetic evaluation",
            configuration_state=GatewayConfigurationState.CONFIGURED,
            transport_scope=GatewayTransportScope.EXTERNAL,
            model="fake-v1",
            base_url="https://example.test/v1",
        )
        self.audit, self.delay, self.error_rate, self.seed = audit, delay, error_rate, seed
        self.control = control
        self.lock = threading.Lock()
        self.calls = 0

    def complete(
        self, schema: type[ResponseT], *, system: str, user: str, reasoning_effort: str = "none"
    ) -> ResponseT:
        del system, reasoning_effort
        started = time.monotonic_ns()
        category: str | None = None
        with self.lock:
            self.calls += 1
            ordinal = self.calls
        phase = "batch"
        try:
            if self.control is not None:
                phase = str(json.loads(self.control.read_text())["phase"])
            request = json.loads(user)
            candidate = request["candidate_profile"]
            # Stable across worker scheduling and random payload request IDs.
            digest = hashlib.sha256(f"{self.seed}:{request['job_description']}".encode()).digest()
            fails = phase == "batch" and int.from_bytes(digest[:8], "big") / 2**64 < self.error_rate
            time.sleep(self.delay)
            if fails:
                category = GatewayErrorCategory.TIMEOUT.value
                raise ModelGatewayTimeoutError(
                    "synthetic timeout",
                    details=GatewayFailureDetails(
                        correlation_id="gateway-" + uuid.uuid4().hex[:24],
                        model="fake-v1",
                        category=GatewayErrorCategory.TIMEOUT,
                        retryable=True,
                        duration_ms=int((time.monotonic_ns() - started) / 1_000_000),
                    ),
                )
            entries = candidate["entries"]
            ids = [entry["profile_entry_id"] for entry in entries]
            facts: dict[str, str] = {}
            for fact in candidate["atomic_facts"]:
                facts.setdefault(fact["profile_entry_id"], fact["fact_id"])
            wanted = request.get("requested_edit_profile_entry_ids", ids)
            return schema.model_validate(
                {
                    "role_summary": "Preserve approved engineering facts.",
                    "evidence_priority": ids,
                    "skill_priority": [
                        f"SKILL-{i:02d}" for i in range(1, len(candidate["skills"]) + 1)
                    ],
                    "bullet_rewrites": {
                        entry["profile_entry_id"]: {
                            "kind": "REWRITE",
                            "text": entry["text"],
                            "target_fact_id": facts[entry["profile_entry_id"]],
                            "supporting_fact_ids": [],
                        }
                        for entry in entries
                        if entry["profile_entry_id"] in wanted
                    },
                    "summary_rewrite": None,
                    "unsupported_requirements": [],
                    "rewrite_guidance": "Preserve approved facts.",
                }
            )
        except Exception:
            category = category or "fake_contract_error"
            raise
        finally:
            ended = time.monotonic_ns()
            with self.lock:
                with self.audit.open("a", encoding="utf-8") as handle:
                    self.audit.chmod(0o600)
                    handle.write(
                        json.dumps(
                            {
                                "ordinal": ordinal,
                                "pid": os.getpid(),
                                "phase": phase,
                                "start_ns": started,
                                "end_ns": ended,
                                "duration_seconds": (ended - started) / 1e9,
                                "error_category": category,
                            }
                        )
                        + "\n"
                    )


@dataclass(frozen=True)
class Reply:
    status: int
    task_id: str | None
    retry_after: str | None
    elapsed: float


def request(
    url: str,
    token: str,
    method: str,
    path: str,
    body: object | None = None,
    key: str | None = None,
    client: str | None = None,
) -> Reply:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username
        or parsed.password
    ):
        raise EvaluationError("HTTP target must be a loopback origin")
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if key is not None:
        req.add_header("Idempotency-Key", key)
    if client is not None:
        req.add_header("X-Client-ID", client)
    started = time.monotonic()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler())
    try:
        response = opener.open(req, timeout=20)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        content = response.read(1_000_001)
        if len(content) > 1_000_000:
            raise EvaluationError("HTTP response exceeded budget")
        decoded = json.loads(content or b"{}")
        return Reply(
            response.status,
            decoded.get("id"),
            response.headers.get("Retry-After"),
            time.monotonic() - started,
        )


def wait_until(predicate: Callable[[], bool], timeout: float, category: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.025)
    raise EvaluationError(category)


def worker_child(args: argparse.Namespace) -> None:
    from soloscale.resume_cloud_observability import Telemetry
    from soloscale.resume_cloud_repository import ResumeCloudRepository
    from soloscale.resume_cloud_service import CloudSettings, ResumeCloudService

    run_dir = Path(args.run_dir)
    service = ResumeCloudService(
        ResumeCloudRepository(args.dsn),
        CloudSettings(
            database_url=args.dsn,
            bearer_token="x" * 32,
            max_model_concurrency=args.threads,
        ),
        telemetry=Telemetry(),
    )
    fake = FakeGateway(
        run_dir / f"calls-{args.worker_index}.jsonl",
        args.delay,
        args.error_rate,
        args.seed,
        run_dir / "control.json",
    )
    (run_dir / f"ready-{args.worker_index}").touch(mode=0o600)
    wait_until(lambda: (run_dir / "start").exists(), args.timeout, "worker_start_timeout")

    def consume(_: int) -> int:
        count = 0
        while not (run_dir / "stop").exists():
            if service.process_one(fake) is None:
                time.sleep(0.01)
            else:
                count += 1
        return count

    try:
        with ThreadPoolExecutor(max_workers=args.threads) as pool:
            processed = sum(pool.map(consume, range(args.threads)))
        metrics = getattr(service.telemetry, "metrics", None)
        write_json(
            run_dir / f"worker-{args.worker_index}.json",
            {
                "pid": os.getpid(),
                "processed": processed,
                "metrics": metrics.snapshot() if metrics is not None else None,
            },
        )
    finally:
        service.telemetry.shutdown()


def api_child(args: argparse.Namespace) -> None:
    import uvicorn

    from soloscale.resume_cloud_api import create_app
    from soloscale.resume_cloud_observability import Telemetry
    from soloscale.resume_cloud_service import CloudSettings

    uvicorn.run(
        create_app(
            CloudSettings(
                database_url=args.dsn,
                bearer_token=os.environ["RESUME_CLOUD_BEARER_TOKEN"],
                rate_limit_capacity=args.capacity,
                rate_limit_refill_per_second=args.refill,
            ),
            telemetry=Telemetry(),
        ),
        host="127.0.0.1",
        port=args.port,
        access_log=False,
        log_level="warning",
    )


def source() -> dict[str, object]:
    paths = sorted(
        {
            Path(__file__),
            *ROOT.glob("src/soloscale/resume_*.py"),
            ROOT / "src/soloscale/model_gateway.py",
            *ROOT.glob("migrations/resume_cloud/*.sql"),
        }
    )
    return {
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)),
        "hashes": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths
        },
        "python": platform.python_version(),
        "platform": platform.platform(),
    }


def read_calls(run_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for path in sorted(run_dir.glob("calls-*.jsonl"))
        for line in path.read_text().splitlines()
        if line
    ]


def peak(calls: list[dict[str, Any]]) -> int:
    events = [(call["start_ns"], 1) for call in calls] + [(call["end_ns"], -1) for call in calls]
    active = maximum = 0
    for _, delta in sorted(events):  # End before start for equal timestamps: half-open intervals.
        active += delta
        maximum = max(maximum, active)
    return maximum


def distribution(values: list[float]) -> dict[str, float | int | None]:
    ordered = sorted(values)

    def quantile(q: float) -> float | None:
        if not ordered:
            return None
        position = (len(ordered) - 1) * q
        lower = math.floor(position)
        return ordered[lower] + (ordered[math.ceil(position)] - ordered[lower]) * (position - lower)

    return {
        "count": len(values),
        "p50": quantile(0.5),
        "p95": quantile(0.95),
        "max": max(values, default=None),
    }


def rows(dsn: str) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        return list(
            connection.execute("""SELECT id::text, state, attempt_count, model_call_attempted,
            output_sha256, output_expires_at::text,
            EXTRACT(EPOCH FROM(completed_at-created_at))::float AS latency,
            EXTRACT(EPOCH FROM(started_at-
                (to_jsonb(t)->>'queued_at')::timestamptz))::float AS queue_wait
            FROM resume_cloud_tasks t""").fetchall()
        )


def install_audit(dsn: str) -> None:
    # Evaluation-only schema, after migration; production public schema stays exact.
    with psycopg.connect(dsn) as connection:
        connection.execute("""CREATE SCHEMA evaluation;
            CREATE TABLE evaluation.terminal_events(
                task_id UUID, state TEXT, observed_at TIMESTAMPTZ DEFAULT clock_timestamp());
            CREATE FUNCTION evaluation.audit_terminal() RETURNS TRIGGER LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.state IN ('SUCCEEDED','FAILED','NEEDS_REVIEW') AND
                   (OLD.state IS DISTINCT FROM NEW.state OR
                    OLD.completed_at IS DISTINCT FROM NEW.completed_at) THEN
                    INSERT INTO evaluation.terminal_events(task_id,state) VALUES(NEW.id,NEW.state);
                END IF;
                RETURN NEW;
            END $$;
            CREATE TRIGGER evaluation_terminal AFTER UPDATE ON resume_cloud_tasks
            FOR EACH ROW EXECUTE FUNCTION evaluation.audit_terminal();""")


def render_report(report: dict[str, Any]) -> str:
    lines = [
        "# Resume Cloud concurrency evaluation",
        "",
        f"Status: **{report['status']}**",
        "",
        (
            "Measured local synthetic workload; no paid model, provider HTTP/TLS, "
            "or production capacity claim."
        ),
        "",
        f"Source: `{report['source']['head']}`; dirty: `{report['source']['dirty']}`.",
        "",
        "| Measurement | Observed value |",
        "|---|---|",
    ]
    for section in ("config", "requests", "tasks", "model", "cache", "metrics"):
        for name, value in report.get(section, {}).items():
            lines.append(f"| {section}.{name} | `{json.dumps(value, sort_keys=True)}` |")
    lines.extend(["", "| Assertion | Result |", "|---|---|"])
    lines.extend(f"| {name} | {passed} |" for name, passed in report["assertions"].items())
    lines.extend(
        [
            "",
            "Full source hashes, runtime, timings and process evidence are in report.json.",
            "Batch failures are seeded by JD hash. Cache control calls use error rate 0.",
            "The terminal audit trigger exists only in this disposable evaluation database.",
            "",
        ]
    )
    return "\n".join(lines)


def evidence_is_private(run_dir: Path, token: str) -> bool:
    forbidden = [
        "Synthetic Candidate",
        "Built a Python retrieval system.",
        "Python and Docker engineering role.",
        "U3ludGhldGljIENh",
        token,
        os.environ.get("PGPASSWORD", ""),
    ]
    needles = [value.encode() for value in forbidden if len(value) >= 8]
    return all(
        not any(needle in path.read_bytes() for needle in needles)
        for path in run_dir.iterdir()
        if path.is_file() and path.suffix in {".json", ".jsonl", ".log", ".md"}
    )


def run(args: argparse.Namespace) -> tuple[Path, bool]:
    if not safe_loopback_dsn(args.dsn):
        raise EvaluationError("DSN must be a loopback resume_service_eval database")
    if not empty_database(args.dsn):
        raise EvaluationError("refusing non-empty evaluation database")
    for directory in (ROOT / ".soloscale", OUT):
        if directory.is_symlink():
            raise EvaluationError("unsafe output directory")
        directory.mkdir(mode=0o700, exist_ok=True)
    run_dir = OUT / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12])
    run_dir.mkdir(mode=0o700)
    token = secrets.token_hex(24)
    report: dict[str, Any] = {
        "status": "INCOMPLETE",
        "source": source(),
        "started_at": datetime.now(UTC).isoformat(),
        "config": {
            key: getattr(args, key)
            for key in (
                "tasks",
                "clients",
                "workers",
                "threads",
                "delay",
                "error_rate",
                "seed",
                "capacity",
                "refill",
            )
        },
        "assertions": {},
    }
    write_json(run_dir / "report.json", report)
    write_json(run_dir / "control.json", {"phase": "batch"})
    env = dict(
        os.environ,
        RESUME_CLOUD_DATABASE_URL=args.dsn,
        RESUME_CLOUD_BEARER_TOKEN=token,
        RESUME_CLOUD_TRACING="false",
        RESUME_CLOUD_METRICS_EXPORTER="none",
        RESUME_CLOUD_MODEL_PROVIDER="",
        RESUME_CLOUD_MODEL="",
        RESUME_CLOUD_OPENAI_API_KEY="",
        RESUME_CLOUD_DEEPSEEK_API_KEY="",
    )
    processes: list[subprocess.Popen[bytes]] = []
    workers: list[subprocess.Popen[bytes]] = []
    started = time.monotonic()
    try:
        subprocess.run(
            [sys.executable, "scripts/migrate_resume_cloud.py"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        install_audit(args.dsn)
        urls: list[str] = []
        for _ in range(2):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            urls.append(f"http://127.0.0.1:{port}")
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__)),
                        "--child",
                        "api",
                        "--port",
                        str(port),
                        "--capacity",
                        str(args.capacity),
                        "--refill",
                        str(args.refill),
                    ],
                    cwd=ROOT,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )

        report["api_processes"] = [
            {"pid": process.pid, "url": url} for process, url in zip(processes, urls, strict=True)
        ]

        def ready() -> bool:
            try:
                return all(
                    request(url, token, "GET", "/health/ready").status == 200 for url in urls
                )
            except OSError:
                return False

        wait_until(ready, args.timeout, "api_readiness_timeout")
        responses: list[Reply] = []
        specs = [
            (payload(i), f"task-{i}-{uuid.uuid4().hex}", f"client-{i % args.clients}")
            for i in range(args.tasks)
        ]
        jobs = [(index, retry) for index in range(args.tasks) for retry in range(3)]

        def submit(job: tuple[int, int]) -> Reply:
            index, retry = job
            body, key, client = specs[index]
            return request(urls[(index + retry) % 2], token, "POST", "/tasks", body, key, client)

        with ThreadPoolExecutor(max_workers=16) as pool:
            responses = list(pool.map(submit, jobs))
        ids: set[str] = {reply.task_id for reply in responses if reply.task_id is not None}
        observed_replays = sum(reply.status == 201 for reply in responses) - len(ids)
        checks = report["assertions"]
        checks["concurrent_replays_one_task_per_key"] = (
            all(
                all(reply.status == 201 for reply in responses[i * 3 : i * 3 + 3])
                and len({reply.task_id for reply in responses[i * 3 : i * 3 + 3]}) == 1
                for i in range(args.tasks)
            )
            and len(ids) == args.tasks
        )
        conflict = request(
            urls[1], token, "POST", "/tasks", payload(99999), specs[0][1], specs[0][2]
        )
        checks["different_payload_conflicts"] = conflict.status == 409
        burst_started = time.monotonic()

        def burst_submit(index: int) -> Reply:
            return request(
                urls[index % 2],
                token,
                "POST",
                "/tasks",
                payload(10000 + index),
                f"burst-{index}-{uuid.uuid4().hex}",
                "burst",
            )

        with ThreadPoolExecutor(max_workers=16) as pool:
            burst = list(pool.map(burst_submit, range(args.capacity + 8)))
        burst_seconds = time.monotonic() - burst_started
        ids.update(reply.task_id for reply in burst if reply.task_id is not None)
        checks["rate_limit_and_retry_after"] = any(reply.status == 429 for reply in burst) and all(
            reply.retry_after is not None
            and reply.retry_after.isdigit()
            and int(reply.retry_after) >= 1
            for reply in burst
            if reply.status == 429
        )
        checks["shared_api_bucket"] = sum(
            reply.status == 201 for reply in burst
        ) <= args.capacity + math.floor(burst_seconds * args.refill)
        checks["only_expected_http_statuses"] = all(reply.status in {201, 429} for reply in burst)
        responses += burst + [conflict]
        with ThreadPoolExecutor(max_workers=16) as pool:
            queued = list(
                pool.map(
                    lambda task_id: request(urls[0], token, "POST", f"/tasks/{task_id}/process"),
                    ids,
                )
            )
        checks["all_accepted_tasks_queued"] = all(reply.status == 200 for reply in queued)
        for index in range(args.workers):
            worker = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__)),
                    "--child",
                    "worker",
                    "--worker-index",
                    str(index),
                    "--run-dir",
                    str(run_dir),
                    "--threads",
                    str(args.threads),
                    "--delay",
                    str(args.delay),
                    "--error-rate",
                    str(args.error_rate),
                    "--seed",
                    str(args.seed),
                    "--timeout",
                    str(args.timeout),
                ],
                cwd=ROOT,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            workers.append(worker)
            processes.append(worker)
        wait_until(
            lambda: all((run_dir / f"ready-{i}").exists() for i in range(args.workers)),
            args.timeout,
            "worker_readiness_timeout",
        )
        (run_dir / "start").touch(mode=0o600)
        wait_until(
            lambda: all(row["state"] in TERMINAL for row in rows(args.dsn)),
            args.timeout,
            "batch_terminal_timeout",
        )
        batch_rows = rows(args.dsn)
        batch_calls = read_calls(run_dir)
        successful = {row["id"] for row in batch_rows if row["state"] == "SUCCEEDED"}
        candidates = [
            (i, responses[i * 3].task_id)
            for i in range(args.tasks)
            if responses[i * 3].task_id in successful
        ]
        if not candidates:
            raise EvaluationError("no_successful_cache_source")
        source_index, source_id = candidates[0]
        original = next(row for row in batch_rows if row["id"] == source_id)
        write_json(run_dir / "control.json", {"phase": "cache_control"})
        body, _, client = specs[source_index]

        def cache_task(partition: str) -> dict[str, Any]:
            reply = request(urls[1], token, "POST", "/tasks", body, uuid.uuid4().hex, partition)
            responses.append(reply)
            if reply.status != 201 or reply.task_id is None:
                raise EvaluationError("cache_task_not_created")
            request(urls[1], token, "POST", f"/tasks/{reply.task_id}/process")
            wait_until(
                lambda: any(
                    row["id"] == reply.task_id and row["state"] in TERMINAL
                    for row in rows(args.dsn)
                ),
                args.timeout,
                "cache_terminal_timeout",
            )
            return next(row for row in rows(args.dsn) if row["id"] == reply.task_id)

        before = len(read_calls(run_dir))
        cached = cache_task(client)
        after_hit = len(read_calls(run_dir))
        other = cache_task("cache-other-client")
        after_other = len(read_calls(run_dir))
        checks["cache_hit_zero_model_calls"] = (
            cached["state"] == "SUCCEEDED"
            and not cached["model_call_attempted"]
            and after_hit == before
        )
        checks["cache_preserves_output_and_expiry"] = (
            cached["output_sha256"] == original["output_sha256"]
            and cached["output_expires_at"] == original["output_expires_at"]
        )
        checks["cross_client_cache_miss"] = (
            other["state"] == "SUCCEEDED"
            and other["model_call_attempted"]
            and after_other - after_hit == 1
        )
        report["cache"] = {
            "same_client_call_delta": after_hit - before,
            "other_client_call_delta": after_other - after_hit,
        }
        (run_dir / "stop").touch(mode=0o600)
        exits = [worker.wait(timeout=10) for worker in workers]
        checks["workers_exit_cleanly"] = all(code == 0 for code in exits)
        final_rows = rows(args.dsn)
        calls = read_calls(run_dir)
        with psycopg.connect(args.dsn, row_factory=dict_row) as connection:
            audit_rows = list(
                connection.execute(
                    "SELECT task_id::text, state FROM evaluation.terminal_events"
                ).fetchall()
            )
            duplicates = connection.execute(
                "SELECT count(*) AS n FROM (SELECT idempotency_key FROM resume_cloud_tasks "
                "GROUP BY idempotency_key HAVING count(*)<>1) s"
            ).fetchone()
        audit_counts = Counter(row["task_id"] for row in audit_rows)
        audit_states = {row["task_id"]: row["state"] for row in audit_rows}
        checks["each_key_one_task"] = duplicates is not None and duplicates["n"] == 0
        checks["rate_denials_not_persisted"] = len(final_rows) == len(ids) + 2
        checks["terminal_transition_exactly_once"] = len(audit_rows) == len(final_rows) and all(
            audit_counts[row["id"]] == 1 and audit_states[row["id"]] == row["state"]
            for row in final_rows
        )
        checks["one_attempt_per_task"] = all(row["attempt_count"] == 1 for row in final_rows)
        checks["actual_model_calls_positive"] = len(calls) > 0
        checks["actual_calls_match_attempt_flags"] = sum(
            row["model_call_attempted"] for row in final_rows
        ) == len(calls)
        checks["batch_errors_no_retry"] = sum(
            row["state"] == "FAILED" for row in batch_rows
        ) == sum(call["error_category"] is not None for call in batch_calls)
        checks["no_fake_contract_errors"] = all(
            call["error_category"] in {None, GatewayErrorCategory.TIMEOUT.value} for call in calls
        )
        checks["requested_failure_scenario_observed"] = args.error_rate == 0 or any(
            call["error_category"] is not None for call in batch_calls
        )
        by_pid = {
            str(pid): peak([call for call in calls if call["pid"] == pid])
            for pid in {call["pid"] for call in calls}
        }
        checks["all_workers_called_model"] = len(by_pid) == args.workers
        checks["per_worker_model_peak_bounded"] = all(
            value <= args.threads for value in by_pid.values()
        )
        checks["aggregate_model_peak_bounded"] = peak(calls) <= args.threads * args.workers
        checks["both_apis_still_ready"] = ready()
        snapshots = [json.loads(path.read_text()) for path in sorted(run_dir.glob("worker-*.json"))]
        metrics = [snapshot["metrics"] for snapshot in snapshots]
        measured = {
            "queue_samples": sum(
                item["histograms"]["queue_wait_seconds"]["count"] for item in metrics
            ),
            "model_samples": sum(
                item["histograms"][f"model_duration_seconds.{outcome}"]["count"]
                for item in metrics
                for outcome in ("success", "error")
            ),
            "cache_lookups": sum(item["counters"]["cache_lookups"] for item in metrics),
            "cache_hits": sum(item["counters"]["cache_hits"] for item in metrics),
        }
        checks["metrics_match_independent_audit"] = (
            measured["queue_samples"] == len(final_rows)
            and measured["model_samples"] == len(calls)
            and measured["cache_hits"] == 1
            and measured["cache_lookups"] == len(final_rows)
            and all(
                snapshot["metrics"]["gauges"]
                == {"model_active": 0, "model_peak": by_pid[str(snapshot["pid"])]}
                for snapshot in snapshots
            )
        )
        # Deliberately exercise the duplicate detector, then roll back the probe.
        with psycopg.connect(args.dsn) as probe_connection:
            probe_connection.execute("SAVEPOINT audit_probe")
            probe_connection.execute(
                "UPDATE resume_cloud_tasks SET completed_at=completed_at+interval '1 second' "
                "WHERE id=%s",
                (original["id"],),
            )
            count = probe_connection.execute(
                "SELECT count(*) FROM evaluation.terminal_events WHERE task_id=%s",
                (original["id"],),
            ).fetchone()
            checks["audit_detects_rolled_back_duplicate_probe"] = (
                count is not None and count[0] == 2
            )
            probe_connection.execute("ROLLBACK TO SAVEPOINT audit_probe")
        report["metrics"] = measured
        report.update(
            {
                "requests": {
                    "create_requests": len(responses),
                    "statuses": dict(Counter(reply.status for reply in responses)),
                    "replays": observed_replays,
                    "burst_duration_seconds": burst_seconds,
                    "create_latency_seconds": distribution([reply.elapsed for reply in responses]),
                },
                "tasks": {
                    "total": len(final_rows),
                    "states": dict(Counter(row["state"] for row in final_rows)),
                    "terminal_audit_events": len(audit_rows),
                    "duplicate_terminal_events": sum(
                        max(0, count - 1) for count in audit_counts.values()
                    ),
                    "end_to_end_seconds": distribution(
                        [row["latency"] for row in final_rows if row["latency"] is not None]
                    ),
                    "queue_wait_seconds": distribution(
                        [row["queue_wait"] for row in final_rows if row["queue_wait"] is not None]
                    ),
                },
                "model": {
                    "calls": len(calls),
                    "batch_calls": len(batch_calls),
                    "batch_errors": sum(call["error_category"] is not None for call in batch_calls),
                    "duration_seconds": distribution([call["duration_seconds"] for call in calls]),
                    "per_worker_peak": by_pid,
                    "aggregate_peak": peak(calls),
                    "aggregate_limit": args.workers * args.threads,
                },
                "worker_snapshots": snapshots,
            }
        )
        write_json(run_dir / "terminal-audit.json", audit_rows)
        write_json(run_dir / "task-metadata.json", final_rows)
        report["status"] = "PASSED" if all(checks.values()) else "FAILED"
    except Exception as error:
        report["status"] = "FAILED"
        report["error_category"] = type(error).__name__
        if isinstance(error, EvaluationError):
            report["error_code"] = str(error)
    finally:
        (run_dir / "stop").touch(mode=0o600)
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        report["elapsed_seconds"] = time.monotonic() - started
        report["finished_at"] = datetime.now(UTC).isoformat()
        write_json(run_dir / "report.json", report)
        (run_dir / "report.md").write_text(render_report(report), encoding="utf-8")
        (run_dir / "report.md").chmod(0o600)
        report["assertions"]["evidence_contains_no_private_content"] = evidence_is_private(
            run_dir, token
        )
        if not report["assertions"]["evidence_contains_no_private_content"]:
            report["status"] = "FAILED"
        write_json(run_dir / "report.json", report)
        (run_dir / "report.md").write_text(render_report(report), encoding="utf-8")
    return run_dir / "report.md", report["status"] == "PASSED"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("RESUME_CLOUD_DATABASE_URL", ""))
    parser.add_argument("--tasks", type=int, default=24)
    parser.add_argument("--clients", type=int, default=4)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--delay", type=float, default=0.15)
    parser.add_argument("--error-rate", type=float, default=0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--capacity", type=int, default=8)
    parser.add_argument("--refill", type=float, default=0.1)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--child", choices=("api", "worker"), help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--worker-index", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--run-dir", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not (
        1 <= args.tasks <= 2000
        and 1 <= args.clients <= 100
        and 2 <= args.workers <= 8
        and 1 <= args.threads <= 16
        and 0 <= args.error_rate < 1
        and math.isfinite(args.delay)
        and 0 <= args.delay <= 10
        and math.isfinite(args.refill)
        and args.refill > 0
        and args.capacity >= 1
        and 0 < args.timeout <= 3600
    ):
        parser.error("invalid bounded evaluation settings")
    try:
        if args.child == "api":
            api_child(args)
        elif args.child == "worker":
            worker_child(args)
        else:
            path, passed = run(args)
            print(path)
            return 0 if passed else 1
    except Exception as error:
        print(f"evaluation failed category={type(error).__name__}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
