# ADR-0009: Bound upstream model calls per worker process

**Status:** Accepted  
**Date:** 2026-11-01

## Context

One worker process may process multiple tasks concurrently, but an unconstrained number
of upstream model calls can exhaust provider capacity.

## Decision

Each worker process has `RESUME_CLOUD_MODEL_CONCURRENCY` process-local slots. The worker
thread-pool and service semaphore both use that one setting. This deliberately does not claim a global limit:
the aggregate maximum is worker-process count times this setting. A global limit would
need another shared coordinator and is deferred; PostgreSQL is retained only for durable
task ownership.

## Alternatives

- A global database semaphore was rejected for this slice because lease/failure cleanup
  would add a second distributed ownership protocol.
- Retrying provider calls was rejected: gateway retries remain zero and interruptions go
  to manual review.
