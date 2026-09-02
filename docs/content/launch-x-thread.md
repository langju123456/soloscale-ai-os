# Draft X Thread — v0.1 Thesis

1/ I nearly built a multi-agent runtime to solve a personal workflow problem.

The real bottleneck wasn’t agent intelligence. It was routing work to the right stateful surface.

So I changed the question.

2/ Route by required state, not by whether a task looks technical.

No local/live state → reasoning
Supported online action → connector
Repo/terminal/tests/Git → local coding
Realtime/scheduled → runtime
Irreversible → human

3/ The routing questions are concrete:

Does it require local state?
Does it require realtime or scheduled execution?
Can a connected action surface complete it?
Is the action risky or irreversible?

4/ The first local baseline was useful because it was imperfect:

8 tests passed
type check passed
CLI demo passed
Ruff found 8 issues

That red result became the hardening backlog—not something to hide.

5/ At hardening revision 9fd720b, local verification recorded:

28 tests passed
Ruff passed
mypy passed across 17 source/test files
CLI demo passed from /private/tmp
isolated build produced sdist + wheel
diff check passed

6/ The hardened v0.1 adds strict versioned contracts, evidence-backed transitions, persisted state continuity, approval receipts, complete execution packets, and broader CLI/orchestration tests.

[Proof placeholder: public commit/PR and CI URLs.]

7/ What this does not prove:

No public CI or PR receipt yet.
No deployment.
No external surfaces invoked.
No measured improvement in turns, time, tokens, cost, or output quality.

Local green checks are not product impact.

8/ Hypothesis—not measured: routing by required state will reduce repeated context and unnecessary use of stateful execution surfaces.

The first dogfood run will measure route accuracy, turns, handoff size, elapsed time, failures, and human interventions.

9/ The loop I’m testing:

Task → route → contract → execution → evidence → review → narrative

The evidence schema, export, and content templates exist. A complete dogfood run and low-edit multichannel reuse are still planned.

## Editorial notes

- Baseline proof: link the `dd2a5cd` baseline devlog in post 4.
- Hardening proof: link revision `9fd720b`, the evidence manifest, public PR, and public CI in posts 5–6 when available.
- Editable architecture reference: [SoloScale AI OS Figma board](https://www.figma.com/board/psWfF0mEOdHqUvyOWrJWeF).
- Source of truth: [launch claim ledger](launch-claim-ledger.md).
