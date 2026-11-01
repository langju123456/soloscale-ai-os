"""Process-local, metadata-only Resume cloud metrics."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

_COUNTERS = (
    "tasks_created",
    "tasks_replayed",
    "idempotency_conflicts",
    "task_create_rate_limited",
    "manual_review_transitions",
    "cache_lookups",
    "cache_hits",
)
_BUCKETS = (0.001, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 30.0, 120.0)


@dataclass
class _Histogram:
    buckets: dict[str, int] = field(default_factory=lambda: {str(value): 0 for value in _BUCKETS})
    count: int = 0
    total: float = 0.0
    minimum: float | None = None
    maximum: float | None = None

    def record(self, value: float) -> None:
        value = max(0.0, value)
        self.count += 1
        self.total += value
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        for bucket in _BUCKETS:
            if value <= bucket:
                self.buckets[str(bucket)] += 1

    def snapshot(self) -> dict[str, object]:
        return {
            "buckets": dict(self.buckets),
            "count": self.count,
            "sum": self.total,
            "min": self.minimum,
            "max": self.maximum,
        }


class ResumeCloudMetrics:
    """Fixed-memory collector that remains usable without OpenTelemetry installed."""

    def __init__(self, provider: Any | None = None, owned_provider: Any | None = None) -> None:
        self._lock = threading.Lock()
        self._counters = {name: 0 for name in _COUNTERS}
        self._histograms = {
            "queue_wait_seconds": _Histogram(),
            "model_duration_seconds.success": _Histogram(),
            "model_duration_seconds.error": _Histogram(),
        }
        self._model_active = 0
        self._model_peak = 0
        self._otel_counters: dict[str, Any] = {}
        self._otel_histograms: dict[str, Any] = {}
        self._otel_gauges: list[Any] = []
        self._owned_provider = owned_provider
        if provider is not None:
            from opentelemetry.metrics import Observation

            meter = provider.get_meter("soloscale.resume-cloud")
            self._otel_counters = {
                name: meter.create_counter(f"resume_cloud.{name}") for name in _COUNTERS
            }
            self._otel_histograms = {
                "queue_wait_seconds": meter.create_histogram(
                    "resume_cloud.queue_wait_seconds", unit="s"
                ),
                "model_duration_seconds": meter.create_histogram(
                    "resume_cloud.model_duration_seconds", unit="s"
                ),
            }
            self._otel_gauges = [
                meter.create_observable_gauge(
                    "resume_cloud.model_active",
                    callbacks=[lambda _: [Observation(self._gauge_value("model_active"))]],
                ),
                meter.create_observable_gauge(
                    "resume_cloud.model_peak",
                    callbacks=[lambda _: [Observation(self._gauge_value("model_peak"))]],
                ),
            ]

    @classmethod
    def from_environment(cls, exporter: str, endpoint: str = "") -> ResumeCloudMetrics:
        if exporter != "otlp":
            return cls()
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        provider = MeterProvider(
            metric_readers=[
                PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint or None))
            ]
        )
        return cls(provider=provider, owned_provider=provider)

    def shutdown(self) -> None:
        if self._owned_provider is not None:
            self._owned_provider.force_flush()
            self._owned_provider.shutdown()

    def increment(self, name: str) -> None:
        if name not in self._counters:
            raise ValueError(f"unknown Resume cloud metric: {name}")
        with self._lock:
            self._counters[name] += 1
        counter = self._otel_counters.get(name)
        if counter is not None:
            counter.add(1)

    def observe_queue_wait(self, seconds: float) -> None:
        self._observe("queue_wait_seconds", seconds)

    def observe_model_duration(self, seconds: float, *, outcome: str) -> None:
        if outcome not in {"success", "error"}:
            raise ValueError("model metric outcome must be success or error")
        self._observe(f"model_duration_seconds.{outcome}", seconds)
        histogram = self._otel_histograms.get("model_duration_seconds")
        if histogram is not None:
            histogram.record(max(0.0, seconds), {"outcome": outcome})

    def _observe(self, name: str, seconds: float) -> None:
        with self._lock:
            self._histograms[name].record(seconds)
        histogram = self._otel_histograms.get(name)
        if histogram is not None:
            histogram.record(max(0.0, seconds))

    @contextmanager
    def model_call(self) -> Iterator[None]:
        with self._lock:
            self._model_active += 1
            self._model_peak = max(self._model_peak, self._model_active)
        try:
            yield
        finally:
            with self._lock:
                self._model_active -= 1

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            lookups = self._counters["cache_lookups"]
            hits = self._counters["cache_hits"]
            return {
                "counters": dict(self._counters),
                "cache_hit_rate": (hits / lookups) if lookups else None,
                "histograms": {
                    name: histogram.snapshot() for name, histogram in self._histograms.items()
                },
                "gauges": {"model_active": self._model_active, "model_peak": self._model_peak},
            }

    def _gauge_value(self, name: str) -> int:
        with self._lock:
            return self._model_active if name == "model_active" else self._model_peak
