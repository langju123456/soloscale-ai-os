"""Exported-span proof, including a real API-to-worker process boundary."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any, TypeVar

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from soloscale.model_gateway import (
    GatewayErrorCategory,
    GatewayFailureDetails,
    ModelCallProfile,
    ModelGatewayTimeoutError,
)
from soloscale.resume_cloud_api import create_app
from soloscale.resume_cloud_observability import Telemetry
from soloscale.resume_cloud_repository import ResumeCloudRepository
from soloscale.resume_cloud_service import CloudSettings
from soloscale.resume_cloud_worker import _trace_recovery

DATABASE_URL = os.environ.get("CLOUD_TEST_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[1]
ResponseT = TypeVar("ResponseT", bound=BaseModel)


def _evaluation() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "cloud_trace_fake", ROOT / "scripts/evaluate_resume_concurrency.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _telemetry() -> tuple[Telemetry, Any]:
    sdk = pytest.importorskip("opentelemetry.sdk.trace")
    exporting = pytest.importorskip("opentelemetry.sdk.trace.export")
    memory = pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")
    exporter = memory.InMemorySpanExporter()
    provider = sdk.TracerProvider()
    provider.add_span_processor(exporting.SimpleSpanProcessor(exporter))
    return Telemetry(enabled=True, provider=provider), exporter


@pytest.fixture
def database() -> str:
    if not DATABASE_URL:
        pytest.skip("requires CLOUD_TEST_DATABASE_URL")
    with ResumeCloudRepository(DATABASE_URL)._connect() as connection:
        connection.execute(
            "TRUNCATE resume_cloud_tasks,resume_cloud_rate_limits,resume_cloud_result_cache"
        )
    return DATABASE_URL


def test_default_disabled_never_imports_opentelemetry_sdk() -> None:
    program = """
import builtins
real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.startswith(("opentelemetry.sdk", "opentelemetry.exporter")):
        raise AssertionError("optional tracing SDK imported while disabled")
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded
from soloscale.resume_cloud_observability import Telemetry
from soloscale.resume_cloud_api import create_app
from soloscale.resume_cloud_service import CloudSettings
telemetry = Telemetry()
with telemetry.span("disabled"):
    assert telemetry.traceparent() is None
from fastapi.testclient import TestClient
app = create_app(CloudSettings(database_url="unused", bearer_token="x" * 32), telemetry=telemetry)
with TestClient(app) as client:
    assert client.get("/health/live").status_code == 204
telemetry.shutdown()
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "RESUME_CLOUD_TRACING": "false",
            "FASTAPI_OTEL_AUTO_CONFIGURE": "true",
            "RESUME_CLOUD_METRICS_EXPORTER": "none",
        },
    )
    assert result.returncode == 0, result.stderr


def test_gateway_failure_and_bad_parent_never_export_content(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    telemetry, exporter = _telemetry()
    evaluation = _evaluation()
    sentinel = "RAW_MODEL_RESUME_JD_SECRET_SENTINEL"

    class FailingGateway:
        descriptor = evaluation.FakeGateway(tmp_path / "unused", 0, 0, 7).descriptor
        last_call_profile: ModelCallProfile | None = None
        calls = 0

        def complete(
            self, schema: type[ResponseT], *, system: str, user: str, reasoning_effort: str = "none"
        ) -> ResponseT:
            self.calls += 1
            raise ModelGatewayTimeoutError(
                sentinel,
                details=GatewayFailureDetails(
                    correlation_id="gateway-" + "a" * 24,
                    model="fake-v1",
                    category=GatewayErrorCategory.TIMEOUT,
                    retryable=True,
                    duration_ms=5,
                ),
            )

    gateway = FailingGateway()
    with telemetry.span("request", parent=sentinel):
        with pytest.raises(ModelGatewayTimeoutError):
            telemetry.wrap_gateway(gateway).complete(BaseModel, system=sentinel, user=sentinel)
    telemetry.shutdown()
    spans = exporter.get_finished_spans()
    model = next(span for span in spans if span.name == "model.gateway.complete")
    assert model.attributes["error.category"] == "gateway_timeout"
    assert model.attributes["retryable"] is True
    assert model.attributes["duration.ms"] >= 0
    assert gateway.calls == 1
    assert not model.events
    assert sentinel not in "".join(span.to_json() for span in spans) + caplog.text


def _worker_process(database: str, tmp_path: Path, suffix: str) -> list[dict[str, Any]]:
    destination = tmp_path / f"spans-{suffix}.json"
    program = """
import importlib.util, json, os, sys
from pathlib import Path
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from soloscale.resume_cloud_observability import Telemetry
from soloscale.resume_cloud_repository import ResumeCloudRepository
from soloscale.resume_cloud_service import ResumeCloudService, CloudSettings
spec = importlib.util.spec_from_file_location("trace_worker_fake", os.environ["TRACE_FAKE_PATH"])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
telemetry = Telemetry(enabled=True, provider=provider)
dsn = os.environ["CLOUD_TEST_DATABASE_URL"]
service = ResumeCloudService(
    ResumeCloudRepository(dsn),
    CloudSettings(database_url=dsn, bearer_token="x"*32),
    telemetry=telemetry,
)
result = service.process_one(module.FakeGateway(Path(os.environ["TRACE_AUDIT_PATH"]), 0, 0, 7))
assert result is not None and result.state.value == "SUCCEEDED"
telemetry.shutdown()
exported = [json.loads(span.to_json()) for span in exporter.get_finished_spans()]
Path(os.environ["TRACE_EXPORT_PATH"]).write_text(json.dumps(exported))
"""
    child = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "CLOUD_TEST_DATABASE_URL": database,
            "TRACE_FAKE_PATH": str(ROOT / "scripts/evaluate_resume_concurrency.py"),
            "TRACE_AUDIT_PATH": str(tmp_path / f"calls-{suffix}.jsonl"),
            "TRACE_EXPORT_PATH": str(destination),
            "RESUME_CLOUD_METRICS_EXPORTER": "none",
        },
    )
    assert child.returncode == 0, child.stderr
    return json.loads(destination.read_text())  # type: ignore[no-any-return]


def test_real_api_worker_process_tree_and_cache_terminal(database: str, tmp_path: Path) -> None:
    telemetry, exporter = _telemetry()
    settings = CloudSettings(database_url=database, bearer_token="t" * 32)
    body = _evaluation().payload(61)
    headers = {"Authorization": "Bearer " + "t" * 32, "Idempotency-Key": str(uuid.uuid4())}
    with TestClient(create_app(settings, telemetry=telemetry)) as client:
        created = client.post("/tasks", json=body, headers=headers)
        assert created.status_code == 201
        task_id = created.json()["id"]
        assert client.post("/tasks", json=body, headers=headers).json()["id"] == task_id
        changed = {**body, "job_description": "private altered JD"}
        assert client.post("/tasks", json=changed, headers=headers).status_code == 409
        assert client.post(f"/tasks/{task_id}/process", headers=headers).status_code == 200
        first_spans = _worker_process(database, tmp_path, "first")
        cached = client.post(
            "/tasks", json=body, headers={**headers, "Idempotency-Key": str(uuid.uuid4())}
        )
        cached_id = cached.json()["id"]
        client.post(f"/tasks/{cached_id}/process", headers=headers)
        cache_spans = _worker_process(database, tmp_path, "cache")
    api_spans = [span for span in exporter.get_finished_spans() if span.name == "api.tasks.create"]
    assert [span.attributes["outcome"] for span in api_spans] == [
        "created",
        "replayed",
        "rejected",
        "created",
    ]
    assert api_spans[2].attributes["error.category"] == "idempotency_conflict"
    by_name = {span["name"]: span for span in first_spans}
    trace_id = f"0x{api_spans[0].context.trace_id:032x}"
    assert all(span["context"]["trace_id"] == trace_id for span in first_spans)
    assert by_name["worker.claim"]["parent_id"] == f"0x{api_spans[0].context.span_id:016x}"
    assert by_name["worker.execute"]["parent_id"] == by_name["worker.claim"]["context"]["span_id"]
    assert (
        by_name["model.gateway.complete"]["parent_id"]
        == by_name["worker.execute"]["context"]["span_id"]
    )
    assert by_name["model.gateway.complete"]["attributes"]["model.name"] == "fake-v1"
    assert by_name["model.gateway.complete"]["attributes"]["duration.ms"] >= 0
    assert {
        "pipeline.extract_candidate_profile",
        "pipeline.prepare_gateway_payload",
        "pipeline.role_strategy",
        "pipeline.select_safe_rewrites",
        "pipeline.render_projects",
        "pipeline.render_skills",
    } <= by_name.keys()
    assert by_name["worker.terminal"]["attributes"]["terminal.state"] == "SUCCEEDED"
    assert not any(span["name"] == "model.gateway.complete" for span in cache_spans)
    cache_terminal = next(span for span in cache_spans if span["name"] == "worker.terminal")
    assert cache_terminal["attributes"] == {"terminal.state": "SUCCEEDED", "cache_hit": True}
    assert cache_terminal["context"]["trace_id"] == f"0x{api_spans[-1].context.trace_id:032x}"
    all_export = json.dumps(first_spans + cache_spans) + "".join(
        span.to_json() for span in api_spans
    )
    for private in (
        "Synthetic Candidate",
        "Built a Python retrieval system.",
        "Python and Docker engineering role.",
        "private altered JD",
    ):
        assert private not in all_export


def test_expired_call_recovery_joins_original_trace_once(database: str) -> None:
    telemetry, exporter = _telemetry()
    settings = CloudSettings(database_url=database, bearer_token="t" * 32)
    headers = {"Authorization": "Bearer " + "t" * 32, "Idempotency-Key": str(uuid.uuid4())}
    repository = ResumeCloudRepository(database)
    with TestClient(create_app(settings, telemetry=telemetry)) as client:
        task_id = client.post("/tasks", json=_evaluation().payload(73), headers=headers).json()[
            "id"
        ]
        client.post(f"/tasks/{task_id}/process", headers=headers)
        task = repository.claim(300)
        assert task is not None and task.worker_token is not None
        assert repository.mark_model_call(task.id, task.worker_token)
        with repository._connect() as connection:
            connection.execute(
                "UPDATE resume_cloud_tasks SET lease_expires_at=now()-interval '1 second' "
                "WHERE id=%s",
                (task_id,),
            )

        def callback(identity: str, parent: str | None) -> None:
            # A separate connection must observe the committed transition.
            visible = ResumeCloudRepository(database).get(identity)
            assert visible is not None and visible.state.value == "NEEDS_REVIEW"
            _trace_recovery(telemetry, identity, parent)

        assert repository.recover_and_cleanup(callback) == 1
        assert repository.recover_and_cleanup(callback) == 0
    spans = exporter.get_finished_spans()
    api = next(span for span in spans if span.name == "api.tasks.create")
    recovered = [span for span in spans if span.name == "worker.recovery.needs_review"]
    assert len(recovered) == 1
    counters = telemetry.metrics.snapshot()["counters"]
    assert isinstance(counters, dict) and counters["manual_review_transitions"] == 1
    assert recovered[0].context.trace_id == api.context.trace_id
    assert recovered[0].parent.span_id == api.context.span_id
    result = repository.get(task_id)
    assert result is not None and result.state.value == "NEEDS_REVIEW"
    assert result.resume_docx is None and result.job_description is None


def test_console_exporter_outputs_safe_trace() -> None:
    pytest.importorskip("opentelemetry.sdk.trace")
    program = """
from soloscale.resume_cloud_observability import Telemetry
telemetry = Telemetry(enabled=True, exporter="console")
with telemetry.span("console.probe", {"task.id": "synthetic-task", "duration.ms": 1}):
    pass
telemetry.shutdown()
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        env={**os.environ, "RESUME_CLOUD_METRICS_EXPORTER": "none"},
    )
    assert result.returncode == 0, result.stderr
    assert "console.probe" in result.stdout and "synthetic-task" in result.stdout


def test_otlp_exporter_selection_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    exporter_module = pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    memory = pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")
    captured: list[str | None] = []
    exporter = memory.InMemorySpanExporter()

    def fake_exporter(*, endpoint: str | None = None) -> Any:
        captured.append(endpoint)
        return exporter

    monkeypatch.setattr(exporter_module, "OTLPSpanExporter", fake_exporter)
    telemetry = Telemetry(enabled=True, exporter="otlp", endpoint="http://127.0.0.1:4318/v1/traces")
    with telemetry.span("otlp.probe"):
        pass
    telemetry.shutdown()
    assert captured == ["http://127.0.0.1:4318/v1/traces"]
    assert len(exporter.get_finished_spans()) == 1
