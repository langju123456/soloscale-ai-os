# SoloScale 0.4.2 — closeout gate (2026-10-02)

Deliver the existing local product and public evidence, then freeze feature work.
The user authorized the original public repository push, CI-checked main merge,
existing Showcase deployment, and a local desktop install candidate. AWS account
setup/deployment is excluded. Approval for an individual release does not make
future publication, paid calls, or credential changes automatic.

## Acceptance checklist

- [x] Identify actual latest source: `codex/resume-intelligence-public-sync`,
      baseline `6ef52b414dd51bbe731a0d191c1c368fffa8bc6a`; avoid the older private
      repo and stale installed candidate.
- [x] Exercise the existing local UI: health, Resume, Resume Intelligence preflight,
      Learning, Content, real generation redirect, result, and valid DOCX download.
- [x] Preserve source facts, private artifacts, human review, and provider gates.
- [x] Re-run baseline checks: 717 passed / 10 skipped, Ruff, strict mypy, wheel/sdist.
- [x] Check public GitHub CI for baseline (Python 3.11/3.12 and container smoke).
- [x] Repair the observed Chinese unsupported-outcome validation gap; add regression.
- [x] Evaluate an actual operator-selected Chinese resume + JD after repair:
      two substantive project edits, no unsupported fact/ownership/outcome,
      same source identity, readable rendered DOCX. Export or a text-change
      score alone is insufficient. Final application use remains human-approved.
- [x] Release checks: 720 passed / 10 skipped, Ruff, strict mypy and wheel/sdist;
      public-safe diff review excludes private model input/output bodies.

## Release execution gates

1. Commit/push to the existing repo, then check exact-SHA CI and mergeability before
   integrating into main.
2. Build a clean same-SHA backend/App 0.4.2 (9), validate the packaged main workflow,
   and install a local candidate with a provenance/launch receipt.
3. Verify and promote the existing `soloscale-showcase` deployment with truthful
   current engineering status while preserving the frozen Hero Demo.

Execution results (final Git SHA, CI run, installed App identity and deployed URL) are
recorded in the release operator receipt and the Showcase status section, avoiding
a second source commit just to change an artifact's own provenance.

Release evidence and reproducible commands: [closeout-0.4.2.md](docs/engineering/closeout-0.4.2.md).
Local receipts stay private; sanitized bounded evaluation receipts may be tracked.

## Completed engineering carried into this release

- Local-first task/state/approval contracts, private routing receipts, registered
  bounded Skills, Evidence Core, source/hash-backed Conversation retrieval.
- Separate Resume generation/export and Resume Intelligence preflight; source facts
  remain distinct from retrieved candidates. Preflight makes no provider call.
- Learning graph and Explain/Trace practice preserve personal mastery gates.
- Content drafts and optional local video rendering preserve human publication gates.
- Resume HTTP service/worker contracts, authenticated idempotent queue and recovery,
  containerization, CI, and a local 20-task/2-worker synthetic service evaluation.
- Five-category offline Agent edge-case evaluation (9 cases).

These are engineering scope statements, not evidence of customer adoption, personal
mastery, paid-provider exactly-once behavior, or unrestricted production readiness.

## Frozen follow-ups

AWS/RDS/ALB, public live API hosting without an existing approved target, HA and scale
claims, additional agent frameworks, background watchers/cloud sync, vector migration,
preflight-to-generator integration, Skill promotion, and new product features.
No Skill is ACTIVE by this release. Historical sprint permissions and counts in Git
history do not describe the current gate. Human mastery and resume-claim eligibility
remain separate reviews; this release does not update a formal resume or submit an
application.
