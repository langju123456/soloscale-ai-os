# SoloScale AI OS — Project Specification

> This is the primary product and engineering context for humans and coding agents.

## 1. Identity

**Working name:** SoloScale AI OS  
**Project type:** Human-controlled AI workflow router and agent orchestration system  
**Primary user:** A solo builder who uses ChatGPT, plugins, Codex, APIs, and GitHub to operate multiple workstreams  
**Core thesis:** A single strong reasoning core plus bounded execution surfaces is usually more efficient than many weak agents debating the same task.

## 2. Problem

AI-assisted work currently loses time and tokens through:

- repeated context reconstruction
- unclear boundaries between reasoning and execution
- coding agents rediscovering already-made product decisions
- unbounded agent loops
- tool calls without explicit risk gates
- missing run evidence and cost data
- development insight disappearing after delivery
- content creation disconnected from real engineering evidence

## 3. Product goal

Create one inspectable operating system that can:

1. classify a task by required surface;
2. keep non-local reasoning in ChatGPT whenever practical;
3. use plugins for supported online actions;
4. invoke Codex only for local engineering state;
5. use APIs only for realtime, scheduled, or product-runtime needs;
6. enforce human approval for risky or irreversible actions;
7. preserve an evidence trail suitable for debugging, replay, evaluation, and publishing.

## 4. Users and use cases

### Personal workflow

- research and architecture in ChatGPT
- cloud actions through plugins
- local feature implementation through Codex
- GitHub Issues and PRs as handoff contracts
- final review in ChatGPT
- development evidence exported to BuildLog

### Runtime orchestration

- planner, executor, reviewer roles
- code-based routing and retries
- Codex SDK as a coding specialist
- API-backed research or domain specialists
- queue-backed background execution
- sandboxed workspaces
- structured traces and cost accounting

## 5. Product principles

### One strong brain before many agents

Add a specialist only when it has a distinct tool, data source, permission boundary, or independently evaluable subtask.

### Code controls the loop

State, retry budgets, cost limits, timeouts, approvals, and completion checks are deterministic code.

### Contracts, not conversational memory

Roles communicate through typed artifacts: Task Envelope, Execution Packet, Review Result, and Run Evidence.

### Trust evidence, not claims

A run is successful only when real commands, diffs, tests, and receipts support it.

### Human control at irreversible boundaries

Production, spending, publishing, destructive changes, secrets, and permission changes require explicit approval.

### Build once, narrate from evidence

Every meaningful run should be capable of producing a BuildLog iteration without inventing a story.

### Learn from the work, not only ship it

Agent speed must not create hidden understanding debt. Selected local evidence should be
preserved with integrity receipts, while engineering completion and human mastery remain
separate, inspectable states.

## 5.1 Shipped foundation: deterministic workflow + Casebook v0.1

SoloScale Casebook turns one resolved AI-assisted engineering incident into a private,
evidence-backed interview practice case:

```text
selected chat / log / diff / test evidence
→ checksum-backed local case
→ deterministic interview packet
→ Explain → Trace → Rebuild → Debug → Defend receipts
→ local Control Tower with one exact next action
```

The v0.1 slice is deliberately local and deterministic. It does not automatically ingest
account history, call an LLM, grade answer quality, publish content, or claim commercial
validation. Delivery and human mastery remain separate states.

## 5.2 Current slice: private Conversation RAG v0.2

Casebook is extended with a local knowledge plane that can repeatedly discover and search
AI-assisted engineering history through explicit full-source rescans:

```text
observed Codex local JSONL + operator-supplied ChatGPT active branch
+ BuildLog narrative files and schema-specific safe projections
→ defensive parsing and best-effort redaction
→ private checksum-backed knowledge index
→ FTS + exact metadata + bounded CJK retrieval
→ custom code-controlled Evidence Agent query/refinement loop
→ citation-backed case/content/learning candidates
→ Control Tower position + human promotion gate
```

The LLM does not control persistence, retry budgets, evidence identity, or promotion.
It can only request searches and draft claims from the chunks returned by those searches.
Every declared claim must cite an in-context same-run chunk. Raw conversations and the
private index stay ignored under `.soloscale/`; derived local directories and files are
created with `0700` and `0600` modes on POSIX systems.

Retrieved text is untrusted. Code limits its possible effects to bounded local search and
verifies that each declared claim cites an in-context chunk from the same run. Prompt
injection, irrelevant citations, and omitted gaps remain possible, so human review is
required.

## 5.3 Post-v0.2 candidate slice: Resume Intelligence Workspace

Resume Workspace is a separate, human-triggered projection over the local knowledge plane:

```text
JD + operator-supplied Candidate Profile + lexical retrieval candidates
→ deterministic requirements and candidate matching
→ operator-claim-only resume draft + gaps + evidence graph
→ private run receipts + optional staged application bundle
→ human review
```

Retrieval matches retain source/document/hash lineage but remain lexical candidates; they
do not semantically verify requirement coverage and cannot become resume facts. Resume
bullets come only from the Candidate Profile. The legacy Evidence-Agent-to-resume renderer
is disabled. The optional application library must be outside the Git repository and uses
an explicit delivery-state receipt. This slice does not update Casebook or BuildLog, apply
to a job, publish, deploy, or call a network service in local-only mode. ADR-0005 freezes
this boundary before the candidate slice is promoted into a released roadmap version.

## 5.4 Local candidate slice: Learning Traceability golden case

The first Learning Traceability slice compiles exactly one existing capability—Conversation
RAG chunking and bounded retrieval—into a shared evidence graph. Tracked source
distillations and ADRs remain raw reasoning evidence; decisions, implemented capabilities,
real code/test anchors, mastery receipts, and approved claims remain distinct truth stages.
Engineering evidence can be verified while operator contribution and mastery remain
unknown. The local UI exposes the graph, bounded source excerpts, Explain/Trace starts,
target-JD relevance, and the resume-claim gate without advancing mastery or publishing.

Private runs and content-hash caches stay under ignored `.soloscale/` storage. This local,
single-operator MVP adds no authentication, multi-user runtime, cloud service, external
publishing, or automatic web/model call. ADR-0006 freezes this candidate boundary.

## 5.5 Local candidate slice: Content Studio MVP

Content Studio is a human-triggered, deterministic projection over an operator-supplied
claim ledger:

```text
VERIFIED / OBSERVED / HYPOTHESIS / PLANNED claims + receipts + limits
→ LinkedIn draft + X thread + short-video script/storyboard
→ private non-overwriting content run
→ preview, copy, and download
→ human fact-check and per-channel publish approval
```

The local generator does not call a model or network service, connect a social account,
or publish. Verified and observed claims require a receipt; every emitted factual block
retains its claim ID and classification. Private absolute paths and common credential
shapes fail closed before persistence. BuildLog remains the downstream publishing system;
SoloScale exposes a local review surface over the compatible evidence contract.

The optional Creator Video Factory is a local Remotion renderer. It reads only the saved
storyboard for a reviewed Content run and produces a non-overwriting MP4 plus render receipt
inside that private run. It adds no voice synthesis, generated facts, uploads, or publishing.

## 5.6 Reusable execution layer: Skill OS

Skill OS preserves successful workflow contracts as versioned repo-scoped Skills rather
than autonomous personas:

```text
high-level request
→ normalized Skill Task Envelope
→ registry-backed dependency route
→ phase-specific model recommendation
→ existing domain capabilities
→ deterministic checks + private Run Receipt
→ optional human-reviewed Skill change proposal
```

The registry records trust status, version, risk, human gates, and validation history.
Runtime input and receipts remain private. Skills do not silently share domain state,
promote Evidence into truth, publish, submit, deploy, spend, or change credentials. A
successful Run can propose a candidate or version update, but it cannot mutate an
`ACTIVE` Skill or claim external outcome without the corresponding receipt and human gate.

## 6. v0.2 success criteria

- Repeated sync preserves stable source/document/chunk identities and does not duplicate a
  Codex thread moved into archives.
- The default Codex adapter and explicit ChatGPT/BuildLog scopes are inspectable and
  operator-controlled.
- A valid ChatGPT `current_node` selects only its root-to-current active ancestry; sibling
  branches are excluded. Long messages and artifacts use deterministic overlapping
  segments.
- BuildLog narrative Markdown is joined by schema-specific safe projections from
  `events.jsonl`, `02_plan.json`, `04_evaluation.json`, `run_metadata.json`, and
  `timeline.json`; raw prompt/tool/stdout/stderr bodies remain excluded from structured
  projections, while narrative Markdown remains operator-reviewed searchable text.
- Search is deterministic and every hit resolves to stored hash lineage.
- Bounded CJK bigrams and mixed Latin/CJK script splitting support bilingual exact/FTS
  retrieval without adding a vector service.
- Retrieval verifies both stored chunk bodies and their FTS projection; an approved resync
  rebuilds a mismatched projection even when the raw source hash is unchanged.
- Agent rounds, searches, hits, and context are bounded by code.
- Run manifests and references retain the exact fitted excerpt visible to the model, while
  final cited chunks are rechecked against the current index before the result is accepted.
- Unsupported conclusions remain explicit gaps.
- The Control Tower exposes Conversation RAG counts, run state, and one deterministic exact
  next action without rendering private bodies or locators.
- A candidate cannot update Casebook, BuildLog, a resume, or a publishing surface without
  a separate human action.
- Private index files and run receipts remain ignored and local by default.

## 7. Non-goals for v0.2

- autonomous production deployment
- automatic social publishing
- multi-tenant SaaS
- six or more persistent agents
- free-form agent voting
- vector or embedding retrieval
- browser computer use
- Figma or Vercel write adapters
- automatic billing
- arbitrary shell execution from untrusted users
- claiming ChatGPT subscription access through an API
- a live ChatGPT history API, browser-cookie scraping, or account-database access
- a stable-public-API claim for Codex's observed local JSONL format
- unattended file watching or scheduled sync
- semantic entailment validation for citations
- concurrent writers, source-level pruning, or automatic cloud synchronization
- semantic grading of interview answers
- claiming self-assessed practice is externally verified mastery

## 8. Retrieval-only golden gate

The public synthetic bilingual fixture evaluates eight queries and three bounded context
cases at top-k five. The current recorded metrics are:

- Recall@5: `1.0`
- MRR: `1.0`
- store neighbor-expansion recall: `1.0`
- neighbor-expansion forbidden-context precision: `1.0`
- deterministic repeated and rebuilt rankings: `true`

One targeted local run observed a maximum search latency of `1.863 ms`. That number is
only the maximum among calls in that local run; no percentile or service commitment was
measured. Semantic faithfulness, answer relevancy, and reasoner-output quality are **not**
evaluated. They remain human-gated and can become future opt-in evaluation layers.

## 9. Key metrics

### Workflow

- time from task definition to approved plan
- Codex turns per merged feature
- percentage of runs with complete evidence
- percentage of tasks correctly routed
- review defects found before merge
- human interventions per run

### Cost

- tokens and API cost by role
- Codex usage by implementation task
- repeated-context ratio
- failed-run cost
- cost per accepted change

### Content

- engineering runs converted into publishable artifacts
- unsupported-claim rate
- human edit distance
- GitHub visits and demo engagement
- qualified conversations generated by public posts

## 10. Version sequence

- **v0.1** — deterministic workflow foundations and local Casebook.
- **v0.2** — private Conversation RAG and the custom bounded Evidence Agent.
- **v0.3** — Codex SDK execution integration.
- **v0.4** — Agents SDK planner/reviewer roles behind deterministic policy.
- **v0.5** — queue workers, sandboxes, observability, and cloud deployment.
