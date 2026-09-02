# GitHub Evidence Plane

GitHub is the durable handoff and evidence layer between planning, local execution, review, and public narrative. Raw private chat and raw `.soloscale/` runs stay outside Git; reviewed contracts and evidence receipts are promoted deliberately.

## Repository setup

Recommended repository: `langju123456/solo-scale-ai-os`.

Before changing repository visibility, the owner must explicitly choose public or private and select a license. No license is added by default because public visibility and open-source permission are separate decisions.

Suggested repository metadata:

- Description: `A deterministic, evidence-first control plane for routing AI work across reasoning, connected actions, local coding, runtimes, and human approval.`
- Topics: `ai-agents`, `agent-orchestration`, `developer-tools`, `human-in-the-loop`, `python`
- Default branch: `main`

## Human-gated first publication

Run these only after the owner confirms visibility and license:

```bash
gh auth status
gh auth login
gh repo create langju123456/solo-scale-ai-os --private --source=. --remote=origin
git push -u origin main
```

Use `--public` instead of `--private` only after the public-visibility and license decision. If `gh auth status` is already valid, skip `gh auth login`. Repository creation and push are intentionally not part of automated local setup.

Create these labels before using the bundled Issue Forms:

| Label | Purpose |
| --- | --- |
| `task` | Structured workflow or feature task |
| `triage` | Routing and scope are not yet frozen |
| `architecture` | Consequential system-design proposal |
| `decision` | ADR discussion or accepted decision |
| `evidence` | Evidence package or DevLog work |
| `content` | Narrative asset derived from verified work |

## Project fields

Create a project named **SoloScale AI OS** with these fields:

| Field | Values |
| --- | --- |
| Status | Backlog, Ready, In Progress, Review, Done |
| Surface | Chat, Plugin, Codex, Runtime, Human |
| Risk | Low, Medium, High, Critical |
| Human gate | Not required, Pending, Approved, Rejected |
| Evidence | Missing, Partial, Complete |
| Content | None, Draft, Reviewed, Published |
| Milestone | v0.1, v0.2, v0.3, Cloud |
| Run ID | Text field linking the task to run evidence |

Useful views:

1. Delivery Board grouped by Status.
2. Surface Routing grouped by Surface.
3. Evidence Gaps filtered where Evidence is not Complete.
4. Content Pipeline grouped by Content.

## Branch and review policy

Protect `main` with a ruleset that:

- requires a pull request;
- requires the Python 3.11 and 3.12 CI checks;
- blocks force pushes and branch deletion;
- dismisses stale approvals after new commits;
- requires conversation resolution before merge.

Do not tag `v0.1.0` until one dogfood pull request has a complete Issue → packet → implementation → verification → review → evidence chain.

## Evidence promotion

Raw run data remains under ignored `.soloscale/`. Promote only reviewed, sanitized artifacts:

```text
docs/devlogs/<date>-<run-id>.md
docs/evidence/<run-id>/manifest.md
docs/evidence/<run-id>/summary.json
```

Each manifest should link to the Issue, commit, pull request, CI run, review, relevant ADR, DevLog, and any public-safe visual. Never promote tokens, private prompts, local absolute paths, raw chat transcripts, or user data.
