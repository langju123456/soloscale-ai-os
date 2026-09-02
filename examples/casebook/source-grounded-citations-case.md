# Sanitized case evidence: source-grounded citations

## Problem

The research assistant could answer from retrieved context but had no strict
public citation contract. Adding citations risked exposing raw retrieval
metadata, showing sources different from the evidence used in the prompt,
breaking the legacy string API, and duplicating model or memory side effects.

## Resolution

- Preserved the legacy `get_response` string API.
- Added a structured response API over one shared orchestration path.
- Built prompt context and public citations from the same validated evidence
  sequence.
- Normalized source lineage and rejected conflicting identities.
- Rendered exact-schema structured responses while preserving legacy history.

## Remaining risk

A pre-existing persisted-FAISS cold-start defect was outside the feature diff
and remained tracked separately. The original review-receipt sequence was also
preserved as a historical process deviation rather than rewritten.

This file is a deliberately sanitized example. It is not a raw conversation,
private terminal transcript, or replacement for the underlying receipts.
