---
name: evidence-refresh-and-bundle
description: Refresh configured local sources and build a bounded metadata-safe EvidenceBundle. Use when Content, Career, Learning, or another workflow needs current approved evidence without exposing raw private bodies.
---

# Evidence Refresh and Bundle

Status: `CANDIDATE` · Version: `0.1.0`.

## Contract

- Triggers: requests for evidence refresh and bundle.
- Non-triggers: requests outside this bounded workflow or requiring unapproved external action.
- Inputs: configured source selection.
- Outputs: EvidenceBundle and refresh receipt; a private Run Receipt.
- Preconditions: canonical domain contract is available; repository state is inspectable; required approval is present when applicable.
- Owned capabilities: bounded preparation, validation, and receipt creation.
- Forbidden actions: expose private data, fabricate claims or model identities, silently mutate another domain, bypass a human gate.
- Risk class: `MEDIUM`.
- Human gates: public publication, paid use, credential/permission changes, destructive operations, deployment, or the domain-specific gate.

## Route and validation

Model route: discovery `D0`; decision `S2`; implementation `D0`; verification `D0`; review `D3` only for a fresh trust-boundary review. Record recommended and actual route without inventing identities. After two low-confidence failures, preserve hypotheses and escalate.

Use the existing EvidenceHub refresh and bundle boundaries. Do not call a model merely to refresh, expose raw private bodies, publish, or turn conversations alone into completed-work claims.

Allowed tools: existing repository commands and the named canonical domain boundary only. Validation: input/output contract, hashes, and deterministic checks. Failure policy: stop on a missing gate or contract violation; after two low-confidence attempts, preserve hypotheses and escalate. Receipts record task envelope, version, models, tools, evidence IDs, hashes, checks, approvals, retries, status, timestamps, and external IDs where applicable. Examples: see `examples/example.md`.
