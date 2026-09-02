# DevLog — 2026-08-11 — Learning Traceability golden case

## Problem

Existing evidence systems could not expose one inspectable reasoning → decision → code →
test → learning → interview → claim-safety path.

## Evidence before change

Conversation RAG already had deterministic chunking, fused retrieval, lineage validation,
and focused tests. Casebook had ordered mastery stages, while Resume Workspace already kept
retrieval candidates outside career facts. The local UI had no unified learning view.

## Decision

Compile only the Conversation RAG chunking/retrieval case. Resolve anchors with Git and the
Python AST, preserve unknown attribution, start mastery at L0, and write private artifacts.

## Alternatives considered

- Index every repository capability: rejected as unbounded.
- Copy raw private conversations into a tracked truth store: rejected by the privacy gate.
- Replace the local UI with an SPA: rejected because the existing server is sufficient.

## Implementation

Added shared contracts, a deterministic selected-case compiler, twelve private run
artifacts, an evidence-hash cache, a clickable graph, safe bounded source excerpts,
Explain/Trace starts, JD relevance, and a strict claim-eligibility view.
Added real Explain/Trace response fields and non-overwriting private pending-review
receipts. Submission leaves the immutable run mastery snapshot at L0.
Fixed the response-save navigation with POST/Redirect/GET: successful saves return to the
originating exercise anchor, show an inline confirmation, and refresh as a safe GET instead
of resubmitting the response.

## Verification

- targeted Learning/UI suite: 18 passed.
- `pytest -q`: 263 passed.
- `ruff check .`: passed.
- strict `mypy src tests`: passed across 43 source files.
- The prior candidate's isolated `uv build` passed. The response-input rerun could not
  re-resolve `wheel` without network, and the shared venv lacks `setuptools`; no dependency
  was installed, so packaging was not reverified for this follow-up.
- `git diff --check` and `.soloscale` ignore checks: passed.
- Browser E2E built a 25-node/27-edge private run, selected the Chunking Concept,
  exposed PROJECT/DECISION/CODE/TEST/MASTERY traceability, opened the recorded
  `_text_segments` line range, and started Explain and Trace with no console errors.
- Browser E2E then submitted an Explain response, received a
  `SUBMITTED_REQUIRES_REVIEW` acknowledgment, and confirmed the real mastery artifact
  remained `L0 Seen` with no receipt IDs.
- Response-navigation E2E observed `POST /learning/respond` → `303` → anchored GET,
  retained the inline confirmation in the exercise viewport, and confirmed a manual
  refresh issued only GET while the private receipt count remained unchanged.

## Result

One real engineering case is runnable without network/model calls or automatic mastery and
claim promotion.

## Remaining risks

Raw case-specific conversation evidence, contribution receipts, fresh CI evidence, and
operator mastery receipts remain absent. The follow-up packaging build remains unverified
for the local reason recorded above. Production hardening is deliberately deferred.

## Lesson

Engineering evidence, personal ownership, mastery, and career claims must be separate gates.

## Content candidates

- LinkedIn thesis: A green test suite does not prove interview ownership.
- Visual: One evidence graph with separate engineering, mastery, and claim gates.
