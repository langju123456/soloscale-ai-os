# Roadmap

## Milestone 0 — Project framing

- product specification
- architecture and ADRs
- issue and PR templates
- deterministic contracts
- CI

## Milestone 1 — Personal Mode MVP

- Task Envelope
- route recommendation
- Execution Packet
- GitHub Issue/PR handoff
- manual Chat → Codex → Chat loop
- BuildLog export
- first dogfood run

## Milestone 2 — Local Orchestrator

Before the orchestrator, a post-v0.2 Resume Intelligence candidate slice is evaluated:

- operator-supplied career facts only
- replayable lexical-candidate lineage
- private staged application bundles with explicit delivery receipts
- human review before any application-facing use
- no automatic Casebook, BuildLog, resume, or publishing promotion

It remains a candidate slice until fresh review and human promotion; it does not change the
released v0.2 Conversation RAG scope.

- Codex Python SDK adapter
- thread start, continue, and resume
- read-only and workspace-write sandbox profiles
- verification commands
- bounded repair loop
- SQLite run store
- structured cost and latency records

## Milestone 3 — API Specialist Runtime

- Agents SDK planner/reviewer
- code-driven orchestration
- input/output/tool guardrails
- optional specialist-as-tool pattern
- model routing and budget policy
- eval suite

## Milestone 4 — Plugin and Cloud Surfaces

- GitHub adapter
- Vercel deployment adapter
- Figma artifact handoff
- MCP/plugin packaging
- scheduled and webhook-triggered runs

## Milestone 5 — Hosted Control Plane

- FastAPI service
- worker queue
- PostgreSQL
- artifact storage
- sandboxed workspaces
- authentication
- observability dashboard
- resumable runs

## Milestone 6 — Scale and Productization

- multi-project support
- policy profiles
- team roles
- reusable workflow marketplace
- public demo
- benchmark and case studies
