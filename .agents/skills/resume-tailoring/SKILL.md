---
name: resume-tailoring
description: Create a tailored resume package from a JD, operator-approved Candidate Profile, and bounded evidence. Use when a resume, evidence map, unsupported requirements, and Learning Gaps are needed without inventing claims.
---

# Resume Tailoring

Status: `CANDIDATE` · Version: `0.1.0`.

## Contract

- Triggers: requests for resume tailoring.
- Non-triggers: requests outside this bounded workflow or requiring unapproved external action.
- Inputs: JD, approved Candidate Profile, EvidenceBundle.
- Outputs: resume, evidence map, gaps, application package; a private Run Receipt.
- Preconditions: canonical domain contract is available; repository state is inspectable; required approval is present when applicable.
- Owned capabilities: bounded preparation, validation, and receipt creation.
- Forbidden actions: expose private data, fabricate claims or model identities, silently mutate another domain, bypass a human gate.
- Risk class: `HIGH`.
- Human gates: public publication, paid use, credential/permission changes, destructive operations, deployment, or the domain-specific gate.

## Route and validation

Model route: discovery `D3` when ambiguous; decision `D3`; implementation `S2`; verification `D0`; review `D3` when independent review is required. Record recommended and actual route without inventing identities. After two low-confidence failures, preserve hypotheses and escalate; otherwise downshift once uncertainty is removed.

Delegate to Resume Workspace. Retrieved evidence is discovery material only and must never become a completed-experience claim automatically. Stop before submission unless separately approved.

Allowed tools: existing repository commands and the named canonical domain boundary only. Validation: input/output contract, hashes, and deterministic checks. Failure policy: stop on a missing gate or contract violation; after two low-confidence attempts, preserve hypotheses and escalate. Receipts record task envelope, version, models, tools, evidence IDs, hashes, checks, approvals, retries, status, timestamps, and external IDs where applicable. Examples: see `examples/example.md`.
