---
name: editorial-package
description: Create a private evidence-backed editorial package with Writer, fresh Reviewer, and controlled Reviser provenance. Use for Canonical Story, LinkedIn, or X drafts that must stop before publication.
---

# Editorial Package

Status: `DRAFT` · Version: `0.1.0`.

## Contract

- Triggers: requests for editorial package.
- Non-triggers: requests outside this bounded workflow or requiring unapproved external action.
- Inputs: EvidenceBundle and requested channels.
- Outputs: private package and editorial receipt; a private Run Receipt.
- Preconditions: canonical domain contract is available; repository state is inspectable; required approval is present when applicable.
- Owned capabilities: bounded preparation, validation, and receipt creation.
- Forbidden actions: expose private data, fabricate claims or model identities, silently mutate another domain, bypass a human gate.
- Risk class: `MEDIUM`.
- Human gates: public publication, paid use, credential/permission changes, destructive operations, deployment, or the domain-specific gate.

## Route and validation

Model route: discovery `D3` when ambiguous; decision `D3`; implementation `S2`; verification `D0`; review `D3` when independent review is required. Record recommended and actual route without inventing identities. After two low-confidence failures, preserve hypotheses and escalate; otherwise downshift once uncertainty is removed.

Delegate to Content Studio. Preserve writer, reviewer, reviser, prompt identity, actual model identity, hashes, and review provenance. A fresh reviewer must be independent; human fact-check remains required before public use.

Allowed tools: existing repository commands and the named canonical domain boundary only. Validation: input/output contract, hashes, and deterministic checks. Failure policy: stop on a missing gate or contract violation; after two low-confidence attempts, preserve hypotheses and escalate. Receipts record task envelope, version, models, tools, evidence IDs, hashes, checks, approvals, retries, status, timestamps, and external IDs where applicable. Examples: see `examples/example.md`.
