# ADR-0013: Evaluate Resume Cloud concurrency with a disposable fake gateway

**Status:** Accepted  
**Date:** 2026-11-01

## Decision

The evaluation harness uses an empty loopback PostgreSQL database whose name is limited to
`resume_service_eval` with an optional suffix. It starts two local API subprocesses and two
or more worker subprocesses. Each worker owns one `ResumeCloudService` and uses a thread pool
with the configured process-local concurrency. A fake object implementing the existing
`ModelGateway` protocol is passed only to `process_one`; production provider configuration is
unchanged.

The fake validates the real structured-output schema, waits for a configured delay, and records
only ordinal, process ID, monotonic timing, and a safe error category in a private JSONL audit.
The report derives observed peak calls, task states, request results, and latency samples from
that run. It makes no capacity or production-performance claim.

## Consequences

The harness exercises the canonical AI rendering pipeline without credentials or network calls.
It is intentionally not a deployment, migration, interruption, or provider-quality evaluation.
