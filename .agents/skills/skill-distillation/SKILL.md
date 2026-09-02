---
name: skill-distillation
description: Propose a candidate reusable Skill or versioned improvement from a completed human-approved Run. Use when a workflow has repeated stable steps, contracts, failures, and reusable acceptance checks.
---

# Skill Distillation

Status: `DRAFT` · Version: `0.1.0`.

## Contract

- Triggers: requests for skill distillation.
- Non-triggers: requests outside this bounded workflow or requiring unapproved external action.
- Inputs: completed approved Run Receipt.
- Outputs: skill change proposal; a private Run Receipt.
- Preconditions: canonical domain contract is available; repository state is inspectable; required approval is present when applicable.
- Owned capabilities: bounded preparation, validation, and receipt creation.
- Forbidden actions: expose private data, fabricate claims or model identities, silently mutate another domain, bypass a human gate.
- Risk class: `MEDIUM`.
- Human gates: public publication, paid use, credential/permission changes, destructive operations, deployment, or the domain-specific gate.

## Route and validation

Model route: discovery `S2`; decision `D3`; implementation `S2`; verification `D0`; review `D3` when an update or promotion is considered. Record recommended and actual route without inventing identities. After two low-confidence failures, preserve hypotheses and escalate.

Evaluate repetition, stable steps, clear I/O, predictable failures, reusable checks, and approval. Never auto-modify ACTIVE definitions. Use DRAFT to CANDIDATE after first approved success; require a second representative success or explicit canonical approval for ACTIVE.

Allowed tools: existing repository commands and the named canonical domain boundary only. Validation: input/output contract, hashes, and deterministic checks. Failure policy: stop on a missing gate or contract violation; after two low-confidence attempts, preserve hypotheses and escalate. Receipts record task envelope, version, models, tools, evidence IDs, hashes, checks, approvals, retries, status, timestamps, and external IDs where applicable. Examples: see `examples/example.md`.
