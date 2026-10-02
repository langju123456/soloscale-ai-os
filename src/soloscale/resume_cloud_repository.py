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

from soloscale.resume_cloud_models import TaskMode, TaskState


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class CloudTask:
    id: str
    state: TaskState
    mode: TaskMode
    approve_external_model_call: bool
    resume_docx: bytes | None
    job_description: str | None
    output_docx: bytes | None
    output_sha256: str | None
    attempt_count: int
    model_call_attempted: bool
    worker_token: str | None
    failure_code: str | None


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
            output_docx=row.get("output_docx"),
            output_sha256=row.get("output_sha256"),
            attempt_count=row["attempt_count"],
            model_call_attempted=row["model_call_attempted"],
            worker_token=(str(row["worker_token"]) if row.get("worker_token") else None),
            failure_code=row.get("failure_code"),
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
        input_ttl_hours: int,
    ) -> tuple[CloudTask, bool]:
        task_id = uuid.uuid4()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO resume_cloud_tasks
                (id,idempotency_key,payload_sha256,state,mode,approve_external_model_call,resume_docx,job_description,input_expires_at)
                VALUES (%s,%s,%s,'PREPARED',%s,%s,%s,%s,%s)
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
                ),
            )
            created = cur.fetchone()
            if created is not None:
                return self._task(created), False
            cur.execute(
                "SELECT * FROM resume_cloud_tasks WHERE idempotency_key=%s FOR UPDATE",
                (idempotency_key,),
            )
            existing = cur.fetchone()
            if existing is None:
                raise RuntimeError("idempotency replay was not persisted")
            if existing["payload_sha256"] != payload_hash:
                raise ValueError("IDEMPOTENCY_CONFLICT")
            return self._task(existing), True

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

    def complete(self, task_id: str, token: str, output: bytes, output_ttl_hours: int) -> bool:
        digest = hashlib.sha256(output).hexdigest()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE resume_cloud_tasks SET state='SUCCEEDED', output_docx=%s, output_sha256=%s,
                output_expires_at=%s, resume_docx=NULL, job_description=NULL, completed_at=now(), updated_at=now()
                WHERE id=%s AND state='RUNNING' AND worker_token=%s AND lease_expires_at > now()""",
                (output, digest, _now() + timedelta(hours=output_ttl_hours), task_id, token),
            )
            return cur.rowcount == 1

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
                resume_docx=NULL, job_description=NULL, updated_at=now() FROM selected WHERE t.id=selected.id""")
            recovered = cur.rowcount
            cur.execute("""WITH selected AS (SELECT id FROM resume_cloud_tasks WHERE state IN ('PREPARED','QUEUED')
                AND input_expires_at < now() ORDER BY input_expires_at LIMIT 100)
                UPDATE resume_cloud_tasks t SET state='FAILED', failure_code='INPUT_EXPIRED', resume_docx=NULL,
                job_description=NULL, completed_at=now(), updated_at=now() FROM selected WHERE t.id=selected.id""")
            cur.execute("""WITH selected AS (SELECT id FROM resume_cloud_tasks WHERE state='SUCCEEDED'
                AND output_expires_at < now() ORDER BY output_expires_at LIMIT 100)
                UPDATE resume_cloud_tasks t SET output_docx=NULL, output_sha256=NULL, updated_at=now()
                FROM selected WHERE t.id=selected.id""")
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
            cur.execute("SELECT version FROM resume_cloud_schema_version")
            return cur.fetchone() == {"version": 1}
