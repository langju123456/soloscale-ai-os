"""Dedicated-Postgres integration checks; skipped locally without an explicit test DB."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from soloscale.model_gateway import ModelGateway
from soloscale.resume_cloud_api import create_app
from soloscale.resume_cloud_models import TaskMode
from soloscale.resume_cloud_repository import CloudTask, ResumeCloudRepository
from soloscale.resume_cloud_service import CloudSettings, ResumeCloudService

DATABASE_URL = os.environ.get("CLOUD_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="requires CLOUD_TEST_DATABASE_URL")


@pytest.fixture(autouse=True)
def clean_tasks() -> None:
    assert DATABASE_URL is not None
    with ResumeCloudRepository(DATABASE_URL)._connect() as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE resume_cloud_tasks")


def _payload() -> dict[str, object]:
    text = """Synthetic Candidate
AI Engineer
PROJECT HIGHLIGHTS
Synthetic project
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
        "job_description": "Python and Docker engineering role.",
        "mode": "template",
        "approve_external_model_call": False,
    }


def _client() -> tuple[TestClient, ResumeCloudService]:
    assert DATABASE_URL is not None
    settings = CloudSettings(database_url=DATABASE_URL, bearer_token="t" * 32)
    service = ResumeCloudService(ResumeCloudRepository(DATABASE_URL), settings)
    return TestClient(create_app(settings)), service


def test_create_idempotency_worker_and_download() -> None:
    client, service = _client()
    headers = {"Authorization": "Bearer " + "t" * 32, "Idempotency-Key": str(uuid.uuid4())}
    created = client.post("/tasks", json=_payload(), headers=headers)
    assert created.status_code == 201
    task_id = created.json()["id"]
    assert client.post("/tasks", json=_payload(), headers=headers).json()["id"] == task_id
    changed = _payload()
    changed["job_description"] = "different"
    assert client.post("/tasks", json=changed, headers=headers).status_code == 409
    assert client.post(f"/tasks/{task_id}/process", headers=headers).json()["state"] == "QUEUED"
    processed = service.process_one()
    assert processed is not None and processed.state.value == "SUCCEEDED"
    output = client.get(f"/tasks/{task_id}/download", headers=headers)
    assert output.status_code == 200 and output.headers["x-soloscale-mode"] == "template-only"
    assert output.headers["x-soloscale-output-sha256"]


def test_auth_and_two_claims_are_bounded() -> None:
    client, service = _client()
    assert client.get("/tasks/nope", headers={"Authorization": b"Bearer \xff"}).status_code == 401
    headers = {"Authorization": "Bearer " + "t" * 32}
    for _ in range(2):
        response = client.post(
            "/tasks", json=_payload(), headers={**headers, "Idempotency-Key": str(uuid.uuid4())}
        )
        service.repository.queue(response.json()["id"])
    first, second = service.repository.claim(300), service.repository.claim(300)
    assert first is not None and second is not None and first.id != second.id
    assert service.repository.claim(300) is None and first.mode is TaskMode.TEMPLATE


def test_same_key_concurrent_create_is_one_task() -> None:
    client, service = _client()
    request = service.create
    from soloscale.resume_cloud_models import CreateTaskRequest

    payload = CreateTaskRequest.model_validate(_payload())
    key = str(uuid.uuid4())
    barrier = threading.Barrier(2)

    def create() -> str:
        barrier.wait()
        return request(payload, key)[0].id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: create(), range(2)))
    assert ids[0] == ids[1]


def test_two_worker_threads_claim_one_task_and_expiry_is_immediate() -> None:
    client, service = _client()
    headers = {"Authorization": "Bearer " + "t" * 32, "Idempotency-Key": str(uuid.uuid4())}
    task_id = client.post("/tasks", json=_payload(), headers=headers).json()["id"]
    client.post(f"/tasks/{task_id}/process", headers=headers)
    barrier = threading.Barrier(2)

    def claim() -> CloudTask | None:
        barrier.wait()
        return service.repository.claim(300)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: claim(), range(2)))
    assert sum(item is not None for item in claims) == 1
    winner = next(item for item in claims if item is not None)
    assert winner is not None and winner.worker_token is not None
    with service.repository._connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE resume_cloud_tasks SET lease_expires_at=now() - interval '1 second' "
            "WHERE id=%s",
            (task_id,),
        )
    assert service.repository.complete(task_id, winner.worker_token, b"docx", 1) is False
    assert service.repository.recover_and_cleanup() == 1
    assert service.repository.get(task_id).state.value == "NEEDS_REVIEW"  # type: ignore[union-attr]


def test_expired_input_and_output_are_unavailable_without_cleanup() -> None:
    client, service = _client()
    headers = {"Authorization": "Bearer " + "t" * 32, "Idempotency-Key": str(uuid.uuid4())}
    task_id = client.post("/tasks", json=_payload(), headers=headers).json()["id"]
    with service.repository._connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE resume_cloud_tasks SET input_expires_at=now() - interval '1 second' "
            "WHERE id=%s",
            (task_id,),
        )
    assert client.post(f"/tasks/{task_id}/process", headers=headers).json()["state"] == "FAILED"
    task_id = client.post(
        "/tasks", json=_payload(), headers={**headers, "Idempotency-Key": str(uuid.uuid4())}
    ).json()["id"]
    client.post(f"/tasks/{task_id}/process", headers=headers)
    assert service.process_one() is not None
    with service.repository._connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE resume_cloud_tasks SET output_expires_at=now() - interval '1 second' "
            "WHERE id=%s",
            (task_id,),
        )
    assert client.get(f"/tasks/{task_id}/download", headers=headers).status_code == 409


@pytest.mark.parametrize("relation_sql", [
    "CREATE TABLE other_schema.audit (id integer)",
    "CREATE MATERIALIZED VIEW other_schema.audit AS SELECT 1 AS id",
])
def test_migrator_refuses_relation_in_other_schema(relation_sql: str) -> None:
    assert DATABASE_URL is not None
    with ResumeCloudRepository(DATABASE_URL)._connect() as conn, conn.cursor() as cur:
        cur.execute("CREATE SCHEMA other_schema")
        cur.execute(relation_sql)
    result = subprocess.run(
        [sys.executable, Path(__file__).parents[1] / "scripts" / "migrate_resume_cloud.py"],
        env={**os.environ, "RESUME_CLOUD_DATABASE_URL": DATABASE_URL},
        capture_output=True,
        text=True,
        check=False,
    )
    with ResumeCloudRepository(DATABASE_URL)._connect() as conn, conn.cursor() as cur:
        cur.execute("DROP SCHEMA other_schema CASCADE")
    assert result.returncode != 0


def test_ai_failure_records_one_attempt_without_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    client, service = _client()
    headers = {"Authorization": "Bearer " + "t" * 32, "Idempotency-Key": str(uuid.uuid4())}
    payload = _payload()
    payload["mode"] = "ai"
    payload["approve_external_model_call"] = True
    task_id = client.post("/tasks", json=payload, headers=headers).json()["id"]
    client.post(f"/tasks/{task_id}/process", headers=headers)
    calls = 0

    def failing_gateway(*_: object, **__: object) -> object:
        nonlocal calls
        calls += 1
        raise RuntimeError("synthetic gateway failure")

    monkeypatch.setattr(
        "soloscale.resume_cloud_service.tailor_resume_docx_with_gateway", failing_gateway
    )
    assert service.process_one(gateway=cast(ModelGateway, object())) is not None
    task = service.repository.get(task_id)
    assert calls == 1 and task is not None and task.model_call_attempted
    assert task.state.value == "FAILED"


def test_stale_ai_recovery_never_invokes_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    client, service = _client()
    headers = {"Authorization": "Bearer " + "t" * 32, "Idempotency-Key": str(uuid.uuid4())}
    payload = _payload()
    payload["mode"] = "ai"
    payload["approve_external_model_call"] = True
    task_id = client.post("/tasks", json=payload, headers=headers).json()["id"]
    client.post(f"/tasks/{task_id}/process", headers=headers)
    assert service.repository.claim(300) is not None
    with service.repository._connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE resume_cloud_tasks SET lease_expires_at=now() - interval '1 second' "
            "WHERE id=%s",
            (task_id,),
        )
    service.repository.recover_and_cleanup()
    monkeypatch.setattr(
        "soloscale.resume_cloud_service.tailor_resume_docx_with_gateway",
        lambda *_args, **_kwargs: pytest.fail("stale task must not invoke gateway"),
    )
    assert service.process_one(gateway=cast(ModelGateway, object())) is None
    assert service.repository.get(task_id).state.value == "NEEDS_REVIEW"  # type: ignore[union-attr]
