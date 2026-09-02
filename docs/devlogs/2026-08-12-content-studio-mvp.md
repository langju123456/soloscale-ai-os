# Content Studio MVP — local implementation record

## Problem

Resume and Learning were reachable in the local product, but turning verified engineering
work into LinkedIn, X, and video candidates still required manual file and prompt assembly.

## Decision

Add one local `/content` vertical slice that accepts a small operator-classified claim
ledger and deterministically renders three reviewable channel candidates. Keep external
publishing behind a separate human action and preserve BuildLog as the downstream
publishing system.

## Boundaries

- `VERIFIED` and `OBSERVED` claims require receipts.
- `HYPOTHESIS` and `PLANNED` remain visibly classified.
- Every factual draft block retains a claim ID.
- Private absolute paths and common credential shapes fail before a run is created.
- Runs are private, non-overwriting, and stored under `.soloscale/content-runs/`.
- No model, network, account connection, deployment, or publishing action occurs.

## Local verification

The implementation adds strict content contracts, deterministic renderers, a local
Content Studio page, addressable result previews, copy controls, bounded downloads, and
tests for storage, lineage, privacy, repeat runs, UI output, and error handling.

- Targeted Content/UI gate: 27 passed.
- Full suite: 296 passed.
- Ruff: passed.
- Strict mypy: passed across 50 source files.
- Offline sdist and wheel build: passed.
- Browser E2E: PR #8 evidence → addressable run → LinkedIn/X/video previews → copy →
  bounded download; refresh preserved the result and restored the input.
- Storage receipt: ten private artifacts with `0700` run-directory and `0600` file modes.
