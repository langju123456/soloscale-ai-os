# ADR-0012: Process-local Resume cloud metrics

**Status:** Accepted
**Date:** 2026-11-01

## Decision

Resume cloud processes retain a fixed-size, thread-safe metadata-only metrics collector.
It counts task creation outcomes, manual-review transitions, eligible cache lookups and
fenced hits, and records queue wait plus successful and failed model-call durations.
It never labels metrics with task, client, model, error, or resume content. The collector
resets on process restart; it is not a distributed service metric.

`/metrics` is authenticated and returns a JSON snapshot for the API process. Workers log
the same snapshot when configured with `RESUME_CLOUD_METRICS_EXPORTER=console`; `none`
suppresses that log and `otlp` uses an optional exporter. `RESUME_CLOUD_METRICS_INTERVAL_SECONDS`
controls the worker's emission cadence. OTLP imports and provider setup are lazy and explicit.
`RESUME_CLOUD_METRICS_OTLP_ENDPOINT` is the explicit metrics endpoint. If it is absent,
the trace endpoint is converted from `/v1/traces` to `/v1/metrics` (or a base endpoint
gets that suffix), so metrics are never sent to a trace endpoint.

The Compose default builds `cloud,observability` because tracing or OTLP metrics are optional
at runtime but require those installed extras when enabled. The package and Dockerfile defaults
remain usable without observability, and tracing remains disabled by default.
