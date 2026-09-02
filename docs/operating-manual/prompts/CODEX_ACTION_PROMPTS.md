# Codex Action Prompts

Codex 只处理本地工程工作。不要把市场、商业、内容策略或长对话重新交给 Codex。

---

## CODEX-DOCS-001 — Integrate This Manual

```text
You are integrating a documentation-only operating manual into the
solo-scale-ai-os repository.

Source directory:
[PATH TO soloscale-execution-manual-v1]

Target:
docs/operating-manual/

Rules:

- Copy the documentation, prompts, and templates under the target directory.
- Add a short link from the root README without rewriting the product README.
- Do not modify src/, tests/, package behavior, dependencies, CI behavior,
  release version, or existing architecture decisions.
- Preserve UTF-8 Markdown and Mermaid blocks.
- Scan for secrets, absolute local paths, and broken relative links.
- Run only existing documentation or repository checks.
- Show git diff --stat and the exact files changed.
- Do not commit, push, create a PR, deploy, or publish without approval.

Return:
- files added/changed
- checks run
- broken links or conflicts
- recommended commit message
```

---

## CODEX-REPO-001 — Read-Only Compatibility Inspection

```text
You are the read-only repository inspection stage.

Read:
- AGENTS.md
- the GitHub Issue
- Approved Plan
- Codex Execution Packet
- repository README and configured verification commands

Do not:
- edit files
- install dependencies
- run destructive commands
- create a branch or commit
- redesign the product
- expand scope

Verify:

1. Does the plan match the actual repository?
2. Which exact paths and symbols are relevant?
3. Which assumptions are false or incomplete?
4. What verification commands already exist?
5. Are any Stop Conditions triggered?
6. What is the smallest safe implementation sequence?

Output a Compatibility Report.
Stop and wait for approval before writing code.
```

---

## CODEX-IMPLEMENT-001 — Implement Approved Packet

```text
Read and follow:

1. AGENTS.md
2. Original GitHub Issue
3. Approved Plan
4. Codex Execution Packet
5. Compatibility Report

Your role is local implementation only.

Rules:

- Treat Frozen Decisions as approved constraints.
- Make the smallest safe diff.
- Do not redo market, product, or architecture analysis.
- Do not modify unrelated files.
- Do not add a production dependency without approval.
- Do not change public APIs, schemas, auth, deployment, or permissions unless
  explicitly approved in the packet.
- Add tests for success and failure paths.
- Run targeted checks first.
- Stop immediately on a Stop Condition.
- Do not push, merge, deploy, publish, or access secrets.

Return:
- files inspected
- files changed and why
- commands executed
- test results
- deviations from plan
- unresolved risks
```

---

## CODEX-VERIFY-001 — Deterministic Verification

```text
Do not add features or refactor.

Verify the current implementation using only repository-defined checks.

Run in this order when available:

1. targeted tests
2. unit tests
3. integration tests
4. lint
5. type checking
6. build/package
7. git diff --check
8. git status --short

Record:

- exact command
- exit code
- duration
- result summary
- failing test or error excerpt
- whether failure is pre-existing or caused by this diff

Generate a verification report and a diff-first review pack.
Do not claim PASS if any required check was skipped or failed.
```

---

## CODEX-FIX-001 — Fix P0/P1 Only

```text
Read the independent review findings.

Fix only P0 and P1 findings.

Rules:

- Do not fix P2 unless explicitly approved.
- Do not redesign the feature.
- Do not refactor unrelated code.
- Add a regression test for each corrected P0/P1 when practical.
- Run targeted checks, then the full relevant verification suite.
- Report each finding as fixed, not fixed, or disputed with code evidence.
- Stop after two repair rounds and request human review.
```

---

## CODEX-GIT-001 — Local Git Checkpoint

```text
Prepare a local Git checkpoint for the verified change.

Before writing Git history:

- confirm the intended branch
- show git status --short
- show git diff --stat
- confirm required checks passed
- confirm no secrets or local absolute paths are present

Create only the approved local commit.
Do not push or create a PR.

Return:
- branch
- full commit SHA
- title
- body
- files included
- verification evidence
```

---

## CODEX-EVIDENCE-001 — Build Run Evidence Pack

```text
Generate a factual run evidence pack from the current task.

Inputs:
- Issue
- Approved Plan
- Execution Packet
- Git history
- diff
- verification logs
- review findings
- final disposition

Outputs:

- task.json
- route-decision.json
- approval-receipt.json
- changed-files.txt
- diff.patch
- verification.json
- review.json
- final-report.md
- metrics.json
- buildlog-iteration.json

Rules:

- do not estimate missing usage, cost, time, or impact
- mark missing data as missing
- remove secrets and absolute local paths
- preserve source commit and evidence paths
- do not publish
```

---

## CODEX-VIDEO-001 — Build Creator Video Factory v0.1

```text
Create Creator Video Factory v0.1 as a local, repository-scoped engineering
module.

Purpose:
Turn verified SoloScale / BuildLog evidence into branded, reviewable video
artifacts.

Inputs:
- BuildLog-compatible iteration JSON
- optional screenshots
- optional screen recordings
- optional voiceover
- brand configuration

Outputs:
1. 1080x1920 EngineeringShort MP4
2. 1920x1080 ArchitectureExplainer MP4
3. 1080x1350 EvidenceCarouselMotion MP4
4. script.md
5. storyboard.json
6. captions.srt
7. publish-pack.json
8. provenance.json

Use:
- Remotion and React
- FFmpeg only where required
- local deterministic rendering
- no paid API calls in v0.1

Reusable components:
- HookCard
- CodeWindow
- TerminalWindow
- GitTimeline
- MetricCard
- DecisionCard
- RiskCard
- CaptionLayer
- ProgressBar
- CTA
- SourceBadge

Validation:
- no unsupported metric may render
- no claim without evidence mapping
- no secret or absolute path in output
- explicit fallback for missing media
- fail closed when required evidence is absent

Do not:
- integrate avatar, voice, clipping, scheduler, or analytics APIs
- deploy
- publish
- build a dashboard
- add authentication
- clone any third-party identity

Run tests, lint, type checking, and one real local render.
```

---

## CODEX-RENDER-001 — Render a Bounded Video Batch

```text
Render the approved video batch from the supplied content manifests.

Inputs:
- approved scripts
- storyboards
- evidence map
- brand config
- approved media assets

Rules:

- render only approved compositions
- validate every claim and metric against evidence
- do not download unapproved media
- do not call paid APIs
- do not publish
- do not alter the content strategy
- fail closed on missing required evidence

Return:
- artifact paths
- dimensions
- duration
- checksums
- render duration
- warnings
- provenance
```

---

## CODEX-LANDING-001 — Implement Approved Landing Page

```text
Implement the approved landing-page copy and design in the target local repo.

Inputs:
- approved copy
- approved Figma/design spec
- existing site architecture
- analytics and privacy requirements

Rules:
- preserve existing stack
- do not invent offers, prices, customer claims, or testimonials
- do not configure live Stripe secrets
- use environment-variable placeholders
- add tests/build checks
- produce a local preview
- do not deploy without approval
```

---

## CODEX-AUDIT-001 — Clean-Room Audit

```text
Perform a clean-room delivery audit.

Do not modify product code or expand features.

Verify:
- Git history integrity
- clean clone/install
- tests/lint/type/build
- package installation outside repo
- artifact integrity and hashes
- secrets and absolute paths across current history
- claims versus evidence
- review independence
- private/public readiness

Write one verification report.
Do not push, deploy, publish, or rewrite history.
```
