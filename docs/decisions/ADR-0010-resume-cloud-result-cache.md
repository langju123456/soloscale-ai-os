# ADR-0010: Reuse only client-bound, source-versioned Resume cloud results

**Status:** Accepted
**Date:** 2026-11-01

## Context

Repeated generation requests can spend another model call for an identical result. Resume
content is private, so reuse must not cross caller partitions or extend its retention.

## Decision

Successful results are cached only after the fenced task completion commits. The cache key
uses hashes of the client label, raw uploaded bytes, stripped job description, mode, actual
provider/model/endpoint identity, and a source-derived generation-pipeline fingerprint.
Credentials never enter the key. A worker checks the cache after claiming its task and before
recording a model-call attempt. A hit completes the claimed task with the cache's original
absolute output expiry; it does not refresh or extend retention. Cache expiry is bounded by
the producing task's input and output TTLs, and bounded cleanup removes expired cached bytes.
Rows created before these hashes exist are not cacheable.

The global idempotency key remains unique. New rows retain a hashed client partition, so
replays require the same partition and payload; another client receives a conflict without
receiving the existing task. Migrated v2 rows have no recoverable partition: their NULL
partition retains global same-payload replay compatibility, is never backfilled on replay,
and their NULL content hashes keep them permanently uncacheable.

An unexpired cache entry is preserved on conflict, so a new task cannot slide its expiry.
An expired entry may be replaced by a newly completed result for the identical key.

## Alternatives

- A shared cache across client labels was rejected because the label is a privacy partition.
- A manual cache-version setting was rejected because source and embedded-prompt edits must
  invalidate automatically.
- Single-flight miss coalescing was deferred; concurrent misses may both generate safely.
