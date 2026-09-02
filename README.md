# SoloScale AI OS

SoloScale turns real engineering work into traceable evidence, learning workflows, job artifacts, and publishable proof — with deterministic verification and explicit human gates.

## What it is

SoloScale is a local-first, human-controlled AI workflow system. Instead of treating AI as an isolated chat box, it routes work through a small number of bounded roles (planner, executor, reviewer, human gate), keeps a deterministic evidence trail, and turns verified work into learning, resume, and content artifacts that a human reviews before anything leaves the machine.

It is intentionally **not** a fully autonomous agent. Code owns the loop: state transitions, retry budgets, cost limits, timeouts, approvals, and completion checks are all deterministic.

## Current product surface

The mainline ships a Python package (`soloscale`) with a CLI and a local web UI, plus optional macOS desktop and video-rendering surfaces.

- **Deterministic workflow** — Task Envelope → route decision → guarded state machine → append-only run events → approval receipts → Execution Packet.
- **Evidence** — EvidenceHub plus a private, local knowledge index (SQLite + FTS) built from defensively parsed local Codex sessions and operator-supplied ChatGPT exports.
- **Resume / Application** — turns a job description plus an operator-supplied candidate profile into a draft and a skill–evidence graph. Resume facts come only from the operator's profile, never from retrieval candidates. Application bundles are staged and human-reviewed.
- **Learning** — Casebook turns resolved engineering incidents into evidence-backed interview practice (Explain → Trace → Rebuild → Debug → Defend), with a local Control Tower that shows one exact next action.
- **Creator / Content** — Content Studio drafts LinkedIn/X posts from approved evidence; Creator and Editorial flows organize video/content production into reviewable, human-gated packages.
- **Publish** — platform accounts and publishing are staged and human-gated. Nothing is published automatically.

## Canonical domains

```text
Real work
   ↓
Work / Evidence
   ├── Resume / Application
   ├── Learning
   ├── Career interpretation
   └── Creator / Content
              ↓
           Publish
              ↓
       External outcomes
```

AI providers and integrations support these domains; they are not domains themselves.

**Career ownership:** Career interprets the canonical evidence, application, learning, and story truth produced by the other domains. It does not own a second parallel database of those facts.

## Truth boundaries

SoloScale separates four kinds of behavior and labels each one:

| Behavior | Where it lives |
|---|---|
| Deterministic verification | state machine, event store, receipts, hashes, schema checks |
| Model-generated suggestions | bounded evidence agent and structured-output provider calls |
| Human approval | publication, spending, destructive actions, and irreversible steps |
| External side effects | optional provider and publishing integrations |

Retrieved text is untrusted. The local Evidence Agent is code-limited to bounded search with fixed query/round/context budgets, and every declared claim must cite an in-context chunk from the same run. Prompt injection and citation gaps remain possible, so human review is required before promotion.

## Optional integrations

These are implemented but not required to run the core:

- **Local models** — Ollama (Evidence Agent) and MLX (`media_runtime/qwen_mlx_worker.py`).
- **Provider gateway** — Ollama, an explicitly configured OpenAI-compatible endpoint, and a hosted gateway; no credentials are embedded.
- **YouTube** — OAuth-based publishing through `platform_accounts` / `youtube_publishing`.
- **LinkedIn / X** — drafts and publish-queue handoff through Content Studio and BuildLog.
- **HeyGen** — a bounded avatar-segment provider behind a paid-operation authorization gate.
- **GitHub** — connection store for the local-to-cloud path.
- **BuildLog** — a separate downstream evidence-to-story and publishing system; SoloScale keeps only adapter and handoff code (see below).

## BuildLog boundary

BuildLog is an independent project with its own repository. SoloScale keeps the minimum adapter and handoff code (`buildlog_adapter.py`, `buildlog_handoff.py`, `editorial_publishing_handoff.py`, and the conversation-intake parsers) to read BuildLog data and stage publishing handoffs. The BuildLog source itself is **not vendored** here; the optional publishing handoffs require BuildLog to be installed separately.

## Dogfood / showcase

The Hero Demo is public proof of the product, not a core subsystem:

[Hero Demo v1](https://soloscale-showcase.vercel.app/showcase/soloscale-hero-demo-v1)

## Quick start

### Core (local, Python)

Requires Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

soloscale demo
python -m soloscale.local_ui
```

The local UI opens at `http://127.0.0.1:8765` by default.

Representative CLI commands:

```bash
soloscale task-create --title "..." --goal "..." --repo "..." --branch "..."
soloscale case-create --case-id "..." --title "..." --project "..."
soloscale knowledge-sync
soloscale evidence-agent "your question"
soloscale control-tower-build
```

Run the checks:

```bash
pytest
ruff check .
mypy src tests
```

### Optional surfaces

- **Video** — `cd video_factory && npm install && npm run build` (Remotion/TypeScript).
- **macOS desktop** — Swift sources in `desktop/macos/`; backend packaging in `packaging/macos/`.
- **Provider integrations** — supply credentials through the documented environment/Keychain path; the core runs without them.

## Repository map

```text
src/soloscale/      product source (domains and adapters)
tests/              deterministic tests
desktop/macos/      optional macOS desktop app
video_factory/      optional Remotion video renderer
media_runtime/      optional local MLX worker
packaging/macos/    desktop backend packaging
.agents/skills/     repo-scoped agent skills
docs/               architecture, ADRs, operating manual
examples/           dogfood inputs
scripts/            bootstrap, demo, and packaging scripts
```

## What SoloScale is not

- Not a multi-tenant or enterprise executor.
- Not a scraper of signed-in ChatGPT or browser accounts.
- Not an autonomous publisher; every external action stays human-gated.
- Not a claim of production customer adoption or measured commercial demand.
