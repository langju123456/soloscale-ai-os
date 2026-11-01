"""Optional, metadata-only Resume cloud tracing."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from soloscale.resume_cloud_metrics import ResumeCloudMetrics

_ACTIVE_TELEMETRY: ContextVar[Telemetry | None] = ContextVar("resume_cloud_telemetry", default=None)
_TRACEPARENT = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")
P = ParamSpec("P")
T = TypeVar("T")


class Telemetry:
    def __init__(
        self,
        enabled: bool = False,
        exporter: str = "console",
        endpoint: str = "",
        provider: Any | None = None,
        metrics: ResumeCloudMetrics | None = None,
    ) -> None:
        self.enabled = enabled
        self._provider: Any | None = provider
        self._trace: Any | None = None
        self._propagate: Any | None = None
        self._closed = False
        metrics_endpoint = os.environ.get("RESUME_CLOUD_METRICS_OTLP_ENDPOINT", "") or endpoint
        if metrics_endpoint:
            metrics_endpoint = (
                metrics_endpoint[: -len("/v1/traces")] + "/v1/metrics"
                if metrics_endpoint.rstrip("/").endswith("/v1/traces")
                else metrics_endpoint.rstrip("/")
                if metrics_endpoint.rstrip("/").endswith("/v1/metrics")
                else metrics_endpoint.rstrip("/") + "/v1/metrics"
            )
        self.metrics = metrics or ResumeCloudMetrics.from_environment(
            os.environ.get("RESUME_CLOUD_METRICS_EXPORTER", "console").casefold(), metrics_endpoint
        )
        if not enabled:
            return
        from opentelemetry import propagate, trace
        if provider is None:
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

            provider = TracerProvider()
            if exporter == "otlp":
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

                processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint or None))
            else:
                processor = BatchSpanProcessor(ConsoleSpanExporter())
            provider.add_span_processor(processor)
        self._provider, self._trace, self._propagate = provider, trace, propagate
        self._tracer = provider.get_tracer("soloscale.resume-cloud")

    @classmethod
    def from_environment(cls) -> Telemetry:
        return cls(
            os.environ.get("RESUME_CLOUD_TRACING", "").casefold() in {"1", "true", "yes"},
            os.environ.get("RESUME_CLOUD_TRACE_EXPORTER", "console").casefold(),
            os.environ.get("RESUME_CLOUD_OTLP_ENDPOINT", ""),
        )

    @contextmanager
    def span(
        self,
        name: str,
        attributes: dict[str, str | int | bool] | None = None,
        parent: str | None = None,
    ) -> Iterator[Any | None]:
        if not self.enabled:
            yield None
            return
        assert self._trace is not None and self._propagate is not None
        context = (
            self._propagate.extract({"traceparent": parent})
            if parent is not None and _TRACEPARENT.fullmatch(parent) and parent[3:35] != "0" * 32
            else None
        )
        with self._tracer.start_as_current_span(
            name, context=context, record_exception=False, set_status_on_exception=False
        ) as span:
            for key, value in (attributes or {}).items():
                span.set_attribute(key, value)
            try:
                yield span
            except Exception:
                span.set_status(self._trace.Status(self._trace.StatusCode.ERROR, "failed"))
                raise

    def traceparent(self) -> str | None:
        if not self.enabled or self._propagate is None:
            return None
        carrier: dict[str, str] = {}
        self._propagate.inject(carrier)
        return carrier.get("traceparent")

    def wrap_gateway(self, gateway: Any) -> Any:
        telemetry = self

        class TracedGateway:
            descriptor = gateway.descriptor
            last_call_profile: Any | None = None

            def complete(
                self, schema: Any, *, system: str, user: str, reasoning_effort: str = "low"
            ) -> Any:
                started = time.monotonic()
                with telemetry.span(
                    "model.gateway.complete",
                    {
                        "model.name": self.descriptor.model or "",
                        "provider": self.descriptor.provider.value,
                    },
                ) as span:
                    try:
                        with telemetry.metrics.model_call():
                            result = gateway.complete(
                                schema, system=system, user=user, reasoning_effort=reasoning_effort
                            )
                        telemetry.metrics.observe_model_duration(
                            time.monotonic() - started, outcome="success"
                        )
                        self.last_call_profile = getattr(gateway, "last_call_profile", None)
                        if span is not None:
                            profile = self.last_call_profile
                            duration_ms = (
                                (profile.total_duration_ms or profile.wall_ms)
                                if profile is not None
                                else max(0, int((time.monotonic() - started) * 1000))
                            )
                            span.set_attribute("duration.ms", duration_ms)
                            span.set_attribute("outcome", "succeeded")
                        return result
                    except Exception as exc:
                        telemetry.metrics.observe_model_duration(
                            time.monotonic() - started, outcome="error"
                        )
                        details = getattr(exc, "details", None)
                        if span is not None:
                            duration_ms = max(0, int((time.monotonic() - started) * 1000))
                            span.set_attribute("duration.ms", duration_ms)
                            span.set_attribute("outcome", "failed")
                            if details is not None:
                                span.set_attribute("error.category", details.category.value)
                                span.set_attribute("retryable", details.retryable)
                        raise

        return TracedGateway()

    @contextmanager
    def activate(self) -> Iterator[None]:
        token = _ACTIVE_TELEMETRY.set(self)
        try:
            yield
        finally:
            _ACTIVE_TELEMETRY.reset(token)

    def shutdown(self) -> None:
        if self._provider is not None and not self._closed:
            self._closed = True
            self._provider.force_flush()
            self._provider.shutdown()
        self.metrics.shutdown()


def traced_stage(name: str) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Add a no-op-unless-active span without changing a pipeline function's result."""

    def decorate(function: Callable[P, T]) -> Callable[P, T]:
        @wraps(function)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
            telemetry = _ACTIVE_TELEMETRY.get()
            if telemetry is None:
                return function(*args, **kwargs)
            with telemetry.span(name):
                return function(*args, **kwargs)

        return wrapped

    return decorate
