"""Dedicated-Postgres integration checks; skipped locally without an explicit test DB."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi.testclient import TestClient

from soloscale.model_gateway import (
    GatewayConfigurationState,
    GatewayDescriptor,
    GatewayTransportScope,
    ModelGateway,
    ModelProviderId,
)
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
        cur.execute(
            "TRUNCATE resume_cloud_tasks, resume_cloud_rate_limits, resume_cloud_result_cache"
        )


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


def _client(settings: CloudSettings | None = None) -> tuple[TestClient, ResumeCloudService]:
    assert DATABASE_URL is not None
    selected = settings or CloudSettings(database_url=DATABASE_URL, bearer_token="t" * 32)
    service = ResumeCloudService(ResumeCloudRepository(DATABASE_URL), selected)
    return TestClient(create_app(selected)), service


def _gateway(model: str = "synthetic") -> ModelGateway:
    return cast(
        ModelGateway,
        SimpleNamespace(
            descriptor=GatewayDescriptor(
                provider=ModelProviderId.OPENAI_COMPATIBLE,
                display_name="Synthetic",
                configuration_state=GatewayConfigurationState.CONFIGURED,
                transport_scope=GatewayTransportScope.EXTERNAL,
                model=model,
                base_url="https://example.test/v1",
            )
        ),
    )


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


def test_result_cache_reuses_same_client_without_extending_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, service = _client()
    headers = {"Authorization": "Bearer " + "t" * 32, "X-Client-ID": "alpha"}
    calls = 0

    def tailor(*_: object) -> object:
        nonlocal calls
        calls += 1
        return type("Result", (), {"content": b"cached-docx"})()

    monkeypatch.setattr("soloscale.resume_cloud_service.tailor_resume_docx", tailor)
    first = client.post(
        "/tasks", json=_payload(), headers={**headers, "Idempotency-Key": str(uuid.uuid4())}
    ).json()["id"]
    assert client.post(f"/tasks/{first}/process", headers=headers).status_code == 200
    assert service.process_one() is not None
    with service.repository._connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT output_expires_at FROM resume_cloud_tasks WHERE id=%s", (first,))
        original = cur.fetchone()
        assert original is not None
        original_expiry = original["output_expires_at"]
    second = client.post(
        "/tasks", json=_payload(), headers={**headers, "Idempotency-Key": str(uuid.uuid4())}
    ).json()["id"]
    client.post(f"/tasks/{second}/process", headers=headers)
    reused = service.process_one()
    assert calls == 1 and reused is not None and reused.state.value == "SUCCEEDED"
    with service.repository._connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT output_expires_at FROM resume_cloud_tasks WHERE id=%s", (second,))
        reused_expiry = cur.fetchone()
        assert reused_expiry is not None
        assert reused_expiry["output_expires_at"] == original_expiry


def test_result_cache_does_not_cross_client_or_survive_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, service = _client()
    headers = {"Authorization": "Bearer " + "t" * 32, "X-Client-ID": "alpha"}
    calls = 0

    def tailor(*_: object) -> object:
        nonlocal calls
        calls += 1
        return type("Result", (), {"content": b"cached-docx"})()

    monkeypatch.setattr("soloscale.resume_cloud_service.tailor_resume_docx", tailor)
    first = client.post(
        "/tasks", json=_payload(), headers={**headers, "Idempotency-Key": str(uuid.uuid4())}
    ).json()["id"]
    client.post(f"/tasks/{first}/process", headers=headers)
    assert service.process_one() is not None
    with service.repository._connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE resume_cloud_result_cache SET output_expires_at=now() - interval '1 second'"
        )
    other_headers = {**headers, "X-Client-ID": "beta", "Idempotency-Key": str(uuid.uuid4())}
    other = client.post("/tasks", json=_payload(), headers=other_headers).json()["id"]
    client.post(f"/tasks/{other}/process", headers=other_headers)
    assert service.process_one() is not None and calls == 2
    service.repository.recover_and_cleanup()
    with service.repository._connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS count FROM resume_cloud_result_cache")
        assert cur.fetchone() == {"count": 1}


def test_cross_client_idempotency_replay_is_conflict() -> None:
    client, _ = _client()
    key = str(uuid.uuid4())
    auth = {"Authorization": "Bearer " + "t" * 32, "Idempotency-Key": key}
    alpha = client.post("/tasks", json=_payload(), headers={**auth, "X-Client-ID": "alpha"})
    beta = client.post("/tasks", json=_payload(), headers={**auth, "X-Client-ID": "beta"})
    assert alpha.status_code == 201
    assert beta.status_code == 409


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
        return request(payload, key, "default")[0].id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: create(), range(2)))
    assert ids[0] == ids[1]


def test_client_rate_limit_shared_default_and_clock_recovery() -> None:
    assert DATABASE_URL is not None
    now = datetime(2026, 1, 1, tzinfo=UTC)
    settings = CloudSettings(
        database_url=DATABASE_URL,
        bearer_token="t" * 32,
        rate_limit_capacity=2,
        rate_limit_refill_per_second=1.0,
        clock=lambda: now,
    )
    client = TestClient(create_app(settings))
    headers = {"Authorization": "Bearer " + "t" * 32}

    def submit(key: str, client_id: str | None = None) -> int:
        supplied = {**headers, "Idempotency-Key": key}
        if client_id is not None:
            supplied["X-Client-ID"] = client_id
        return int(client.post("/tasks", json=_payload(), headers=supplied).status_code)

    assert submit(str(uuid.uuid4()), "alpha") == 201
    assert submit(str(uuid.uuid4()), "alpha") == 201
    assert submit(str(uuid.uuid4()), "alpha") == 429
    assert submit(str(uuid.uuid4()), "beta") == 201
    assert submit(str(uuid.uuid4())) == 201
    assert submit(str(uuid.uuid4())) == 201
    assert submit(str(uuid.uuid4())) == 429
    now += timedelta(seconds=1)
    assert submit(str(uuid.uuid4()), "alpha") == 201
    now -= timedelta(seconds=10)
    assert submit(str(uuid.uuid4()), "alpha") == 429
    invalid = client.post(
        "/tasks",
        json=_payload(),
        headers={**headers, "Idempotency-Key": str(uuid.uuid4()), "X-Client-ID": "bad label"},
    )
    assert invalid.status_code == 400


def test_rate_limit_replays_and_conflicts_do_not_spend_tokens() -> None:
    assert DATABASE_URL is not None
    now = datetime(2026, 1, 1, tzinfo=UTC)
    settings = CloudSettings(
        database_url=DATABASE_URL,
        bearer_token="t" * 32,
        rate_limit_capacity=1,
        rate_limit_refill_per_second=1.0,
        clock=lambda: now,
    )
    client, _ = _client(settings)
    headers = {"Authorization": "Bearer " + "t" * 32, "X-Client-ID": "alpha"}
    key = str(uuid.uuid4())
    original = client.post("/tasks", json=_payload(), headers={**headers, "Idempotency-Key": key})
    assert original.status_code == 201
    replay = client.post("/tasks", json=_payload(), headers={**headers, "Idempotency-Key": key})
    assert replay.json()["id"] == original.json()["id"]
    changed = _payload()
    changed["job_description"] = "changed"
    conflict = client.post("/tasks", json=changed, headers={**headers, "Idempotency-Key": key})
    assert conflict.status_code == 409
    limited = client.post(
        "/tasks", json=_payload(), headers={**headers, "Idempotency-Key": str(uuid.uuid4())}
    )
    assert limited.status_code == 429 and limited.headers["retry-after"] == "1"
    with ResumeCloudRepository(DATABASE_URL)._connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS count FROM resume_cloud_tasks")
        assert cur.fetchone() == {"count": 1}


def test_same_key_concurrent_requests_spend_one_token() -> None:
    assert DATABASE_URL is not None
    settings = CloudSettings(
        database_url=DATABASE_URL,
        bearer_token="t" * 32,
        rate_limit_capacity=1,
        rate_limit_refill_per_second=1.0,
    )
    key = str(uuid.uuid4())
    headers = {
        "Authorization": "Bearer " + "t" * 32,
        "Idempotency-Key": key,
        "X-Client-ID": "alpha",
    }
    barrier = threading.Barrier(2)

    def submit() -> tuple[int, str]:
        client = TestClient(create_app(settings))
        barrier.wait()
        response = client.post("/tasks", json=_payload(), headers=headers)
        return response.status_code, response.json()["id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(), range(2)))
    assert [status for status, _ in results] == [201, 201]
    assert len({task_id for _, task_id in results}) == 1
    allowed, _ = ResumeCloudRepository(DATABASE_URL).consume_rate_limit(
        "alpha", capacity=1, refill_per_second=1.0, now=datetime.now(UTC)
    )
    assert allowed is False


def test_distinct_keys_across_api_instances_share_one_token() -> None:
    assert DATABASE_URL is not None
    now = datetime(2026, 1, 1, tzinfo=UTC)
    settings = CloudSettings(
        database_url=DATABASE_URL,
        bearer_token="t" * 32,
        rate_limit_capacity=1,
        rate_limit_refill_per_second=0.5,
        clock=lambda: now,
    )
    barrier = threading.Barrier(2)

    def submit() -> tuple[int, str | None]:
        client = TestClient(create_app(settings))
        barrier.wait()
        response = client.post(
            "/tasks",
            json=_payload(),
            headers={
                "Authorization": "Bearer " + "t" * 32,
                "Idempotency-Key": str(uuid.uuid4()),
                "X-Client-ID": "alpha",
            },
        )
        return response.status_code, response.headers.get("retry-after")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(), range(2)))
    assert sorted(status for status, _ in results) == [201, 429]
    assert next(retry for status, retry in results if status == 429) == "2"
    with ResumeCloudRepository(DATABASE_URL)._connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS count FROM resume_cloud_tasks")
        assert cur.fetchone() == {"count": 1}


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


def test_no_available_worker_slot_leaves_task_queued() -> None:
    client, service = _client()
    headers = {"Authorization": "Bearer " + "t" * 32, "Idempotency-Key": str(uuid.uuid4())}
    task_id = client.post("/tasks", json=_payload(), headers=headers).json()["id"]
    client.post(f"/tasks/{task_id}/process", headers=headers)
    assert service._model_slots.acquire(blocking=False)
    try:
        assert service.process_one() is None
    finally:
        service._model_slots.release()
    queued = service.repository.get(task_id)
    assert queued is not None and queued.state.value == "QUEUED" and queued.attempt_count == 0


def test_claim_exception_releases_worker_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    _, service = _client()

    def fail_claim(_: int) -> CloudTask | None:
        raise RuntimeError("synthetic claim failure")

    monkeypatch.setattr(service.repository, "claim", fail_claim)
    assert service.process_one() is None
    assert service._model_slots.acquire(blocking=False)
    service._model_slots.release()


def test_model_concurrency_leaves_surplus_task_queued(monkeypatch: pytest.MonkeyPatch) -> None:
    assert DATABASE_URL is not None
    settings = CloudSettings(
        database_url=DATABASE_URL, bearer_token="t" * 32, max_model_concurrency=2
    )
    client, service = _client(settings)
    headers = {"Authorization": "Bearer " + "t" * 32}
    task_ids = []
    for _ in range(3):
        created = client.post(
            "/tasks", json=_payload(), headers={**headers, "Idempotency-Key": str(uuid.uuid4())}
        )
        task_ids.append(created.json()["id"])
        client.post(f"/tasks/{task_ids[-1]}/process", headers=headers)
    active = 0
    peak = 0
    lock = threading.Lock()
    entered = threading.Event()
    release = threading.Event()

    def blocking_tailor(*_: object, **__: object) -> object:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                entered.set()
        assert release.wait(timeout=2)
        with lock:
            active -= 1
        return type("Result", (), {"content": b"docx"})()

    monkeypatch.setattr("soloscale.resume_cloud_service.tailor_resume_docx", blocking_tailor)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(service.process_one) for _ in range(3)]
        assert entered.wait(timeout=2)
        release.set()
        [future.result() for future in futures]
    assert peak == 2
    states = [service.repository.get(task_id).state.value for task_id in task_ids]  # type: ignore[union-attr]
    assert sorted(states) == ["QUEUED", "SUCCEEDED", "SUCCEEDED"]


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


def test_stale_or_wrong_worker_tokens_cannot_change_task() -> None:
    client, service = _client()
    headers = {"Authorization": "Bearer " + "t" * 32, "Idempotency-Key": str(uuid.uuid4())}
    task_id = client.post("/tasks", json=_payload(), headers=headers).json()["id"]
    client.post(f"/tasks/{task_id}/process", headers=headers)
    claim = service.repository.claim(300)
    assert claim is not None and claim.worker_token is not None
    assert not service.repository.complete(task_id, str(uuid.uuid4()), b"docx", 1)
    assert not service.repository.fail(task_id, str(uuid.uuid4()), "PROCESSING_REJECTED")
    assert not service.repository.renew_lease(task_id, str(uuid.uuid4()), 300)
    with service.repository._connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE resume_cloud_tasks SET lease_expires_at=now() - interval '1 second' "
            "WHERE id=%s",
            (task_id,),
        )
    assert not service.repository.complete(task_id, claim.worker_token, b"docx", 1)
    assert not service.repository.fail(task_id, claim.worker_token, "PROCESSING_REJECTED")
    assert not service.repository.renew_lease(task_id, claim.worker_token, 300)
    assert service.repository.recover_and_cleanup() == 1
    task = service.repository.get(task_id)
    assert task is not None and task.state.value == "NEEDS_REVIEW"


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


def test_migrator_rerun_accepts_exact_v3_schema() -> None:
    assert DATABASE_URL is not None
    result = subprocess.run(
        [sys.executable, Path(__file__).parents[1] / "scripts" / "migrate_resume_cloud.py"],
        env={**os.environ, "RESUME_CLOUD_DATABASE_URL": DATABASE_URL},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_migrator_refuses_v3_relations_with_v1_version() -> None:
    assert DATABASE_URL is not None
    with ResumeCloudRepository(DATABASE_URL)._connect() as conn, conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE resume_cloud_schema_version DROP CONSTRAINT "
            "resume_cloud_schema_version_version_check"
        )
        cur.execute("UPDATE resume_cloud_schema_version SET version=1")
    try:
        result = subprocess.run(
            [sys.executable, Path(__file__).parents[1] / "scripts" / "migrate_resume_cloud.py"],
            env={**os.environ, "RESUME_CLOUD_DATABASE_URL": DATABASE_URL},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
    finally:
        with ResumeCloudRepository(DATABASE_URL)._connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE resume_cloud_schema_version SET version=3")
            cur.execute(
                "ALTER TABLE resume_cloud_schema_version ADD CONSTRAINT "
                "resume_cloud_schema_version_version_check CHECK (version IN (1, 2, 3))"
            )


def test_ready_requires_latest_schema_version() -> None:
    assert DATABASE_URL is not None
    repository = ResumeCloudRepository(DATABASE_URL)
    assert repository.ready()
    with repository._connect() as conn, conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE resume_cloud_schema_version DROP CONSTRAINT "
            "resume_cloud_schema_version_version_check"
        )
        cur.execute("UPDATE resume_cloud_schema_version SET version=1")
    try:
        assert not repository.ready()
    finally:
        with repository._connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE resume_cloud_schema_version SET version=3")
            cur.execute(
                "ALTER TABLE resume_cloud_schema_version ADD CONSTRAINT "
                "resume_cloud_schema_version_version_check CHECK (version IN (1, 2, 3))"
            )


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
    assert service.process_one(gateway=_gateway()) is not None
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
    assert service.process_one(gateway=_gateway()) is None
    assert service.repository.get(task_id).state.value == "NEEDS_REVIEW"  # type: ignore[union-attr]
