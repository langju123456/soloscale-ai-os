# Private Conversation RAG v0.2

Conversation RAG finds engineering evidence that would otherwise remain buried across
Codex, ChatGPT, and BuildLog. It is a private discovery layer. It is not a live account
connector, a semantic fact checker, an automatic resume generator, or a publishing system.

## Source boundary

v0.2 supports three deliberately different adapters:

| Source | Default scope | Searchable content | Operator control |
|---|---|---|---|
| Codex | Enabled; discovers `sessions/` and `archived_sessions/` below `~/.codex` | User and assistant message records from the locally observed JSONL format | Override with `--codex-home`; disable with `--no-codex` |
| ChatGPT | Disabled until supplied | User and assistant messages from an operator-supplied `conversations.json` or export ZIP; a valid `current_node` selects only active ancestry, sibling branches are excluded, and known hidden-message flags are filtered | Repeat `--chatgpt-export`; no cookies, browser state, account database, or signed-in-history API is read |
| BuildLog | Explicit `--buildlog-root`, or an enclosing BuildLog checkout detected from the current directory | Narrative Markdown plus schema-specific safe projections from events, plan, evaluation, run metadata, and timeline files | Select roots explicitly or run outside an enclosing BuildLog checkout; v0.2 has no separate `--no-buildlog` flag |

The Codex JSONL adapter describes an observed internal local format, not a stable public
API. Unknown record types are ignored. A changed format may reduce imported coverage and
must be evaluated before relying on it.

BuildLog indexes narrative bodies from `ingestion-report.md`, `03_draft.md`, and
`05_final.md`. Structured sources use separate schema-specific allowlists:

- `events.jsonl` — safe scalar event identity, stage, status, model/prompt version,
  duration, token counts, and artifact hashes;
- `02_plan.json` — narrative planning fields such as central idea, hook, technical points,
  decision story, reader value, and ending;
- `04_evaluation.json` — named scores, findings, unsupported/vague-section lists, revision
  instructions, hard-failure state, and status;
- `run_metadata.json` — run identity/status, model/config identifiers, timings, revision
  state, and observability metadata; and
- `timeline.json` — run-level timing/status fields plus a per-step allowlist.

All of those files contribute to the run snapshot's integrity hash and byte count. Raw
prompts, responses, tool arguments, stdout, stderr, arbitrary nested payloads, and unknown
fields are excluded from searchable text.

Attachments, other chat products, and arbitrary repository files are outside the v0.2
source scope.

## Trust and privacy boundary

The index can contain private user and assistant text. Keep the data root under ignored
`.soloscale/` storage. On POSIX systems, SoloScale creates managed directories with mode
`0700` and managed files with mode `0600`. These permissions do not protect an already
compromised user account and do not replace disk encryption, backups policy, or human
review.

Before persistence, adapters apply best-effort filters for known automatically injected
control-plane blocks and common credential shapes, including private keys, bearer/basic
authorization headers, database credentials, known token prefixes, and secret
assignments. This is pattern matching, not a data-loss-prevention proof. A secret in an
unknown format, ordinary private prose, a credential quoted in a user message, or a new
source-schema variant can still enter the index. Inspect source scope and candidate output
before promotion or publication.

Retrieved text is untrusted. Code limits its possible effects to bounded local search and
verifies that each declared claim cites an in-context chunk from the same run. Prompt
injection, irrelevant citations, and omitted gaps remain possible, so human review is
required.

## Data flow

```text
read-only source discovery
→ full selected-source rescan
→ defensive normalization and best-effort redaction
→ deterministic overlapping segments for long messages/artifacts
→ stable document/chunk identifiers + content hashes
→ idempotent local snapshot replacement
→ SQLite FTS + exact metadata + bounded CJK retrieval
→ optional custom bounded Evidence Agent
→ exact model-visible fitted excerpts + current-lineage recheck
→ cited candidate + explicit gaps
→ human promotion gate
```

Synchronization is explicit. v0.2 has no file watcher or scheduler. Each selected source
is rescanned in full within configured size limits; an unchanged content hash is a no-op,
and an updated source replaces its stored current snapshot. A Codex thread moved from
active to archived storage keeps the same identity when its session metadata ID is
unchanged.

Stable identifiers are derived from source identity, not generated UUIDs:

- Codex uses the session metadata ID;
- ChatGPT uses the exported conversation ID, with a content-hash fallback;
- BuildLog uses `run_id`, with a run-directory name and absolute-path hash fallback; and
- chunks use document/message/artifact identity plus deterministic segment data.

If a selected source changes during the read, it is deferred rather than partially
committed. For JSONL, only parseable complete records are normalized. Sync is local
single-writer operation; concurrent writers are not supported.

For a ChatGPT graph with a valid `current_node`, only the root-to-current ancestry is
normalized; conflicting sibling answers and their descendants are excluded. Exports
without a usable `current_node` retain deterministic compatibility traversal, so operators
should review coverage for older or nonstandard export shapes. Long messages and artifact
bodies are split into deterministic segments of at most 1,200 UTF-8 bytes with up to
200 bytes of overlap,
preserving stable segment identities across unchanged input.

## Commands

```bash
# Default: local Codex discovery plus any enclosing BuildLog checkout.
soloscale knowledge-sync

# Override the Codex home, or opt out while importing another explicit source.
soloscale knowledge-sync --codex-home /private/path/to/codex-home
soloscale knowledge-sync \
  --no-codex \
  --chatgpt-export /private/path/conversations.json

# Add one or more operator-supplied ChatGPT exports.
soloscale knowledge-sync \
  --chatgpt-export /private/path/conversations.json \
  --chatgpt-export /private/path/another-export.zip

# Select one or more BuildLog roots explicitly.
soloscale knowledge-sync --buildlog-root /private/path/to/buildlog

# Metadata-only coverage and deterministic retrieval do not call an LLM.
soloscale knowledge-status
soloscale knowledge-search "SoloScale BuildLog structured output"

# Restrict retrieval to one or more source kinds when needed.
soloscale knowledge-search "citation lineage" --source-kind codex_session

# Rebuild the local dashboard after sync or an agent run.
soloscale control-tower-build

# The optional model boundary is a loopback Ollama endpoint.
soloscale evidence-agent \
  "Which engineering failures should become interview cases?" \
  --model qwen3:8b \
  --ollama-url http://127.0.0.1:11434
```

The first comment above describes the default source selection, not a guarantee that a
BuildLog checkout exists. ChatGPT always remains opt-in. `knowledge-status` prints counts
and timestamps, not conversation bodies. `knowledge-search` prints excerpts, so its
terminal output must still be treated as private.

## Retrieval and Evidence Agent

Deterministic retrieval combines SQLite FTS relevance with exact term/metadata matching
using stable reciprocal-rank fusion. Query normalization splits mixed Latin/CJK script
runs and adds a bounded set of CJK bigrams without exceeding the total query-token budget.
Retrieval can include adjacent chunks to retain a bounded question/answer or segmented
artifact context window. Equivalent text is deduplicated within a search result only when
its role and source kind are also equivalent; distinct chunk/source lineage is preserved
across agent queries and neighbor expansion. There is no vector or embedding channel in
v0.2.

Every returned chunk is checked against its stored body hash and its FTS projection,
including projection identity and uniqueness. A mismatch raises an integrity error rather
than returning forged text. Running an approved source resync rebuilds a mismatched stored
body/FTS projection even if the raw source hash itself did not change; search does not
silently repair data during a read.

The Evidence Agent is a custom Python, code-controlled loop. It is not LangChain, the
OpenAI Agents SDK, or another external agent framework. The model receives one read-only
knowledge-search capability. Ordinary code owns maximum rounds, queries per round, total
hits, per-query allocation, source filters, excerpt size, and total context bytes.

The model can suggest searches and draft candidate claims. It cannot write the index,
change Casebook, rewrite BuildLog, update a resume, invoke a shell, publish content, or
deploy. Each declared claim must name an in-context chunk from the same run. This is a
citation-membership check, not semantic entailment: a cited chunk can still be irrelevant
or insufficient.

Before a successful result is finalized, cited chunk identities and content hashes are
re-read from the current index. This check also binds source kind, locator, title, role,
timestamp, and a digest of searchable metadata, so an unchanged body cannot silently keep
a stale alias or provenance role. Search can produce more metadata and longer excerpts than
the model context budget admits. Under load, the model receives lean records containing
only the exact chunk ID, role, bounded matched-metadata signal, and fitted excerpt needed
for reasoning. The retrieval manifest separately retains full canonical lineage plus the
exact `model_visible_record`; it never implies that receipt-only hashes or source metadata
were shown to the model. SoloScale treats that fitted snapshot as immutable receipt content
for the run even if a later sync changes the current index; local files are not a
tamper-proof ledger. A lineage mismatch fails the run.

## Control Tower position

`soloscale control-tower-build` now includes a private Conversation RAG section alongside
Casebook. It renders:

- document and chunk counts;
- counts by source kind and the last sync timestamp;
- completed, failed, and pending Evidence Agent runs;
- one current state; and
- one deterministic exact next action.

The state/action pair directs the operator to sync an absent/empty index, ask a concrete
question when retrieval is ready, review failed receipts before retrying, review a
completed candidate before Casebook promotion, or repair/reset unsafe storage. The HTML
does not embed conversation bodies or source locators.

## Retrieval-only golden evaluation

The committed golden fixture is entirely synthetic bilingual data and contains no private
conversation content. It evaluates eight queries and three bounded context cases at top-k
five, including English multi-term retrieval, unspaced Chinese, mixed scripts, metadata
fanout, duplicate saturation, long-message context, adjacent turns, and branched context.

| Metric | Recorded value |
|---|---:|
| Recall@5 | `1.0` |
| MRR | `1.0` |
| Store neighbor-expansion recall | `1.0` |
| Neighbor-expansion forbidden-context precision | `1.0` |
| Deterministic repeated and rebuilt rankings | `true` |

One targeted local run on 2026-08-09 measured `1.863 ms` as the maximum search latency
among its measured calls. This is a workstation-specific single-run maximum; no
percentile or service commitment was measured.

This gate evaluates retrieval and `KnowledgeStore` neighbor expansion only. It does not
exercise the Evidence Agent's final context-byte allocator. Semantic faithfulness,
answer relevancy, and reasoner-output quality are **not evaluated**. Citation membership
is structural. Semantic support remains a human gate and may later become a separate,
explicitly opt-in evaluation.

## Private artifacts and recovery

```text
.soloscale/knowledge/index.sqlite3
.soloscale/knowledge/agent-runs/<run-id>/00_input.json
.soloscale/knowledge/agent-runs/<run-id>/01_query_plan.json
.soloscale/knowledge/agent-runs/<run-id>/02_retrieval.json
.soloscale/knowledge/agent-runs/<run-id>/03_retrieval_manifest.json
.soloscale/knowledge/agent-runs/<run-id>/04_result.json
.soloscale/knowledge/agent-runs/<run-id>/failure.json
```

`04_result.json` is the success artifact. `failure.json` is written for a bounded agent
failure and does not turn a partial run into a successful result. Run receipts remain
private and are not public evidence by themselves.

v0.2 does not prune a document merely because its original source file disappeared or was
no longer selected in a later sync. To remove stale searchable data, reset the entire
derived index and then resync only approved sources:

```bash
soloscale knowledge-reset --yes
soloscale knowledge-sync <approved source options>
```

`knowledge-reset --yes` deletes the derived SQLite index and sidecars only. It preserves
`.soloscale/knowledge/agent-runs/` receipts, including their exact model-visible fitted
excerpt snapshots. Review and remove those private receipts separately if policy requires
their deletion.

## Human promotion gate

Agent results are candidates. Human review is required before they become:

- confirmed Casebook facts;
- final BuildLog input;
- a resume bullet or interview claim;
- an article, post, carousel, or video script; or
- public, deployed, or customer-facing content.

## Known v0.2 limitations

- full selected-source rescans rather than incremental byte-range readers;
- local single-writer SQLite rather than a concurrent service;
- no source-level pruning or retention scheduler;
- no live ChatGPT history API, cookie scraping, or browser/session connector;
- an observed internal Codex JSONL adapter, not a stable public contract;
- best-effort redaction and hidden-message filtering, not completeness guarantees;
- keyword/metadata retrieval rather than vectors or embeddings;
- no unattended sync scheduler;
- golden metrics cover retrieval/context only, not reasoner or answer quality;
- citation membership and lineage checks, not semantic entailment or factual truth;
- no automatic Casebook/BuildLog/resume/content promotion; and
- no cloud sync, deployment, or publishing.

The source distribution includes the Markdown documentation under `docs/`. The wheel is
not documented as carrying the repository's top-level documentation tree; use the source
checkout or source distribution when those files are required.
