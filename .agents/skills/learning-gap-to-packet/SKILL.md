---
name: learning-gap-to-packet
description: Turn an evidence-backed learning gap into bounded Explain, Trace, Rebuild, Debug, and Defend practice. Use when engineering completion must remain separate from human mastery and claim eligibility.
---

# Learning Gap to Packet

Status: `CANDIDATE` · Version: `0.1.0`.

## Contract

- Triggers: requests for learning gap to packet.
- Non-triggers: requests outside this bounded workflow or requiring unapproved external action.
- Inputs: Learning Gap and approved anchors.
- Outputs: learning packet and receipts; a private Run Receipt.
- Preconditions: canonical domain contract is available; repository state is inspectable; required approval is present when applicable.
- Owned capabilities: bounded preparation, validation, and receipt creation.
- Forbidden actions: expose private data, fabricate claims or model identities, silently mutate another domain, bypass a human gate.
- Risk class: `MEDIUM`.
- Human gates: public publication, paid use, credential/permission changes, destructive operations, deployment, or the domain-specific gate.

## Route and validation

Model route: discovery `S2`; decision `D3`; implementation `S2`; verification `D0`; review `D3` when mastery or claim eligibility is reviewed. Record recommended and actual route without inventing identities. After two low-confidence failures, preserve hypotheses and escalate.

Delegate to Learning Traceability. Keep engineering completion distinct from mastery; no practice response promotes mastery without the defined review gate.

Allowed tools: existing repository commands and the named canonical domain boundary only. Validation: input/output contract, hashes, and deterministic checks. Failure policy: stop on a missing gate or contract violation; after two low-confidence attempts, preserve hypotheses and escalate. Receipts record task envelope, version, models, tools, evidence IDs, hashes, checks, approvals, retries, status, timestamps, and external IDs where applicable. Examples: see `examples/example.md`.
