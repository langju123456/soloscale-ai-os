# Visual Brief — “One strong brain, many execution surfaces”

## Format

- LinkedIn carousel: 6 slides, 1080 × 1350
- X: one landscape architecture diagram plus a short thread

Editable architecture reference: [SoloScale AI OS architecture board](https://www.figma.com/board/psWfF0mEOdHqUvyOWrJWeF)

Evidence rule: every result in the artwork must be labeled `VERIFIED`, `OBSERVED`, `HYPOTHESIS`, or `PLANNED`. Do not imply a time, token, cost, or quality improvement until a comparative run supports it.

## Slide 1 — Pivot

**I nearly built a multi-agent runtime to solve a personal workflow problem.**

Subhead: The bottleneck wasn’t agent intelligence. It was routing work to the right stateful surface.

Evidence label: `OBSERVED — origin story`

## Slide 2 — Diagnosis

Headline: **Route by required state, not by whether a task looks technical.**

Visual: a technical-looking task passes through several overlapping roles before any required state is identified. A diagnostic marker isolates the missing question: what state, tool, permission, or independent evaluation does this step require?

Do not label the false start “slower,” “more expensive,” or “wasteful.” Those outcomes remain unmeasured.

## Slide 3 — Four routing questions

1. Does it need local repository, terminal, test, build, or Git state?
2. Does it need realtime, scheduled, repeated, or unattended execution?
3. Can a connected action surface complete it?
4. Is it public, costly, privileged, destructive, or irreversible?

## Slide 4 — Five surfaces

Show five parallel cards fed by one route decision:

- Reasoning — no local or live system state
- Connected actions — supported online systems
- Local coding — repository / terminal / tests / build / Git
- Runtime — realtime / scheduled / repeated / unattended
- Human approval — public / costly / privileged / destructive / irreversible

All five surfaces write to a shared evidence layer. Planner, executor, and reviewer roles appear only where their boundaries are independently evaluable.

## Slide 5 — Verified / not claimed

`VERIFIED LOCALLY`

- Baseline `dd2a5cd`: 8 tests, type check, and CLI demo passed; Ruff found 8 issues.
- Hardening `9fd720b`: 28 tests, Ruff, mypy across 17 files, CLI from `/private/tmp`, isolated sdist/wheel build, and diff check passed.

`NOT CLAIMED`

- public CI or PR evidence
- deployment
- invocation of external execution surfaces
- measured improvement in time, turns, tokens, cost, or quality

Proof placeholder: `[Link evidence manifest / commit / public PR / public CI]`

## Slide 6 — Evidence loop

Task → route → contract → execution → event receipts → independent review → BuildLog → channel narrative → feedback

`VERIFIED`: evidence schema, export, and content templates exist.

`PLANNED`: complete dogfood run and measured multichannel reuse.

`HYPOTHESIS`: routing by required state will reduce repeated context and unnecessary use of stateful execution surfaces.

Measurement strip: route accuracy · handoff size · turns · elapsed time · failures · human interventions · human edit distance

## Alt text

### Landscape architecture diagram

Short alt text: A task routes across five state-based surfaces, each producing evidence for review and an optional public narrative.

Long description: SoloScale AI OS asks four questions, then routes a task to one of five bounded surfaces: reasoning, connected online actions, local coding with repository and terminal state, realtime or scheduled runtime execution, or human approval. Each surface writes to a shared evidence layer for independent review and optional narrative reuse. The diagram describes the design; it does not claim measured efficiency gains.

### Carousel slides

1. Pivot: a first-person origin statement explains that an attempted multi-agent runtime exposed a routing problem rather than an intelligence problem.
2. Diagnosis: overlapping roles appear before the task’s required state, tools, permissions, or review boundary have been identified; no performance comparison is claimed.
3. Four questions test for local state, live or unattended execution, supported connected actions, and risk requiring human approval.
4. Five cards represent reasoning, connected actions, local coding, runtime execution, and human approval, all feeding a shared evidence layer.
5. Verified / not claimed: the baseline’s three passing checks and eight Ruff issues lead to the hardened revision’s local passing checks; public CI, PR, deployment, external execution, and efficiency remain unclaimed.
6. Evidence loop: a task moves through route, contract, execution, receipts, review, evidence export, and narrative; existing artifacts are separated from planned dogfooding and the unmeasured routing hypothesis.
