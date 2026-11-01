#!/usr/bin/env python3
"""Disposable, fake-gateway concurrency evaluation for Resume Cloud.

This never starts containers or touches a non-empty database.  It is deliberately a
harness: production provider selection and source are not changed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from statistics import quantiles
from typing import Any, cast
from urllib.parse import urlsplit

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel

from soloscale.model_gateway import (
    GatewayConfigurationState,
    GatewayDescriptor,
    GatewayTransportScope,
    ModelGateway,
    ModelProviderId,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".soloscale" / "resume-concurrency-evals"


class EvaluationError(ValueError):
    pass


def safe_loopback_dsn(dsn: str) -> bool:
    try:
        p = urlsplit(dsn)
        return (
            p.scheme in {"postgres", "postgresql"}
            and p.hostname in {"127.0.0.1", "::1"}
            and not p.query
            and not p.fragment
            and re.fullmatch(r"/resume_service_eval(?:_[A-Za-z0-9]+)?", p.path or "") is not None
        )
    except ValueError:
        return False


def empty_database(dsn: str) -> bool:
    with psycopg.connect(dsn, connect_timeout=3) as c:
        r = c.execute(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema' "
            "AND c.relkind IN ('r','p','v','m','S','f')"
        ).fetchone()
        return bool(r and r[0] == 0)


def private_dir(p: Path) -> None:
    if p.is_symlink() or (p.exists() and not p.is_dir()):
        raise EvaluationError("unsafe evaluation directory")
    p.mkdir(parents=True, mode=0o700, exist_ok=False)
    p.chmod(0o700)


def write_json(p: Path, value: object) -> None:
    p.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    p.chmod(0o600)


def request(
    url: str,
    token: str,
    method: str,
    path: str,
    payload: object | None = None,
    key: str | None = None,
    client: str | None = None,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    body = None if payload is None else json.dumps(payload).encode()
    r = urllib.request.Request(url + path, data=body, method=method)
    r.add_header("Authorization", "Bearer " + token)
    if body:
        r.add_header("Content-Type", "application/json")
    if key:
        r.add_header("Idempotency-Key", key)
    if client is not None:
        r.add_header("X-Client-ID", client)
    try:
        h = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(r, timeout=10)
    except urllib.error.HTTPError as e:
        h = e
    with h:
        return h.status, {k.lower(): v for k, v in h.headers.items()}, json.loads(h.read() or b"{}")


def payload(i: int) -> dict[str, object]:
    text = f"Candidate {i}\nEXPERIENCE\n- Built Python service {i}.\nSKILLS\n- Python\n"
    return {
        "resume": {
            "filename": "synthetic.txt",
            "content_base64": base64.b64encode(text.encode()).decode(),
        },
        "job_description": f"Python role {i}",
        "mode": "ai",
        "approve_external_model_call": True,
    }


class FakeGateway:
    def __init__(self, audit: Path, delay: float, error_rate: float, seed: int) -> None:
        self.descriptor = GatewayDescriptor(
            provider=ModelProviderId.OPENAI_COMPATIBLE,
            display_name="Evaluation fake",
            configuration_state=GatewayConfigurationState.CONFIGURED,
            transport_scope=GatewayTransportScope.EXTERNAL,
            model="fake",
            base_url="https://example.test/v1",
        )
        self.audit, self.delay, self.error_rate, self.seed = audit, delay, error_rate, seed
        self.lock = threading.Lock()
        self.calls = 0
        self.last_call_profile = None

    def complete(
        self, schema: type[BaseModel], *, system: str, user: str, reasoning_effort: str = "none"
    ) -> BaseModel:
        p = json.loads(user)
        entries = p["candidate_profile"]
        ids = [x["profile_entry_id"] for x in entries]
        facts = p["atomic_facts"]
        with self.lock:
            self.calls += 1
            ordinal = self.calls
        start = time.monotonic_ns()
        bad = ((self.seed + ordinal) * 1103515245 + 12345) % 10000 < int(self.error_rate * 10000)
        time.sleep(self.delay)
        end = time.monotonic_ns()
        rec = {
            "ordinal": ordinal,
            "pid": os.getpid(),
            "start_monotonic_ns": start,
            "end_monotonic_ns": end,
            "walltime_ns": end - start,
            "errorcategory": "TIMEOUT" if bad else None,
        }
        with self.lock:
            with self.audit.open("a", encoding="utf-8") as h:
                h.write(json.dumps(rec, separators=(",", ":")) + "\n")
        if bad:
            raise RuntimeError("evaluation fake failure")
        wanted = p.get("requested_edit_profile_entry_ids", ids)
        by = {f["profile_entry_id"]: f["fact_id"] for f in facts}
        return schema.model_validate(
            {
                "role_summary": "Python engineering role.",
                "evidence_priority": ids,
                "skill_priority": [f"SKILL-{i + 1:02d}" for i in range(len(ids))],
                "bullet_rewrites": {
                    e["profile_entry_id"]: {
                        "kind": "REWRITE",
                        "text": e["text"],
                        "target_fact_id": by[e["profile_entry_id"]],
                        "supporting_fact_ids": [],
                    }
                    for e in entries
                    if e["profile_entry_id"] in wanted
                },
                "summary_rewrite": None,
                "unsupported_requirements": [],
                "rewrite_guidance": "Preserve source facts.",
            }
        )


def worker_child(args: argparse.Namespace) -> None:
    from soloscale.resume_cloud_repository import ResumeCloudRepository
    from soloscale.resume_cloud_service import CloudSettings, ResumeCloudService

    audit = Path(args.audit)
    service = ResumeCloudService(
        ResumeCloudRepository(args.dsn),
        CloudSettings(
            database_url=args.dsn, bearer_token="x" * 32, max_model_concurrency=args.threads
        ),
    )
    fake = cast(
        ModelGateway, FakeGateway(audit, args.delay, args.error_rate, args.seed + os.getpid())
    )

    def one(_: int) -> int:
        done = 0
        while service.process_one(fake) is not None:
            done += 1
        return done

    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        print(sum(pool.map(one, range(args.threads))))


def api_child(args: argparse.Namespace) -> None:
    import uvicorn

    from soloscale.resume_cloud_api import create_app
    from soloscale.resume_cloud_service import CloudSettings

    uvicorn.run(
        create_app(
            CloudSettings(
                database_url=args.dsn,
                bearer_token=args.token,
                rate_limit_capacity=args.capacity,
                rate_limit_refill_per_second=args.refill,
            )
        ),
        host="127.0.0.1",
        port=args.port,
        access_log=False,
        log_level="warning",
    )


def source() -> dict[str, object]:
    paths = (
        "scripts/evaluate_resume_concurrency.py",
        "src/soloscale/resume_cloud_service.py",
        "src/soloscale/resume_cloud_repository.py",
    )
    return {
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "dirty_hash": hashlib.sha256(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)
        ).hexdigest(),
        "hashes": {x: hashlib.sha256((ROOT / x).read_bytes()).hexdigest() for x in paths},
    }


def percentile(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    return sorted(xs)[-1] if len(xs) == 1 else quantiles(xs, n=100, method="inclusive")[p - 1]


def run(args: argparse.Namespace) -> dict[str, object]:
    if not safe_loopback_dsn(args.dsn):
        raise EvaluationError("DSN must be loopback resume_service_eval database")
    if not empty_database(args.dsn):
        raise EvaluationError("refusing non-empty evaluation database")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
    out = OUT / stamp
    OUT.mkdir(mode=0o700, exist_ok=True)
    OUT.chmod(0o700)
    private_dir(out)
    token = secrets.token_hex(24)
    audit = out / "fake-audit.jsonl"
    audit.touch(mode=0o600)
    report = {
        "status": "INCOMPLETE",
        "config": {
            "workers": args.workers,
            "threads": args.threads,
            "delay_seconds": args.delay,
            "error_rate": args.error_rate,
            "seed": args.seed,
        },
        "source": source(),
        "assertions": {},
    }
    env = {
        **os.environ,
        "RESUME_CLOUD_DATABASE_URL": args.dsn,
        "RESUME_CLOUD_BEARER_TOKEN": token,
        "RESUME_CLOUD_OPENAI_API_KEY": "",
        "RESUME_CLOUD_DEEPSEEK_API_KEY": "",
    }
    subprocess.run(
        [sys.executable, "scripts/migrate_resume_cloud.py"],
        cwd=ROOT,
        env=env,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    ports = []
    apis = []
    import socket

    try:
        for _ in range(2):
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            ports.append(port)
            apis.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--api-child",
                        "--dsn",
                        args.dsn,
                        "--token",
                        token,
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
        urls = [f"http://127.0.0.1:{p}" for p in ports]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                if all(request(u, token, "GET", "/health/ready")[0] == 200 for u in urls):
                    break
            except OSError:
                pass
            time.sleep(0.1)
        else:
            raise EvaluationError("API readiness timeout")
        statuses = []
        ids = []
        for i in range(args.tasks):
            key = f"eval-{i}-{uuid.uuid4().hex}"
            st, _, body = request(urls[i % 2], token, "POST", "/tasks", payload(i), key, "alpha")
            statuses.append(st)
            ids.append(body.get("id"))
            request(urls[i % 2], token, "POST", f"/tasks/{ids[-1]}/process")
        same = "same-" + uuid.uuid4().hex
        a = request(urls[0], token, "POST", "/tasks", payload(999), same, "beta")
        b = request(urls[1], token, "POST", "/tasks", payload(999), same, "beta")
        conflict = request(urls[0], token, "POST", "/tasks", payload(998), same, "beta")
        burst = [
            request(
                urls[i % 2],
                token,
                "POST",
                "/tasks",
                payload(2000 + i),
                "burst-" + uuid.uuid4().hex,
                "rate",
            )[0]
            for i in range(args.capacity + 2)
        ]
        report["requests"] = {
            "create_statuses": statuses,
            "same_key_ids": [a[2].get("id"), b[2].get("id")],
            "conflict_status": conflict[0],
            "rate_statuses": burst,
            "retry_after_present": any(s == 429 for s in burst),
        }
        workers = [
            subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--worker-child",
                    "--dsn",
                    args.dsn,
                    "--audit",
                    str(audit),
                    "--threads",
                    str(args.threads),
                    "--delay",
                    str(args.delay),
                    "--error-rate",
                    str(args.error_rate),
                    "--seed",
                    str(args.seed),
                ],
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            for _ in range(args.workers)
        ]
        exits = [p.wait(timeout=args.timeout) for p in workers]
        with psycopg.connect(args.dsn, row_factory=dict_row) as c:
            rows = list(
            c.execute(
                "SELECT id::text,state,attempt_count,model_call_attempted,output_sha256,"
                "output_expires_at,EXTRACT(EPOCH FROM(completed_at-created_at)) latency "
                "FROM resume_cloud_tasks"
            ).fetchall()
            )
        calls = [json.loads(x) for x in audit.read_text().splitlines() if x]
        active = {}
        peaks = {}
        for e in sorted(calls, key=lambda x: x["start_monotonic_ns"]):
            pid = e["pid"]
            active.setdefault(pid, []).append((e["start_monotonic_ns"], 1))
            active[pid].append((e["end_monotonic_ns"], -1))
        for pid, events in active.items():
            n = 0
            peaks[str(pid)] = max(
                (n := n + d for _, d in sorted(events, key=lambda x: (x[0], x[1]))), default=0
            )
        all_ok = all(
            r["state"] == ("FAILED" if args.error_rate else "SUCCEEDED") and r["attempt_count"] == 1
            for r in rows
            if r["id"] in ids
        )
        report.update(
            {
                "worker_exit_codes": exits,
                "tasks": {
                    "states": {
                        s: sum(r["state"] == s for r in rows) for s in {r["state"] for r in rows}
                    },
                    "latency_seconds": {
                        "p50": percentile(
                            [float(r["latency"]) for r in rows if r["latency"] is not None], 50
                        ),
                        "p95": percentile(
                            [float(r["latency"]) for r in rows if r["latency"] is not None], 95
                        ),
                        "max": max(
                            [float(r["latency"]) for r in rows if r["latency"] is not None],
                            default=None,
                        ),
                    },
                },
                "calls": {
                    "total": len(calls),
                    "errors": sum(x["errorcategory"] is not None for x in calls),
                    "per_worker_peak": peaks,
                    "aggregate_limit": args.workers * args.threads,
                },
                "assertions": {
                    "canonical_calls": len(calls) > 0,
                    "per_process_peak_bounded": all(x <= args.threads for x in peaks.values()),
                    "terminal_single_attempt": all_ok,
                    "idempotent_replay": a[0] == b[0] == 201 and a[2].get("id") == b[2].get("id"),
                    "replay_conflict": conflict[0] == 409,
                    "rate_limited": 429 in burst,
                    "workers_participated": len(peaks) >= min(args.workers, 2),
                },
            }
        )
        report["status"] = "PASSED" if all(report["assertions"].values()) else "FAILED"
        report["passed"] = report["status"] == "PASSED"
        write_json(out / "report.json", report)
        (out / "report.md").write_text(
            "# Resume Cloud concurrency evaluation\n\n" + json.dumps(report, indent=2) + "\n"
        )
        (out / "report.md").chmod(0o600)
        return report
    finally:
        for p in apis:
            if p.poll() is None:
                p.terminate()
                p.wait(timeout=5)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dsn")
    p.add_argument("--tasks", type=int, default=8)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--threads", type=int, default=2)
    p.add_argument("--delay", type=float, default=0.05)
    p.add_argument("--error-rate", type=float, default=0)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--capacity", type=int, default=20)
    p.add_argument("--refill", type=float, default=1)
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--api-child", action="store_true")
    p.add_argument("--worker-child", action="store_true")
    p.add_argument("--token")
    p.add_argument("--port", type=int)
    p.add_argument("--audit")
    a = p.parse_args()
    if a.api_child:
        api_child(a)
    elif a.worker_child:
        worker_child(a)
    else:
        try:
            print(json.dumps(run(a), sort_keys=True))
        except EvaluationError as e:
            print("evaluation refused: " + str(e), file=sys.stderr)
            raise SystemExit(2) from None


if __name__ == "__main__":
    main()
