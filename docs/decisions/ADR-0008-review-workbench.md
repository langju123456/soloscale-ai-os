# ADR-0008: Human Review Workbench over Evidence Agent drafts

**Status:** Accepted for local candidate implementation
**Date:** 2026-11-03

## Context

The Evidence Agent (ADR-0004) produces a private `CANDIDATE_REQUIRES_HUMAN_CONFIRMATION`
result. Each claim cites chunk IDs, and the run receipt keeps the exact excerpt, chunk hash
and document hash the model saw. Until now there was no structured way for a person to
record a decision about an individual claim: confirm it, correct it while keeping the
model's wording, or reject it, with an auditable history.

Three properties matter more than UI polish:

1. A model output must not become accepted because a client skipped a check.
2. Correcting a claim must not destroy the evidence of what the model originally said.
3. A reviewer must see when the cited evidence has changed since the model read it.

## Decision

1. **One review item per claim.** Import reads a completed run's `04_result.json`, verifies
   it against its own `00_input.json` and `03_retrieval_manifest.json` hashes, and requires
   every cited excerpt to equal the excerpt recorded as model-visible. An inconsistent
   receipt is not imported. Item identity is `<run_id>.c<claim_index>`, so a replayed import
   is a no-op and a receipt changed after import is refused.
2. **Rule-based evidence status, no confidence number.** The agent produces no calibrated
   confidence, so none is invented. At import each cited chunk is re-resolved in the
   current KnowledgeStore and classified (`VERIFIED`, `DOCUMENT_REVISED`, `HASH_MISMATCH`,
   `INTEGRITY_FAILED`, `SOURCE_MISSING`, `NOT_IN_RECEIPT`, `UNCHECKED`). A fixed rule maps
   those to `FULLY_VERIFIED`, `PARTIALLY_VERIFIED`, `UNVERIFIED` or `HASH_MISMATCH`.
   Anything not fully verified starts in `NEEDS_REVIEW`.
3. **Server-enforced state machine.** `PENDING_REVIEW`/`NEEDS_REVIEW` may move to
   `CONFIRMED`, `CORRECTED` or `REJECTED`; a decided item may only be reopened. Correct,
   reject and reopen require a note. Confirming non-verified evidence requires a note.
   `HASH_MISMATCH` can never be confirmed. Batch confirmation accepts only fully verified
   pending items and is atomic. Confirm re-checks citations at action time and refuses if
   any is weaker than at import. The UI renders `allowed_actions` from the server; it
   cannot widen them.
4. **Append-only versions and log.** Version 1 is always the model text. A correction
   appends a version with reviewer, note and timestamp. Every action appends a review
   event. Diffs are computed by the server between any two stored versions.
5. **Optimistic concurrency.** Every action names `expected_revision`. The update is
   `WHERE revision = ?`; a mismatch returns `409 STALE_REVISION` with the current revision
   and writes nothing. This follows the conditional-update pattern already used by the
   Resume Cloud worker token and lease checks.
6. **Local SQLite, schema-level invariants.** `.soloscale/review/workbench.sqlite3` uses the
   same conventions as the KnowledgeStore: `0600` file, `0700` directory, symlink
   rejection, `DELETE` journal, `PRAGMA user_version` gating and a migration applied
   atomically to an empty database. Triggers and CHECK constraints keep runs, citation
   snapshots, versions and events append-only, require revisions to advance by one, keep
   evidence status immutable, and forbid `CONFIRMED` with `HASH_MISMATCH` for any writer,
   not only the Python store.
7. **Loopback API.** FastAPI on `127.0.0.1:8767`, a Host allowlist (DNS-rebinding defense
   for private excerpts), CORS only for the local UI origin, a streamed request-body limit
   reused from the Resume API, and zero model calls.

## Consequences

- A claim cannot reach `CONFIRMED` without passing the server rules, and the stored text
  of what the model said cannot be edited through the store or by direct SQL.
- Evidence drift is visible per citation and blocks confirmation where it matters.
- The status is structural: it proves the cited text is the text the model saw, not that
  the text semantically supports the claim. A human still makes that judgment.
- The confirm-time re-check and the write are not one transaction across the two SQLite
  files; a source synced in that window is not detected. Acceptable for one local operator.
- `reviewer` is a free-text label, not an authenticated identity.

## DEFERRED_PRODUCTION_HARDENING

Authentication and reviewer identity, multi-tenancy, role-based approval (for example
two-person rule for high-risk fields), cross-database transactional evidence checks,
per-field (structured) correction instead of whole-claim text, export of decided claims
to downstream products, retention/deletion policy, and hosted deployment.
