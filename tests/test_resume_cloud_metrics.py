from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest

from soloscale.resume_cloud_metrics import ResumeCloudMetrics
from soloscale.resume_cloud_observability import Telemetry


def test_collector_is_fixed_shape_thread_safe_and_has_nullable_empty_hit_rate() -> None:
    metrics = ResumeCloudMetrics()

    def update() -> None:
        for _ in range(100):
            metrics.increment("tasks_created")
            metrics.observe_queue_wait(0.01)

    threads = [threading.Thread(target=update) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    snapshot = metrics.snapshot()
    assert snapshot["counters"] == {
        "tasks_created": 800,
        "tasks_replayed": 0,
        "idempotency_conflicts": 0,
        "task_create_rate_limited": 0,
        "manual_review_transitions": 0,
        "cache_lookups": 0,
        "cache_hits": 0,
    }
    assert snapshot["cache_hit_rate"] is None
    assert len(snapshot["histograms"]["queue_wait_seconds"]["buckets"]) == 9  # type: ignore[index]


def test_gateway_metrics_wrap_actual_call_and_balance_after_error() -> None:
    class Gateway:
        descriptor = SimpleNamespace(model="synthetic", provider=SimpleNamespace(value="test"))

        def complete(self, *_: Any, **__: Any) -> object:
            raise RuntimeError("synthetic")

    telemetry = Telemetry(metrics=ResumeCloudMetrics())
    with pytest.raises(RuntimeError, match="synthetic"):
        telemetry.wrap_gateway(Gateway()).complete(object, system="safe", user="safe")
    snapshot = telemetry.metrics.snapshot()
    assert snapshot["gauges"] == {"model_active": 0, "model_peak": 1}
    assert snapshot["histograms"]["model_duration_seconds.error"]["count"] == 1  # type: ignore[index]
    assert snapshot["histograms"]["model_duration_seconds.success"]["count"] == 0  # type: ignore[index]


def test_collector_import_and_default_use_do_not_require_opentelemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    original_import: Any = builtins.__import__

    def blocked(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("opentelemetry"):
            raise ImportError("blocked")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    metrics = ResumeCloudMetrics()
    metrics.increment("tasks_created")
    assert metrics.snapshot()["counters"]["tasks_created"] == 1  # type: ignore[index]


def test_in_memory_otel_reader_receives_fixed_metric_names() -> None:
    pytest.importorskip("opentelemetry.sdk.metrics")
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    metrics = ResumeCloudMetrics(provider=provider)
    metrics.increment("tasks_created")
    metrics.observe_queue_wait(0.1)
    with metrics.model_call():
        provider.force_flush()
    provider.force_flush()
    data = reader.get_metrics_data()
    assert data is not None
    names = {
        metric.name
        for scope in data.resource_metrics[0].scope_metrics
        for metric in scope.metrics
    }
    assert names == {
        "resume_cloud.tasks_created",
        "resume_cloud.queue_wait_seconds",
        "resume_cloud.model_active",
        "resume_cloud.model_peak",
    }
