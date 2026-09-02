# Local-to-Cloud Deployment Path

SoloScale v0.1 is a local CLI and evidence workflow. It is intentionally not a hosted executor yet. Shipping a fake cloud wrapper before the contracts and evidence loop are reliable would hide the most important product risk.

## Current boundary

Today the repository can:

- validate and route typed task envelopes;
- generate bounded execution packets;
- guard workflow state transitions with persisted continuity and execution approval receipts;
- persist local JSONL evidence;
- export a BuildLog-compatible summary.

It does not yet expose an HTTP application, run Codex programmatically, enqueue work, isolate untrusted repositories, or persist durable cloud state.

The local JSONL store is inspectable but is not a concurrent, tamper-evident, or multi-tenant database. It must be replaced behind a repository interface before hosted execution.

## Phase 1 — Local evidence loop

Complete 20–30 representative local runs before moving execution to the cloud. Measure routing errors, human interventions, repair rounds, time, and evidence completeness.

## Phase 2 — Thin hosted control plane

Add a stateless FastAPI adapter only after the local contracts stabilize:

```text
GET  /api/health
POST /api/route
POST /api/packet
```

The API should validate input and return JSON. It should not clone repositories, run shell commands, or write local JSONL files inside a request-serving function.

Vercel's current Python runtime supports FastAPI through a recognized ASGI entrypoint and `pyproject.toml`; the eventual adapter can use `tool.vercel.entrypoint`. Git integration should create preview deployments for pull requests, while GitHub Actions remains the required correctness gate.

## Phase 3 — Durable orchestration

Introduce stable interfaces before selecting vendors:

```text
TaskRepository
RunRepository
ApprovalRepository
ArtifactStore
WorkQueue
SandboxExecutor
```

Use PostgreSQL for task, run, approval, and receipt metadata. Store larger logs, diffs, and screenshots in object storage. GitHub receives curated evidence links rather than the entire raw event stream.

## Phase 4 — Isolated workers

Repository cloning, Codex execution, commands, tests, and repair loops belong in isolated workers, not in the Vercel request function. Start with a local worker behind the queue interface; later move it to sandboxed cloud workers without changing the domain contracts.

## Phase 5 — Scale and productization

Only after measured reliability add multi-project tenancy, authentication, policy profiles, cost budgets, dashboards, scheduled/webhook triggers, and billing. Production deployment, secret access, destructive changes, and public publishing remain explicit human-gate actions.

## Deployment gate

A preview deployment is ready when:

- local and CI verification are green;
- HTTP contracts have tests;
- no executor depends on request-local filesystem state;
- environment variables contain no committed secrets;
- approval and idempotency behavior are explicit;
- logs and error reporting are configured;
- rollback and incident ownership are documented.
