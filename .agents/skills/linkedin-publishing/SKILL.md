---
name: linkedin-publishing
description: Preview an approved LinkedIn artifact and delegate gated publication to BuildLog. Use only when the exact final artifact is ready and publication must stop at an explicit human PUBLISH confirmation.
---

# LinkedIn Publishing

Status: `DRAFT` · Version: `0.1.0`.

## Contract

- Triggers: requests for linkedin publishing.
- Non-triggers: requests outside this bounded workflow or requiring unapproved external action.
- Inputs: approved LinkedIn artifact.
- Outputs: exact preview and Publication Receipt; a private Run Receipt.
- Preconditions: canonical domain contract is available; repository state is inspectable; required approval is present when applicable.
- Owned capabilities: bounded preparation, validation, and receipt creation.
- Forbidden actions: expose private data, fabricate claims or model identities, silently mutate another domain, bypass a human gate.
- Risk class: `HIGH`.
- Human gates: public publication, paid use, credential/permission changes, destructive operations, deployment, or the domain-specific gate.

## Route and validation

Model route: discovery `D0`; decision `S2`; implementation `D0`; verification `D0`; review `D3` only for fresh approval-boundary review. Record recommended and actual route without inventing identities. Stop rather than speculate after an ambiguous platform outcome.

Use the existing BuildLog LinkedIn publisher only. Never access token internals. Stop at the PUBLISH human gate unless an explicit approval receipt is present.

Allowed tools: existing repository commands and the named canonical domain boundary only. Validation: input/output contract, hashes, and deterministic checks. Failure policy: stop on a missing gate or contract violation; after two low-confidence attempts, preserve hypotheses and escalate. Receipts record task envelope, version, models, tools, evidence IDs, hashes, checks, approvals, retries, status, timestamps, and external IDs where applicable. Examples: see `examples/example.md`.
