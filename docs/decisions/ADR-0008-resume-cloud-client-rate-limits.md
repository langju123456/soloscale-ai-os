# ADR-0008: Partition Resume cloud request limits by client label

**Status:** Accepted  
**Date:** 2026-11-01

## Context

Concurrent API instances need one shared request budget, while the current cloud API has
one shared bearer token and no tenant identity.

## Decision

`POST /tasks` uses a PostgreSQL-backed token bucket keyed by `X-Client-ID`. A missing
header uses the `default` partition, so omission cannot bypass the limit. The capacity and
refill rate are environment-configurable. A single `INSERT ... ON CONFLICT DO UPDATE ...
RETURNING` spends/refills each existing bucket; the task insert resolves idempotency first,
then a newly inserted task consumes its bucket inside the same database transaction. A replay
or payload conflict therefore spends no token; a rejected new request rolls back both the task
and its bucket mutation. The update clamps backwards clocks and never moves the stored timestamp
backward. The API clock is assumed to have bounded NTP skew across instances. Invalid labels
return 400; rejected requests return 429 with `Retry-After`.

The label is a partition key only, not authentication. In this trusted-caller deployment,
a holder of the shared bearer token can spoof another label. A future multi-tenant service
must derive the partition from authenticated credentials.

## Alternatives

- Per-process memory was rejected because multiple API instances could each admit a burst.
- Redis was rejected because PostgreSQL is already the service's durable shared dependency.
- Rejecting missing labels was rejected to preserve existing callers; the default bucket
  gives those callers a bounded budget.
