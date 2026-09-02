---
name: visual-storytelling
description: Choose and produce a public-safe visual while preserving editable source, render, alt text, and receipt. Use when a reviewed story needs an evidence screenshot, diagram, comparison, card, or illustration.
---

# Visual Storytelling

Status: `DRAFT` · Version: `0.1.0`.

## Contract

- Triggers: requests for visual storytelling.
- Non-triggers: requests outside this bounded workflow or requiring unapproved external action.
- Inputs: story and approved evidence.
- Outputs: editable source, rendered asset, alt text, visual receipt; a private Run Receipt.
- Preconditions: canonical domain contract is available; repository state is inspectable; required approval is present when applicable.
- Owned capabilities: bounded preparation, validation, and receipt creation.
- Forbidden actions: expose private data, fabricate claims or model identities, silently mutate another domain, bypass a human gate.
- Risk class: `MEDIUM`.
- Human gates: public publication, paid use, credential/permission changes, destructive operations, deployment, or the domain-specific gate.

## Route and validation

Model route: discovery `S2`; decision `D3`; implementation `S2`; verification `D0`; review `D3` when independent review is required. Record recommended and actual route without inventing identities. After two low-confidence failures, preserve hypotheses and escalate; otherwise downshift once uncertainty is removed.

Choose in order: public-safe evidence screenshot, process diagram, architecture diagram, decision comparison, insight card, generated illustration. Review public safety before release.

Allowed tools: existing repository commands and the named canonical domain boundary only. Validation: input/output contract, hashes, and deterministic checks. Failure policy: stop on a missing gate or contract violation; after two low-confidence attempts, preserve hypotheses and escalate. Receipts record task envelope, version, models, tools, evidence IDs, hashes, checks, approvals, retries, status, timestamps, and external IDs where applicable. Examples: see `examples/example.md`.
