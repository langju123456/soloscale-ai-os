---
name: task-intake-and-routing
description: Normalize a high-level SoloScale request into a Task Envelope and composed Skill route. Use when an operator states an outcome without a full execution packet or when several registered workflows must be ordered.
---

# Task Intake and Routing

Status: `DRAFT` · Version: `0.1.0`.

## Contract

- Triggers: requests for task intake and routing.
- Non-triggers: requests outside this bounded workflow or requiring unapproved external action.
- Inputs: operator request.
- Outputs: Task Envelope and route; a private Run Receipt.
- Preconditions: canonical domain contract is available; repository state is inspectable; required approval is present when applicable.
- Owned capabilities: bounded preparation, validation, and receipt creation.
- Forbidden actions: expose private data, fabricate claims or model identities, silently mutate another domain, bypass a human gate.
- Risk class: `LOW`.
- Human gates: public publication, paid use, credential/permission changes, destructive operations, deployment, or the domain-specific gate.

## Route and validation

Model route: discovery `S2`; decision `S2`; implementation `S2`; verification `D0`; review `D3` when independent review is required. Escalate discovery or decision to `D3` only when ambiguity or cross-module risk remains. Record recommended and actual route without inventing identities.

Normalize the request; select exactly one primary Skill, ordered supporting Skills, phase route, gates, and expected receipts. Print TASK, SKILL ROUTE, MODEL ROUTE, HUMAN GATES, and EXPECTED OUTPUT, then continue unless an immediate gate applies.

Allowed tools: existing repository commands and the named canonical domain boundary only. Validation: input/output contract, hashes, and deterministic checks. Failure policy: stop on a missing gate or contract violation; after two low-confidence attempts, preserve hypotheses and escalate. Receipts record task envelope, version, models, tools, evidence IDs, hashes, checks, approvals, retries, status, timestamps, and external IDs where applicable. Examples: see `examples/example.md`.
