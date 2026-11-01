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

The opt-in `compose.resume-cloud-eval.yaml` override publishes PostgreSQL only on loopback
(port 55439 by default). Start only its `postgres` service and create a fresh evaluation
database with `createdb` inside that container; the README provides the complete commands.
The production Compose file continues to keep PostgreSQL off the host network.

The fake validates the real structured-output schema, waits for a configured delay, and records
only ordinal, process ID, monotonic timing, and a safe error category in a private JSONL audit.
The report derives observed peak calls, task states, request results, and latency samples from
that run. It makes no capacity or production-performance claim.

## Consequences

The harness exercises the canonical AI rendering pipeline without credentials or network calls.
It is intentionally not a deployment, migration, interruption, or provider-quality evaluation.

## Problem and alternatives

The earlier template evaluator deliberately makes zero model calls, so it cannot establish
an upstream concurrency bound. A task's `model_call_attempted` flag is also insufficient
proof of the number or overlap of actual gateway calls.

This evaluator sends simultaneous HTTP requests to two API processes, including same-key
replays and a shared-client burst. An evaluation-only PostgreSQL trigger records each
terminal transition, including an attempted repeated completion timestamp update. Per-worker
JSONL files contain completed gateway call intervals; their overlap gives per-process and
aggregate peaks. Worker scheduling does not affect the seeded error decision, which uses a
hash of the synthetic JD. Cache control calls explicitly use zero error rate. A same-client
cache hit must add zero calls; the same content under another client must add one call.

Rejected alternatives:

- Paid model load tests spend money and introduce unbounded external variance.
- Replacing the entire generation pipeline does not prove the real gateway boundary.
- A fake HTTP endpoint adds unrelated TLS setup; production endpoints correctly require HTTPS.
- Attempt flags or worker completion logs alone cannot detect a second committed completion.
- A global semaphore or new queue/cache infrastructure would change the service under test.

Reports retain measured samples, source hashes, assertions, and process metadata in a private
ignored directory. They never retain the synthetic resume, JD, model response, DSN, or bearer
token. Failure produces a nonzero exit status and a partial report with a safe failure category.
Only the evaluator's own child processes are stopped; databases and volumes are preserved.
