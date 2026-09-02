# Conversation Distillation — From agent swarm to surface routing

- **Date:** 2026-08-06
- **Publication status:** Public-safe summary; no raw transcript

## Trigger

I nearly built a multi-agent runtime to solve a personal workflow problem. The real bottleneck wasn’t agent intelligence. It was routing work to the right stateful surface.

The false start assumed more agent roles were needed before checking whether each step required distinct state, tools, permissions, or independent evaluation. That made the orchestration more elaborate without first proving that the extra roles added independent value.

## New model

Route by required state, not by whether a task looks technical.

| Required state or boundary | Default surface |
| --- | --- |
| Reasoning with no local or live system state | Reasoning surface |
| Supported action in an online system | Connected action surface |
| Repository, terminal, tests, build, or Git state | Local coding surface |
| Realtime, scheduled, repeated, or unattended work | Runtime surface |
| Public, costly, privileged, destructive, or irreversible action | Human approval gate |

## Decisions

- Start with one default reasoning core.
- Add a specialist only for a distinct tool, data source, permission boundary, latency profile, independent subtask, or independent review.
- Pass typed contracts between surfaces instead of copying full conversations.
- Keep routing, state transitions, retry limits, and completion checks in deterministic code.
- Preserve run evidence and require human approval at irreversible boundaries.

## Rejected alternatives

- A persistent group of role-playing agents that reads the same context and debates every task.
- Sending every technical-looking task to the local coding surface.
- Treating conversational memory as the handoff contract.
- Claiming efficiency gains before a comparative run has been measured.

## Evidence and limits

The initial v0.1 baseline at `dd2a5cd` recorded eight passing tests, a passing type check and CLI demo, and eight Ruff issues. Hardening revision `9fd720b` then recorded 28 passing local tests, passing Ruff and strict mypy checks, a CLI demo from outside the repository, an isolated package build, and a passing diff check. This is local implementation evidence, not proof that surface routing saves time, tokens, or money.

v0.1 recommends a route, generates a handoff, and records evidence. It does not yet invoke external reasoning, connected action, local coding, or runtime services on the operator’s behalf.

## Open questions

- Does surface routing reduce repeated context or local-coding turns on a real feature?
- Which tasks are misrouted by the initial deterministic policy?
- How much review independence is enough for each risk level?
- What evidence format has the lowest human edit distance across channels?

## Experiment

Run one narrow feature through planning, a versioned execution contract, local implementation, verification, independent review, and evidence export. Capture the route, handoff size, turns, elapsed time, failures, human interventions, and final receipts. Compare only after a repeatable baseline exists.

## Reusable language

- “I nearly built a multi-agent runtime to solve a personal workflow problem.”
- “The real bottleneck wasn’t agent intelligence. It was routing work to the right stateful surface.”
- “Route by required state, not by whether a task looks technical.”
- “Build once. Preserve the evidence. Adapt the story without inventing the result.”
