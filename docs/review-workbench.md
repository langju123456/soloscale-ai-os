# Review Workbench

The Review Workbench is where a person decides, claim by claim, whether an Evidence Agent
draft may be used. The model proposes claims with citations; code checks the citations;
a person confirms, corrects or rejects; the server enforces the rules and keeps the
history. Design decision record: [ADR-0008](decisions/ADR-0008-review-workbench.md).

## Pieces

| Piece | File | Job |
| --- | --- | --- |
| Intake | `src/soloscale/review_intake.py` | Read a completed agent run, verify its receipts, re-check each citation in the KnowledgeStore |
| Rules | `src/soloscale/review_workflow.py` | Evidence-status rule, state machine, note rules, diff |
| Store | `src/soloscale/review_store.py` | SQLite persistence, optimistic concurrency, confirm-time re-check |
| Schema | `src/soloscale/review_migrations/0001_init.sql` | Tables, CHECK constraints, append-only triggers |
| API | `src/soloscale/review_api.py` | Loopback FastAPI app; maps rule errors to stable codes |
| Sample | `src/soloscale/review_sample.py` | Builds a labelled sample run through the real agent loop |
| UI | `web/review-workbench/` | Next.js client: queue, item view, history, diff |

## Data model

```text
review_runs            one imported agent run: question, model, receipt hashes, gaps
  └─ review_items      one per claim: state, evidence_status, revision, current_version
       ├─ review_citations      per cited chunk: excerpt the model saw, recorded hashes,
       │                        check result and current hashes at import (immutable)
       ├─ review_text_versions  v1 = model text; v2.. = reviewer corrections (append-only)
       └─ review_events         review log: action, from/to state, from/to revision,
                                reviewer, note, time (append-only)
```

`revision` counts every change to an item and is what clients send back as
`expected_revision`. `current_version` points at the text version currently shown.

## From draft to review item

1. `BoundedEvidenceAgent.run()` writes `00_input.json`, `03_retrieval_manifest.json` and
   `04_result.json` under `.soloscale/knowledge/agent-runs/<run_id>/`.
2. `POST /review/imports {run_id}` loads `04_result.json` as `AgentRunResult`, then:
   - recomputes the input and retrieval manifest hashes and compares them with the hashes
     inside the result;
   - requires each cited reference's excerpt, chunk hash and document ID to equal the
     manifest entry recorded as `in_context` (what the model actually saw);
   - re-resolves each cited chunk with `KnowledgeStore.get_chunks()` and compares hashes.
3. Each claim gets an evidence status from its citation checks:

   | Citation checks | Evidence status | Initial state |
   | --- | --- | --- |
   | every chunk and document hash matches | `FULLY_VERIFIED` | `PENDING_REVIEW` |
   | some chunk text matches, not all verify (e.g. document revised) | `PARTIALLY_VERIFIED` | `NEEDS_REVIEW` |
   | nothing matches (missing, no index, not in receipt) | `UNVERIFIED` | `NEEDS_REVIEW` |
   | any chunk hash changed or index integrity failed | `HASH_MISMATCH` | `NEEDS_REVIEW` |

   There is no numeric confidence because the pipeline does not produce one.
4. One transaction inserts the run, items, version 1, citation snapshots and an
   `IMPORTED` event. Re-importing the same receipt returns the existing items (200).

## State machine

```text
PENDING_REVIEW ─┬─ confirm ─▶ CONFIRMED ─┐
NEEDS_REVIEW  ──┼─ correct ─▶ CORRECTED ─┼─ reopen (note) ─▶ PENDING_REVIEW or NEEDS_REVIEW
                └─ reject  ─▶ REJECTED  ─┘                   (chosen by evidence status)
```

| Rule | Enforced in |
| --- | --- |
| Only the edges above exist | `plan_transition()`; trigger `review_items_state_edges` |
| Correct, reject, reopen need a note | `plan_transition()`; a CHECK also requires a note on every correction version |
| Confirming non-verified evidence needs a note | `plan_transition()` |
| `HASH_MISMATCH` is never confirmed | `plan_transition()`; CHECK on `review_items` |
| Batch confirm only for fully verified pending items, all-or-nothing | `ReviewStore.confirm_batch()` (one transaction) |
| Confirm refuses if a citation got weaker after import | `ReviewStore._require_evidence_unchanged()` |
| Stale edits rejected | `UPDATE ... WHERE revision = ?`; trigger requires `revision + 1` |
| Model text and history never change | triggers on versions, citations, events, runs |

The UI only shows the `allowed_actions` and `confirm_requirement` the server returns.
Every rule is also covered by API-level tests, so a client that skips the UI gets the
same rejections.

## Failure modes and how they surface

| Situation | Response |
| --- | --- |
| Action no longer allowed (item already decided) | `409 INVALID_TRANSITION` |
| Edit based on an old revision (second tab, other reviewer) | `409 STALE_REVISION` with `current_revision`; UI keeps the unsaved text and offers reload |
| Confirm on changed source | `409 CONFIRM_BLOCKED_HASH_MISMATCH` |
| Source re-synced after import, then confirm | `409 EVIDENCE_CHANGED` with chunk and current check |
| Missing note | `422 NOTE_REQUIRED` |
| Batch includes a low-confidence item | `409 LOW_CONFIDENCE_REQUIRES_INDIVIDUAL_REVIEW`; nothing confirmed |
| Receipt edited or inconsistent | `422 RECEIPT_INTEGRITY_FAILED`; not imported |
| Receipt changed after import | `409 DRAFT_CHANGED_SINCE_IMPORT` |
| Failed agent run | `422 DRAFT_INVALID` |
| Non-loopback `Host` header / body over 64 KiB | `400` / `413` |
| API not running | UI shows "Review API unreachable" with the start command |

## Run locally

Backend (from the repository root, Python 3.11+):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,api]'
python -m soloscale.review_sample --data-root .soloscale/review-demo   # optional sample
python -m soloscale.review_api --data-root .soloscale/review-demo      # 127.0.0.1:8767
```

For real drafts, point `--data-root` at the data root used by `soloscale evidence-agent`
(default `.soloscale`).

Frontend (Node 20.9+):

```bash
cd web/review-workbench
npm ci
npm run build && npm start        # or: npm run dev
# open http://127.0.0.1:3000 and use "Import drafts"
```

`NEXT_PUBLIC_REVIEW_API_BASE_URL` changes the API URL (read at build time).
`SOLOSCALE_REVIEW_ALLOWED_ORIGINS` (comma-separated) changes the API's CORS origins.

Checks: `pytest -q tests/test_review_workbench.py`, and in `web/review-workbench/`
`npm run typecheck`, `npm test`, `npm run build`.

## About the sample run

`review_sample` is demo and test data, not a user's history. It syncs four synthetic
documents from `tests/fixtures/review_workbench_sample.json` into a real KnowledgeStore,
runs the real `BoundedEvidenceAgent` with a scripted reasoner (no LLM) whose answers come
from the fixture, then edits three sources in the index so the five claims land in all
four evidence statuses. The question, model name (`fixture/scripted-reasoner-no-llm`) and
source titles are labelled as sample data. It reads the fixture from the source checkout,
so it is not usable from an installed wheel.

## What this does not do

- It does not judge whether a passage semantically supports a claim. The status shows
  only that the cited text is the text the model saw.
- `reviewer` is a free-text label, not a login.
- The confirm-time re-check and the write are separate SQLite files, so a sync in between
  is not detected.
- Decided claims are not yet exported to Casebook, Resume or Content.

`DEFERRED_PRODUCTION_HARDENING`: authentication and reviewer identity, multi-tenancy,
role-based or two-person approval, structured per-field corrections, export of decided
claims, retention policy, CI for the frontend, and hosted deployment.
