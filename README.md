# SoloScale AI OS

**A local-first, human-controlled applied AI workflow system for turning real engineering work into verified evidence, learning workflows, job artifacts, and publishable proof.**

[**Watch the Hero Demo →**](https://soloscale-showcase.vercel.app/showcase/soloscale-hero-demo-v1)

> Current package: `0.4.2` · Python `3.11+` · Local-first · Human-controlled

**Closeout scope:** local operator workflows, the existing public Showcase, and a
provenance-matched macOS candidate. See [current delivery gate](TASK.md) and
[verification and release procedure](docs/engineering/closeout-0.4.2.md). The hosted
Resume service is locally evaluated; AWS deployment is deferred. A generated DOCX
still requires content review before application use.

The current operator priority is job-search outcomes: applications, interviews, and offers.
Learning and building proceed in parallel, while real recruiting feedback chooses the next
slice. Code existence, personal mastery, and interview ability are separate states; missing
evidence remains unknown.

**Engineering signals:** agentic workflows · RAG / evidence retrieval · structured model outputs · deterministic validation · provider routing · OAuth integrations · CLI / local UI / macOS · Python quality tooling: pytest · Ruff · mypy

---

## What this repository demonstrates

| Engineering area | SoloScale implementation |
| --- | --- |
| Agentic workflows | Bounded planner, executor, reviewer, and human-gate contracts with deterministic state and receipts outside the model |
| State & recovery | Explicit state transitions, persisted run continuity, bounded model/retrieval retries, timeouts, completion checks, and approval receipts |
| RAG / evidence | Local SQLite + FTS knowledge index, bounded retrieval, evidence citations, hash lineage, and claim validation |
| Structured AI outputs | Pydantic-validated model contracts keep generated candidates separate from verified facts and approved outputs |
| Evaluation | Synthetic retrieval/context gates plus citation-lineage, schema, packaging, and deterministic acceptance checks |
| Provider routing | Local Ollama, explicit OpenAI-compatible and DeepSeek structured-output paths, a hosted gateway, and optional local MLX—with no implicit provider fallback |
| Human-in-the-loop AI | Public, paid, destructive, credential, and irreversible actions remain explicitly gated |
| Developer tooling | Python package, CLI, local web UI, versioned Skills, receipts, and inspectable run artifacts |
| Productization | Optional native macOS desktop app and Remotion / TypeScript video-rendering surfaces |
| External integrations | YouTube OAuth/upload, read-only GitHub metadata, LinkedIn/X handoffs, and a paid-operation-gated HeyGen provider |
| Quality tooling | pytest, Ruff, strict mypy configuration, deterministic checks, and package/build tooling |

---

## Why SoloScale exists

Most AI demos stop when the model returns an answer.

Real AI products have to handle everything around that answer:

* What evidence was retrieved?
* What is the model allowed to infer?
* What happens when a step fails?
* Who owns retries and state transitions?
* How is an output validated?
* When should a human approve an external action?
* How can real engineering work become reusable evidence instead of disappearing after the task is complete?

SoloScale is my exploration of those applied AI engineering problems.

Instead of giving an autonomous agent unlimited control, **code owns the execution loop** while models operate inside bounded roles.

```text
REAL WORK
   ↓
EVIDENCE / RETRIEVAL
   ↓
MODEL REASONING
   ↓
VALIDATION / EVAL
   ↓
HUMAN REVIEW
   ↓
RESUME · LEARNING · CONTENT · EXTERNAL ACTION
```

---

## Current product surface

### Work & Evidence

SoloScale captures structured evidence from real engineering activity and turns it into reusable inputs for downstream workflows.

The local Evidence system combines:

* engineering artifacts
* Git metadata
* local Codex session data
* operator-supplied ChatGPT exports
* project runs and validation artifacts
* external outcome metadata

Retrieval is bounded and treated as untrusted input. Retrieved text can support reasoning, but retrieval alone does not authorize a public claim.

### Resume & Applications

A job description plus an operator-supplied candidate profile can produce:

* targeted resume drafts
* skill–evidence graphs
* explicit evidence gaps
* reviewable application bundles

Candidate facts remain separate from retrieval candidates.

The current `/resume/intelligence` route is a local **preflight candidate**: it
analyzes the JD, retrieves only the evidence sources selected for that run, grades
claim truth, and prepares a generation contract without making a provider call.
Private Codex/ChatGPT and BuildLog history are excluded by default. The existing
`/resume` route remains the active generation/export path; connecting the candidate
generator and recovering from provider length/schema failures are separate follow-up
work, not completed capabilities in this revision.

### Engineering Learning

Resolved engineering work can become structured interview practice through:

```text
Explain → Trace → Rebuild → Debug → Defend
```

Completing software work does not automatically imply human mastery.

### Creator & Content

Verified engineering evidence can be transformed into reviewable:

* LinkedIn drafts
* X threads
* technical stories
* diagrams
* video/storyboard packages

External publication remains human-controlled.

---

## Architecture

```mermaid
flowchart LR
    W[Real Engineering Work] --> E[EvidenceHub]
    E --> R[Retrieval / Evidence Agent]
    R --> M[Model / Structured Output]
    M --> V[Validation & Evals]
    V --> H[Human Review]

    H --> C[Resume / Applications]
    H --> L[Learning]
    H --> P[Creator / Content]

    P --> X[External Publishing]
    X --> O[Outcome Evidence]
    O --> E
```

The model helps reason and generate.

**Code owns control flow.**

State transitions, retry budgets, timeouts, approvals, validation, and completion checks remain deterministic.

---

## Tech stack

### Core

* Python 3.11+
* Pydantic
* Typer
* Rich
* SQLite + FTS
* pytest
* Ruff
* strict mypy

### AI / model integrations

* Ollama
* MLX
* configurable OpenAI-compatible endpoints
* DeepSeek structured outputs
* hosted provider gateway
* structured model outputs

### External integrations

* Google / YouTube OAuth and upload
* LinkedIn / X handoffs through BuildLog
* read-only GitHub metadata
* HeyGen behind an explicit paid-operation gate

BuildLog is a separate downstream project and is not vendored in this repository. SoloScale keeps only the adapter and handoff boundary needed to stage compatible publishing workflows.

### Product surfaces

* Python package
* CLI
* local web UI
* native macOS desktop app
* Remotion / TypeScript video rendering

---

## Hero Demo

The public Hero Demo shows the product workflow rather than only describing the architecture.

**[Open SoloScale Hero Demo v1 →](https://soloscale-showcase.vercel.app/showcase/soloscale-hero-demo-v1)**

---

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate

pip install -e '.[dev]'

soloscale demo
python -m soloscale.local_ui
```

For the optional loopback-only resume preparation API, install its extra and run:

```bash
pip install -e '.[api]'
python -m soloscale.resume_api
```

It listens on `127.0.0.1:8766` by default and provides a deterministic template-only
DOCX reorderer. It makes zero model calls and writes no resume data to disk.

For the containerized API and worker with durable PostgreSQL tasks, use the
[Resume Cloud runbook](docs/engineering/resume-cloud-runbook.md). The
[ephemeral AWS deployment package](docs/engineering/resume-cloud-aws.md) includes
infrastructure, scoped deployment access, and cleanup commands. Local container
checks have passed; a live AWS deployment has not yet been verified.

The local UI opens at:

```text
http://127.0.0.1:8765
```

Run the engineering checks:

For reproducible offline retrieval reports over the existing fixtures, see
[Retrieval evaluation](docs/engineering/retrieval-evaluation.md). For synthetic Evidence Agent
fault-path coverage, see [Agent edge-case evaluation](docs/engineering/agent-edge-case-evaluation.md).

```bash
pytest
ruff check .
mypy src tests
```

---

## Representative CLI workflows

```bash
soloscale task-create \
  --title "..." \
  --goal "..." \
  --repo "..." \
  --branch "..."

soloscale knowledge-sync

soloscale evidence-agent "your question"

soloscale case-create \
  --case-id "..." \
  --title "..." \
  --project "..."

soloscale control-tower-build
```

---

## Repository map

```text
src/soloscale/      Core product domains and adapters
tests/              Deterministic test suite
desktop/macos/      Native macOS desktop application
video_factory/      Remotion / TypeScript video renderer
media_runtime/      Optional local MLX worker
packaging/macos/    Desktop backend packaging
.agents/skills/     Versioned bounded agent Skills
docs/               Architecture, ADRs, and operating documentation
examples/           Dogfood and example inputs
scripts/            Bootstrap, demo, validation, and packaging utilities
```

---

## Design principles

### Models reason; code controls

Models are useful for planning, interpretation, extraction, and generation.

They do not silently own retries, irreversible actions, or completion semantics.

### Retrieval is evidence, not truth

Retrieved text can inform a decision.

It does not automatically become a verified fact or public claim.

### Human control stays explicit

Publishing, spending, destructive operations, credential changes, and other irreversible actions remain behind explicit human gates.

### The system should be inspectable

Important decisions produce structured state, evidence, validation results, or receipts that can be inspected later.

---

## Project status

SoloScale is an actively developed applied AI engineering project and dogfood environment.

It demonstrates implemented engineering workflows and product surfaces.

It does **not** claim:

* production customer adoption
* measured commercial demand
* enterprise multi-tenancy
* fully autonomous external execution

Those boundaries are intentional and documented.
