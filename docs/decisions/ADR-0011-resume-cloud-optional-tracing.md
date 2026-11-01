# ADR-0011: Optional metadata-only Resume cloud tracing

**Status:** Accepted
**Date:** 2026-11-01

## Problem

API and worker execute in separate processes; task IDs alone do not show where a request
waited or failed. Diagnosis must preserve the existing content-free gateway contract.

## Decision

Tracing is off by default. When `RESUME_CLOUD_TRACING=true`, a process-local
OpenTelemetry provider exports to console or a configured OTLP/HTTP endpoint. Trace context
is persisted with a task so worker processes continue the request trace. Attributes contain
only allowlisted identifiers, model/provider metadata, durations, and sanitized gateway
categories; resume text, JD text, prompts, schemas, outputs, headers, secrets, and exception
messages are excluded.

The API records created, replayed, and rejected idempotency outcomes on its one create span.
After a worker claims a task, it restores the persisted trace context for a claim span, then
executes pipeline, gateway, and terminal spans beneath that continuation. Fenced completion
and failure transitions record their actual terminal result; recovery emits a separate
`NEEDS_REVIEW` span from the persisted task context. API and worker shutdown flush their
process-local provider. No global provider is installed.

## Alternatives

Automatic instrumentation was rejected because it can capture HTTP bodies or headers. A
required collector was rejected because the cloud service must keep its no-infrastructure,
default-off operation.

FastAPI framework telemetry is explicitly disabled, including automatic exporter setup and
exception logging. Only the manual, allowlisted spans are exported. Acceptance tests cross a
real subprocess boundary and check console/OTLP selection, cache completion, recovery after
commit, optional SDK loading, and sentinel content exclusion.
