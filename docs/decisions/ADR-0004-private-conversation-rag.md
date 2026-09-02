# ADR-0004: Add a private, evidence-bound Conversation RAG plane

**Status:** Accepted
**Date:** 2026-08-09
**Target:** SoloScale v0.2

## Context

Casebook v0.1 preserves evidence after the operator has already found and selected the
relevant files. In real AI-assisted work, diagnoses, rejected hypotheses, implementation
decisions, test results, and review receipts are distributed across Codex conversations,
exported ChatGPT conversations, and BuildLog runs. Manual search creates evidence debt and
makes it harder to convert completed work into learning and reviewed content.

An unconstrained LLM over raw account history would create a different problem. Private
text could cross a trust boundary; control-plane text or tool payloads could be mistaken
for user-visible evidence; generated claims could become detached from the exact source
seen during the run; and model output could be promoted without human confirmation.

## Decision

Add a local-only Conversation Knowledge plane with five explicit boundaries:

1. **Defensive source adapters** read an observed local Codex JSONL format,
   operator-supplied ChatGPT exports, and a narrow BuildLog evidence scope. There is no
   live ChatGPT history API, cookie scraping, account-database access, or attachment
   ingestion.
2. **Narrow BuildLog projection** indexes `ingestion-report.md`, `03_draft.md`, and
   `05_final.md` as narrative bodies. `events.jsonl`, `02_plan.json`,
   `04_evaluation.json`, `run_metadata.json`, and `timeline.json` contribute only through
   separate schema-specific safe-field projections. Raw prompt, response, tool,
   stdout/stderr, arbitrary nested, and unknown fields are not searchable through those
   structured projections. Narrative bodies remain searchable after best-effort redaction
   and require operator review.
3. **Private normalization and indexing** applies best-effort redaction before
   persistence, retains stable source/document/chunk identities and content hashes, and
   stores the current snapshot in an ignored SQLite FTS index under `.soloscale/`.
4. **Bounded retrieval** combines FTS and exact metadata matching with deterministic rank
   fusion, mixed Latin/CJK splitting, and budgeted CJK bigrams. Every hit resolves to one
   stored chunk and hash lineage; bounded adjacent chunks can preserve turn or segmented
   artifact context. Stored bodies and FTS projections are integrity-checked, and approved
   resync replaces a mismatch.
5. **Custom Evidence Agent** lets a local LLM suggest and refine searches, while ordinary
   Python code owns tool access, rounds, query/hit/context budgets, source filters,
   citation membership, persistence, and completion. This loop is not an external agent
   framework or an OpenAI Agents SDK integration.

The agent produces a private candidate. It does not automatically promote output into a
confirmed Casebook case, a final BuildLog artifact, a resume claim, or public content.

Retrieved text is untrusted. Code limits its possible effects to bounded local search and
verifies that each declared claim cites an in-context chunk from the same run. Prompt
injection, irrelevant citations, and omitted gaps remain possible, so human review is
required.

## Source boundary

- **Codex:** discovery is enabled by default below the selected Codex home. Records are
  selected from the locally observed JSONL structure. The adapter is not a promise that
  this internal format is a stable public API. `--no-codex` is the explicit opt-out.
- **ChatGPT:** imports only an operator-provided `conversations.json` or export ZIP.
  When a valid `current_node` exists, only its root-to-current ancestry is imported;
  sibling branches and known hidden-message variants are excluded. Older/nonstandard
  exports without a usable current node use deterministic compatibility traversal and can
  require operator coverage review. ChatGPT is opt-in on every sync.
- **BuildLog:** explicit roots take precedence; otherwise an enclosing checkout can be
  detected from the current directory. Narrative bodies and the five schema-specific safe
  structured projections are the only searchable content. v0.2 has no dedicated
  `--no-buildlog` flag; omitting roots outside an enclosing checkout excludes it.
- **All sources:** a source that changes during read is deferred. Each selected source is
  rescanned in full within size limits and stored as a current snapshot. Long message and
  artifact bodies use deterministic overlapping segments. Sync does not watch files or
  run on a schedule.

## Evidence and lifecycle boundary

- Stable identifiers derive from source identity; they are not described as random UUIDs.
- Successful agent runs preserve the exact context-budget-fitted excerpts supplied to the
  model as immutable receipt content for that run, not longer search excerpts the model
  did not see. The local files are not a tamper-proof ledger.
- Before finalization, cited chunk identities and hashes are rechecked against the current
  index. A mismatch fails rather than silently accepting stale lineage.
- Failed agent runs write a private `failure.json`; they do not create a successful final
  result.
- Removed or deselected source files are not pruned incrementally in v0.2.
  `soloscale knowledge-reset --yes` deletes only the derived index and preserves private
  agent-run receipts. A subsequent sync rebuilds the index from approved sources.

## Security and storage boundary

- Redaction covers known control-plane blocks and common credential shapes on a
  best-effort basis. It is not complete DLP and cannot make arbitrary private text safe to
  publish.
- The knowledge store is local single-writer SQLite/FTS storage.
- Body and FTS projection integrity is checked on retrieval. An approved resync can heal a
  mismatch from the normalized source; reads fail rather than silently repairing it.
- Managed directories and files use POSIX modes `0700` and `0600`. Host access control and
  disk policy still apply.
- Deterministic sync and search do not call a model or network service. The optional
  Evidence Agent uses a loopback-only Ollama endpoint with an already-installed model.
- Raw conversations, the index, and agent-run receipts remain ignored private artifacts.

## Consequences

- Relevant engineering history becomes searchable without committing raw conversations.
- A model can synthesize across multiple threads while retaining inspectable run-bound
  excerpts and explicit unknowns.
- Citation membership and hash lineage are enforceable, but semantic support and truth are
  not. A human must still judge whether evidence supports a claim.
- Full rescans and the lack of per-source pruning simplify the first local slice but make
  retention operations coarse.
- The first retrieval slice has no vectors, embedding service, scheduler, multi-writer
  coordination, automatic content promotion, cloud synchronization, or deployment.
- Control Tower can project Conversation RAG counts, run state, and one deterministic exact
  next action without embedding conversation bodies or source locators.
- A synthetic bilingual retrieval-only golden gate records Recall@5 `1.0`, MRR `1.0`,
  store neighbor-expansion recall `1.0`, neighbor-expansion forbidden-context precision
  `1.0`, and deterministic repeated/
  rebuilt rankings. One targeted local run measured a maximum search latency of
  `1.863 ms`; this is a single local maximum, not a percentile or service commitment.
  Semantic faithfulness, answer relevancy, and reasoner-output quality are not evaluated
  and remain human-gated or future opt-in evaluation work.
- v0.3 can add Codex SDK execution, v0.4 can add Agents SDK planner/reviewer roles, and
  v0.5 can add queue workers, sandboxes, observability, and cloud deployment without
  changing the v0.2 evidence boundary.
