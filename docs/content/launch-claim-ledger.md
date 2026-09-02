# Launch Claim Ledger

This ledger is the editorial source of truth for the v0.1 X, LinkedIn, and visual drafts. A design decision can be verified without proving its expected benefit.

## Status vocabulary

- `VERIFIED`: an inspectable receipt supports the exact claim.
- `OBSERVED`: a dated first-person observation or provenance statement.
- `HYPOTHESIS`: an expected outcome that has not been measured.
- `PLANNED`: future work or proof that does not exist yet.
- `LIMITATION`: a current boundary that must remain visible.

## Claims

| ID | Public claim | Status | Current evidence | Safe publication wording |
| --- | --- | --- | --- | --- |
| C-01 | The project began when a personal workflow problem nearly became a multi-agent runtime. | OBSERVED | Owner-confirmed, public-safe [conversation distillation](../conversations/2026-08-06-agent-swarm-to-surface-routing.md) | “I nearly built…” |
| C-02 | The design routes by required state rather than by whether a task looks technical. | VERIFIED | [Architecture](../architecture.md) and [ADR-0001](../decisions/ADR-0001-one-brain-many-executors.md) | State as a design rule, not a performance result. |
| C-03 | v0.1 implements a typed task contract, deterministic route classification, guarded transitions, a handoff, append-only evidence, and an evidence export. | VERIFIED | Revision `9fd720b` and the [hardening evidence manifest](../evidence/2026-08-06-v0.1-hardening-manifest.md) | “v0.1 implements…” |
| C-04 | The pre-remediation baseline had 8 passing tests, a passing type check, a passing CLI demo, and 8 lint issues. | VERIFIED | Revision `dd2a5cd` in the dated [baseline devlog](../devlogs/2026-08-06-v0.1-baseline.md); public command receipt still required | Report all four outcomes together. Mention later remediation only with C-10 evidence. |
| C-05 | v0.1 does not invoke external reasoning, connected action, local coding, or runtime services. | LIMITATION | Current v0.1 boundary in repository documentation | “v0.1 does not yet invoke external surfaces.” |
| C-06 | State-based routing reduces repeated context, turns, latency, or cost. | HYPOTHESIS | No comparative run yet | “My hypothesis is…” followed by the measurement plan. |
| C-07 | A small bounded topology outperforms an agent swarm. | HYPOTHESIS | ADR rationale only; no comparative evaluation | Describe the chosen topology. Do not claim superiority yet. |
| C-08 | One run can become reviewed X, LinkedIn, and visual assets with low human effort. | PLANNED | Evidence export exists; end-to-end multichannel run is not yet demonstrated | “The planned content loop is…” |
| C-09 | An editable architecture visual exists. | VERIFIED | [Editable Figma board](https://www.figma.com/board/psWfF0mEOdHqUvyOWrJWeF) | Link it as the editable source, not as product-performance proof. |
| C-10 | At revision `9fd720b`, 28 local tests, Ruff, strict mypy over 17 source/test files, the CLI demo from `/private/tmp`, an isolated sdist/wheel build, and the diff check passed. | VERIFIED | [Hardening DevLog](../devlogs/2026-08-06-v0.1-hardening.md) and [evidence manifest](../evidence/2026-08-06-v0.1-hardening-manifest.md) | “Local verification recorded…” Never shorten this to “CI is green.” |
| C-11 | Public CI, PR review, and deployment have not been evidenced. | LIMITATION | No public CI, PR, or deployment receipt is linked | “Public CI, PR review, and deployment remain unverified.” |

## Proof placeholders before publication

- `P-01`: public CI URL for revision `9fd720b` showing tests, Ruff, mypy, and package build.
- `P-02`: public command receipt or committed evidence artifact for the CLI demo.
- `P-03`: public commit and PR URLs matching revision `9fd720b`.
- `P-04`: first dogfood run summary with route, handoff size, turns, elapsed time, failures, and human interventions.
- `P-05`: exported content package plus human edit notes for the multichannel claim.
- `P-06`: published sdist/wheel artifact or public build receipt.
- `P-07`: deployment receipt, only after an approved deployment exists.

Until these placeholders are replaced, keep C-06 and C-07 as hypotheses, C-08 as planned work, and C-10 explicitly local. Do not claim public CI, PR review, deployment, or measured efficiency.
