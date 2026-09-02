# Draft LinkedIn Post — One strong brain, many execution surfaces

I nearly built a multi-agent runtime to solve a personal workflow problem.

The real bottleneck wasn’t agent intelligence. It was routing work to the right stateful surface.

The false start treated orchestration topology as the first decision before establishing whether each step needed distinct state, tools, permissions, or independent evaluation.

That observation led to a different rule:

**Route by required state, not by whether a task looks technical.**

The current model uses:

- a reasoning surface for work with no local or live system state;
- connected action surfaces for supported online operations;
- a local coding surface for repositories, terminals, tests, builds, and Git;
- a runtime surface for realtime, scheduled, repeated, or unattended work;
- a human approval gate for public, costly, privileged, destructive, or irreversible actions.

**One strong reasoning core. Many bounded execution surfaces.**

I am implementing this model in an open engineering project called **SoloScale AI OS**.

The first local baseline was deliberately honest: eight tests passed, the type check and CLI demo passed, and Ruff found eight issues. That red result became the hardening backlog rather than disappearing from the story.

At hardening revision `9fd720b`, local verification recorded:

- 28 passing tests;
- a passing Ruff check;
- a passing strict mypy check across 17 source and test files;
- a CLI demo that passed from `/private/tmp`, outside the repository;
- an isolated package build that produced an sdist and wheel;
- a passing diff check.

The hardened v0.1 also adds strict versioned contracts, evidence-backed transitions, persisted state continuity, explicit approval receipts, complete execution packets, and broader CLI and orchestration coverage.

[Proof before publication: link the hardening commit, evidence manifest, public PR, and public CI run.]

That is local implementation evidence, not product impact. There is no public CI or PR receipt yet, no deployment, and no measured efficiency comparison. v0.1 still recommends routes, generates handoffs, and records evidence; it does not invoke external reasoning, connected action, local coding, or runtime services on the operator’s behalf.

My hypothesis is that routing by required state will reduce repeated context and unnecessary use of stateful execution surfaces. That is not a measured result yet. The dogfood run will capture route accuracy, handoff size, turns, elapsed time, failures, and human interventions before I make an efficiency claim.

The project’s bounded roles exist only where their responsibilities can be evaluated independently:

- a planner freezes decisions;
- an executor performs constrained work;
- a reviewer checks evidence independently;
- deterministic code controls state and limits;
- a human approves risk.

The longer-term content loop is also a hypothesis under test: build once, preserve the evidence, and adapt it into reviewed channel-specific narratives without inventing outcomes. The evidence schema, export, and templates exist; a complete dogfood run and measured multichannel reuse do not.

Editorial source: [launch claim ledger](launch-claim-ledger.md)
