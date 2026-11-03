# DevLog — 2026-11-03 — Review Workbench

## Problem

Evidence Agent results are candidates that require human confirmation, but there was no
place to record a per-claim decision, keep the model's original wording after an edit, or
see whether a cited passage had changed since the model read it.

## Decision

Add a local Review Workbench (ADR-0008): a server-enforced state machine over one item per
claim, a rule-based evidence status derived from existing citation lineage instead of an
invented confidence score, append-only text versions and review log, optimistic
concurrency with `expected_revision`, SQLite schema triggers for the history invariants,
a loopback FastAPI service, and a Next.js client. No model call, authentication, queue or
deployment was added.

## Verification (observed on 2026-11-03, Linux, Python 3.12.3, Node 22.23.4)

| Command | Result |
| --- | --- |
| `ruff check .` | exit 0, all checks passed |
| `pytest -q` | exit 0, 771 passed, 10 skipped (baseline on `main`: 720 passed, 10 skipped) |
| `pytest -q tests/test_review_workbench.py` | 51 passed |
| `mypy src tests` (mypy 1.20.2) | exit 1, 7 errors, all in `resume_docx.py`, identical to `main` before this change; 0 in new or changed files |
| `mypy src tests` (mypy 1.18.2) | 2 errors, both in `creator_accounts.py`, also present on `main` |
| `python -m build` | sdist and wheel built; wheel contains `review_migrations/0001_init.sql` |
| `npm ci`, `npm run typecheck`, `npm test`, `npm run build` | all exit 0; vitest 9 passed; Next.js 16.4.1 static build |

The mypy failures are not introduced by this branch: a fresh environment resolves newer
mypy/pydantic versions than the 0.4.2 closeout used. They are left for a separate change.

Live run: `python -m soloscale.review_sample` created a sample run, and
`python -m soloscale.review_api` served it on `127.0.0.1:8767`. Observed with curl: import
201 (two `PENDING_REVIEW`, three `NEEDS_REVIEW`), re-import 200 `created: false`, confirm
of a verified claim 200, correction 200 with version 1 kept, second confirm
`409 INVALID_TRANSITION`, correction with an old revision `409 STALE_REVISION`, confirm of
a changed source `409 CONFIRM_BLOCKED_HASH_MISMATCH`, confirm without note `422
NOTE_REQUIRED`, mixed batch `409` with the verified item left pending, foreign Host `400`.
In the browser (Playwright against `next start`), a correction submitted after another
client rejected the item showed the stale-revision error inline and kept the unsaved
text; import from an empty queue and a noted confirmation of a partly verified claim
also worked. The first browser pass logged the expected 409 and a missing-favicon 404
(fixed); the final pass logged no console errors. At 390 px width the page did not scroll
horizontally.

## Defects found while building

- `npm install` crashed inside npm's arborist (`reading 'edgesOut'`) while resolving
  vitest 4.1's peer range for vite 8. vitest 3.2.7 with vite 7.3.7 pinned installs cleanly.
- The first confirm-time re-check only looked at citations that were `VERIFIED` at import;
  it now refuses any citation whose check got weaker, including a revised document.
- Screenshot review found a raw enum in the import log entry, an uneven filter wrap and a
  missing favicon; all fixed before commit.

## Remaining risks

Evidence status is structural, not semantic. Reviewer names are unauthenticated labels.
The confirm-time re-check and the write span two SQLite files. Frontend checks are not
yet in CI.
