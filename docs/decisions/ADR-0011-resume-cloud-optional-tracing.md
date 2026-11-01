# ADR-0011: Optional metadata-only Resume cloud tracing

**Status:** Accepted
**Date:** 2026-11-01

## Decision

Tracing is off by default. When `RESUME_CLOUD_TRACING=true`, a process-local
OpenTelemetry provider exports to console or a configured OTLP/HTTP endpoint. Trace context
is persisted with a task so worker processes continue the request trace. Attributes contain
only allowlisted identifiers, model/provider metadata, durations, and sanitized gateway
categories; resume text, JD text, prompts, schemas, outputs, headers, secrets, and exception
messages are excluded.

## Alternatives

Automatic instrumentation was rejected because it can capture HTTP bodies or headers. A
required collector was rejected because the cloud service must keep its no-infrastructure,
default-off operation.
