"""Optional, metadata-only Resume cloud tracing."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any, ParamSpec, TypeVar

_ACTIVE_TELEMETRY: ContextVar[Telemetry | None] = ContextVar("resume_cloud_telemetry", default=None)
_TRACEPARENT = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")
P = ParamSpec("P")
T = TypeVar("T")


class Telemetry:
    def __init__(
        self, enabled: bool = False, exporter: str = "console", endpoint: str = ""
    ) -> None:
        self.enabled = enabled
        self._provider: Any | None = None
        self._trace: Any | None = None
        self._propagate: Any | None = None
        if not enabled:
            return
        from opentelemetry import propagate, trace
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
    ) -> Iterator[None]:
        if not self.enabled:
            yield
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
                yield
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
                with telemetry.span(
                    "model.gateway.complete",
                    {
                        "model.name": self.descriptor.model or "",
                        "provider": self.descriptor.provider.value,
                    },
                ):
                    try:
                        result = gateway.complete(
                            schema, system=system, user=user, reasoning_effort=reasoning_effort
                        )
                        self.last_call_profile = getattr(gateway, "last_call_profile", None)
                        return result
                    except Exception as exc:
                        details = getattr(exc, "details", None)
                        if details is not None:
                            # GatewayFailureDetails intentionally contains no response content.
                            with telemetry.span(
                                "model.gateway.failure",
                                {
                                    "error.category": details.category.value,
                                    "retryable": details.retryable,
                                },
                            ):
                                pass
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
        if self._provider is not None:
            self._provider.shutdown()


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
