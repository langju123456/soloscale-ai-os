"""PostgreSQL persistence for the isolated Resume cloud database only."""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row

from soloscale.resume_cloud_cache import ResultCacheKey, sha256_text
from soloscale.resume_cloud_models import TaskMode, TaskState

LATEST_SCHEMA_VERSION = 4
BASE_SCHEMA_RELATIONS = {
    ("public", "resume_cloud_schema_version"),
    ("public", "resume_cloud_tasks"),
}
RATE_LIMIT_SCHEMA_RELATIONS = BASE_SCHEMA_RELATIONS | {
    ("public", "resume_cloud_rate_limits"),
}
LATEST_SCHEMA_RELATIONS = RATE_LIMIT_SCHEMA_RELATIONS | {
    ("public", "resume_cloud_result_cache"),
}


def _now() -> datetime:
    return datetime.now(UTC)


class RateLimitExceeded(Exception):
    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__("client rate limit exceeded")


@dataclass(frozen=True)
class CloudTask:
    id: str
    state: TaskState
    mode: TaskMode
    approve_external_model_call: bool
    resume_docx: bytes | None
    job_description: str | None
    client_key: str | None
    raw_resume_sha256: str | None
    job_description_sha256: str | None
    output_docx: bytes | None
    output_sha256: str | None
    attempt_count: int
    model_call_attempted: bool
    worker_token: str | None
    failure_code: str | None
    traceparent: str | None


class ResumeCloudRepository:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def _connect(self) -> psycopg.Connection[dict[str, Any]]:
        return psycopg.connect(self.database_url, row_factory=dict_row)

    @staticmethod
    def _task(row: dict[str, Any]) -> CloudTask:
        return CloudTask(
            id=str(row["id"]),
            state=TaskState(row["state"]),
            mode=TaskMode(row["mode"]),
            approve_external_model_call=row["approve_external_model_call"],
            resume_docx=row.get("resume_docx"),
            job_description=row.get("job_description"),
            client_key=row.get("client_key"),
            raw_resume_sha256=row.get("raw_resume_sha256"),
            job_description_sha256=row.get("job_description_sha256"),
            output_docx=row.get("output_docx"),
            output_sha256=row.get("output_sha256"),
            attempt_count=row["attempt_count"],
            model_call_attempted=row["model_call_attempted"],
            worker_token=(str(row["worker_token"]) if row.get("worker_token") else None),
            failure_code=row.get("failure_code"),
            traceparent=row.get("traceparent"),
        )

    def create_or_replay(
        self,
        *,
        idempotency_key: str,
        payload_hash: str,
        resume_docx: bytes,
        job_description: str,
        mode: TaskMode,
        approved: bool,
        raw_resume_sha256: str,
        job_description_sha256: str,
        input_ttl_hours: int,
        client_id: str,
        rate_limit_capacity: int,
        rate_limit_refill_per_second: float,
        rate_limit_now: datetime,
        traceparent: str | None = None,
    ) -> tuple[CloudTask, bool]:
        task_id = uuid.uuid4()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO resume_cloud_tasks
                (id,idempotency_key,payload_sha256,state,mode,approve_external_model_call,resume_docx,job_description,input_expires_at,client_key,raw_resume_sha256,job_description_sha256,traceparent)
                VALUES (%s,%s,%s,'PREPARED',%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (idempotency_key) DO NOTHING RETURNING *""",
                (
                    task_id,
                    idempotency_key,
                    payload_hash,
                    mode.value,
                    approved,
                    resume_docx,
                    job_description,
                    _now() + timedelta(hours=input_ttl_hours),
                    sha256_text(client_id),
                    raw_resume_sha256,
                    job_description_sha256,
                    traceparent,
                ),
            )
            created = cur.fetchone()
            if created is not None:
                allowed, retry_after = self._consume_rate_limit(
                    cur,
                    client_id,
                    capacity=rate_limit_capacity,
                    refill_per_second=rate_limit_refill_per_second,
                    now=rate_limit_now,
                )
                if not allowed:
                    raise RateLimitExceeded(retry_after)
                return self._task(created), False
            cur.execute(
                "SELECT * FROM resume_cloud_tasks WHERE idempotency_key=%s FOR UPDATE",
                (idempotency_key,),
            )
            existing = cur.fetchone()
            if existing is None:
                raise RuntimeError("idempotency replay was not persisted")
            if existing["payload_sha256"] != payload_hash or (
                existing.get("client_key") is not None
                and existing["client_key"] != sha256_text(client_id)
            ):
                raise ValueError("IDEMPOTENCY_CONFLICT")
            return self._task(existing), True

    def consume_rate_limit(
        self,
        client_id: str,
        *,
        capacity: int,
        refill_per_second: float,
        now: datetime,
    ) -> tuple[bool, float]:
        """Atomically spend one client token; API instances share this PostgreSQL row."""
        with self._connect() as conn, conn.cursor() as cur:
            return self._consume_rate_limit(
                cur,
                client_id,
                capacity=capacity,
                refill_per_second=refill_per_second,
                now=now,
            )

    @staticmethod
    def _consume_rate_limit(
        cur: psycopg.Cursor[dict[str, Any]],
        client_id: str,
        *,
        capacity: int,
        refill_per_second: float,
        now: datetime,
    ) -> tuple[bool, float]:
        cur.execute(
            """INSERT INTO resume_cloud_rate_limits (client_id, tokens, updated_at, last_allowed)
            VALUES (%s, %s, %s, FALSE) ON CONFLICT (client_id) DO NOTHING""",
            (client_id, capacity, now),
        )
        cur.execute(
            """WITH available AS (
                    SELECT LEAST(%s::double precision, tokens + GREATEST(0, EXTRACT(EPOCH FROM
                    (%s::timestamptz - updated_at))) * %s) AS tokens FROM resume_cloud_rate_limits
                    WHERE client_id=%s FOR UPDATE)
                UPDATE resume_cloud_rate_limits AS limits SET
                  tokens = GREATEST(0, available.tokens - CASE WHEN available.tokens >= 1 THEN 1 ELSE 0 END),
                  updated_at = GREATEST(limits.updated_at, %s::timestamptz),
                  last_allowed = available.tokens >= 1
                FROM available WHERE limits.client_id=%s
                RETURNING limits.last_allowed, limits.tokens""",
            (capacity, now, refill_per_second, client_id, now, client_id),
        )
        row = cur.fetchone()
        assert row is not None
        return bool(row["last_allowed"]), max(0.0, (1.0 - float(row["tokens"])) / refill_per_second)

    def get(self, task_id: str) -> CloudTask | None:
        with self._connect() as conn, conn.cursor() as cur:
            self._expire_one(cur, task_id)
            cur.execute("SELECT * FROM resume_cloud_tasks WHERE id = %s", (task_id,))
            row = cur.fetchone()
            return self._task(row) if row else None

    def queue(self, task_id: str) -> CloudTask | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE resume_cloud_tasks SET state='QUEUED', updated_at=now()
                WHERE id=%s AND state='PREPARED' AND input_expires_at >= now()
                    AND resume_docx IS NOT NULL AND job_description IS NOT NULL
                RETURNING *""",
                (task_id,),
            )
            row = cur.fetchone()
            if row:
                return self._task(row)
            return self.get(task_id)

    def claim(self, lease_seconds: int) -> CloudTask | None:
        token = uuid.uuid4()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """WITH candidate AS (
                    SELECT id FROM resume_cloud_tasks WHERE state='QUEUED'
                        AND input_expires_at >= now() AND resume_docx IS NOT NULL AND job_description IS NOT NULL
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED LIMIT 1)
                UPDATE resume_cloud_tasks t SET state='RUNNING', attempt_count=attempt_count+1,
                    worker_token=%s, started_at=now(), lease_expires_at=now() + (%s * interval '1 second'), updated_at=now()
                FROM candidate WHERE t.id=candidate.id AND t.attempt_count=0 RETURNING t.*""",
                (token, lease_seconds),
            )
            row = cur.fetchone()
            return self._task(row) if row else None

    def complete(
        self,
        task_id: str,
        token: str,
        output: bytes,
        output_ttl_hours: int,
        cache_key: ResultCacheKey | None = None,
    ) -> bool:
        digest = hashlib.sha256(output).hexdigest()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE resume_cloud_tasks SET state='SUCCEEDED', output_docx=%s, output_sha256=%s,
                output_expires_at=LEAST(input_expires_at, now() + (%s * interval '1 hour')),
                resume_docx=NULL, job_description=NULL, completed_at=now(), updated_at=now()
                WHERE id=%s AND state='RUNNING' AND worker_token=%s AND lease_expires_at > now()
                RETURNING output_expires_at""",
                (output, digest, output_ttl_hours, task_id, token),
            )
            row = cur.fetchone()
            if row is None:
                return False
            if cache_key is not None:
                cur.execute(
                    """INSERT INTO resume_cloud_result_cache
                    (client_key,raw_resume_sha256,job_description_sha256,mode,provider,model,
                    base_url_sha256,pipeline_sha256,output_docx,output_sha256,output_expires_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (client_key,raw_resume_sha256,job_description_sha256,mode,provider,
                    model,base_url_sha256,pipeline_sha256) DO UPDATE SET
                    output_docx=EXCLUDED.output_docx, output_sha256=EXCLUDED.output_sha256,
                    output_expires_at=EXCLUDED.output_expires_at
                    WHERE resume_cloud_result_cache.output_expires_at <= now()""",
                    (
                        cache_key.client_key,
                        cache_key.raw_resume_sha256,
                        cache_key.job_description_sha256,
                        cache_key.mode,
                        cache_key.provider,
                        cache_key.model,
                        cache_key.base_url_sha256,
                        cache_key.pipeline_sha256,
                        output,
                        digest,
                        row["output_expires_at"],
                    ),
                )
            return True

    def complete_from_cache(self, task_id: str, token: str, cache_key: ResultCacheKey) -> bool:
        """Fence a cached completion and retain the cache's original absolute expiry."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """WITH cached AS (
                    SELECT output_docx, output_sha256, output_expires_at
                    FROM resume_cloud_result_cache
                    WHERE client_key=%s AND raw_resume_sha256=%s AND job_description_sha256=%s
                    AND mode=%s AND provider=%s AND model=%s AND base_url_sha256=%s
                    AND pipeline_sha256=%s AND output_expires_at > now())
                UPDATE resume_cloud_tasks AS task SET state='SUCCEEDED', output_docx=cached.output_docx,
                    output_sha256=cached.output_sha256, output_expires_at=cached.output_expires_at,
                    resume_docx=NULL, job_description=NULL, completed_at=now(), updated_at=now()
                FROM cached WHERE task.id=%s AND task.state='RUNNING' AND task.worker_token=%s
                    AND task.lease_expires_at > now() RETURNING task.id""",
                (
                    cache_key.client_key,
                    cache_key.raw_resume_sha256,
                    cache_key.job_description_sha256,
                    cache_key.mode,
                    cache_key.provider,
                    cache_key.model,
                    cache_key.base_url_sha256,
                    cache_key.pipeline_sha256,
                    task_id,
                    token,
                ),
            )
            return cur.fetchone() is not None

    def fail(self, task_id: str, token: str, code: str) -> bool:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE resume_cloud_tasks SET state='FAILED', failure_code=%s, resume_docx=NULL,
                job_description=NULL, completed_at=now(), updated_at=now()
                WHERE id=%s AND state='RUNNING' AND worker_token=%s AND lease_expires_at > now()""",
                (code, task_id, token),
            )
            return cur.rowcount == 1

    def mark_model_call(self, task_id: str, token: str) -> bool:
        """Persist the possible paid-call boundary before provider invocation."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE resume_cloud_tasks SET model_call_attempted=TRUE, updated_at=now()
                WHERE id=%s AND state='RUNNING' AND worker_token=%s AND lease_expires_at > now()""",
                (task_id, token),
            )
            return cur.rowcount == 1

    def renew_lease(self, task_id: str, token: str, lease_seconds: int) -> bool:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE resume_cloud_tasks SET lease_expires_at=now() + (%s * interval '1 second'),
                updated_at=now() WHERE id=%s AND state='RUNNING' AND worker_token=%s
                AND lease_expires_at > now()""",
                (lease_seconds, task_id, token),
            )
            return cur.rowcount == 1

    def recover_and_cleanup(self) -> int:
        """Never requeue an interrupted model call; it may already have been charged."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("""WITH selected AS (SELECT id FROM resume_cloud_tasks
                WHERE state='RUNNING' AND lease_expires_at < now() ORDER BY lease_expires_at LIMIT 100)
                UPDATE resume_cloud_tasks t SET state='NEEDS_REVIEW', failure_code='LEASE_EXPIRED',
                resume_docx=NULL, job_description=NULL, updated_at=now() FROM selected
                WHERE t.id=selected.id AND t.state='RUNNING' AND t.lease_expires_at < now()""")
            recovered = cur.rowcount
            cur.execute("""WITH selected AS (SELECT id FROM resume_cloud_tasks WHERE state IN ('PREPARED','QUEUED')
                AND input_expires_at < now() ORDER BY input_expires_at LIMIT 100)
                UPDATE resume_cloud_tasks t SET state='FAILED', failure_code='INPUT_EXPIRED', resume_docx=NULL,
                job_description=NULL, completed_at=now(), updated_at=now() FROM selected
                WHERE t.id=selected.id AND t.state IN ('PREPARED','QUEUED') AND t.input_expires_at < now()""")
            cur.execute("""WITH selected AS (SELECT id FROM resume_cloud_tasks WHERE state='SUCCEEDED'
                AND output_expires_at < now() ORDER BY output_expires_at LIMIT 100)
                UPDATE resume_cloud_tasks t SET output_docx=NULL, output_sha256=NULL, updated_at=now()
                FROM selected WHERE t.id=selected.id AND t.state='SUCCEEDED' AND t.output_expires_at < now()""")
            cur.execute("""WITH selected AS (SELECT client_key, raw_resume_sha256, job_description_sha256,
                mode, provider, model, base_url_sha256, pipeline_sha256 FROM resume_cloud_result_cache
                WHERE output_expires_at < now() ORDER BY output_expires_at LIMIT 100)
                DELETE FROM resume_cloud_result_cache AS cache USING selected
                WHERE (cache.client_key, cache.raw_resume_sha256, cache.job_description_sha256, cache.mode,
                cache.provider, cache.model, cache.base_url_sha256, cache.pipeline_sha256) =
                (selected.client_key, selected.raw_resume_sha256, selected.job_description_sha256, selected.mode,
                selected.provider, selected.model, selected.base_url_sha256, selected.pipeline_sha256)""")
            return recovered

    @staticmethod
    def _expire_one(cur: psycopg.Cursor[dict[str, Any]], task_id: str) -> None:
        cur.execute(
            """UPDATE resume_cloud_tasks SET state='FAILED', failure_code='INPUT_EXPIRED',
            resume_docx=NULL, job_description=NULL, completed_at=now(), updated_at=now()
            WHERE id=%s AND state IN ('PREPARED','QUEUED') AND input_expires_at < now()""",
            (task_id,),
        )
        cur.execute(
            """UPDATE resume_cloud_tasks SET output_docx=NULL, output_sha256=NULL, updated_at=now()
            WHERE id=%s AND state='SUCCEEDED' AND output_expires_at < now()""",
            (task_id,),
        )

    def ready(self) -> bool:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT n.nspname, c.relname FROM pg_class AS c
                JOIN pg_namespace AS n ON n.oid=c.relnamespace
                WHERE n.nspname='public' AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')"""
            )
            relations = {(row["nspname"], row["relname"]) for row in cur.fetchall()}
            if relations != LATEST_SCHEMA_RELATIONS:
                return False
            cur.execute("SELECT version FROM resume_cloud_schema_version ORDER BY version")
            return cur.fetchall() == [{"version": LATEST_SCHEMA_VERSION}]
