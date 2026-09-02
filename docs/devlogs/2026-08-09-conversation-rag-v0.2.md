# DevLog — 2026-08-09 — Private Conversation RAG v0.2

## Problem

The engineering evidence needed for interview practice, BuildLog narratives, articles,
video scripts, and future resume drafts already existed, but much of it was buried in
Codex sessions, ChatGPT exports, and BuildLog runs. Casebook v0.1 could preserve a case
after manual selection; it did not help the operator find the strongest source material
across a growing local history.

The product requirement was therefore not “let an LLM read everything.” It was:

```text
bounded local sources
→ inspectable normalization
→ deterministic retrieval
→ constrained synthesis with citations and explicit gaps
→ human promotion
```

## Decision

Build v0.2 as a local private knowledge plane with:

- an adapter for the locally observed Codex JSONL format;
- operator-supplied ChatGPT JSON/ZIP exports, with valid `current_node` ancestry selection
  and no browser or account scraping;
- BuildLog narrative files plus schema-specific safe projections of events, plans,
  evaluations, run metadata, and timelines;
- a permission-restricted SQLite FTS index with stable identifiers and hash lineage; and
- a custom code-controlled Evidence Agent using an already-running loopback Ollama model.

The Evidence Agent is not an external agent framework. Code owns its tools, rounds,
queries, hit allocation, context budget, citation membership checks, persistence, and stop
conditions. Model output remains a private candidate.

## Dogfood attempts preserved as evidence

Two early Evidence Agent runs ended safely without producing a supported final claim:

1. `evidence-20260809T071945Z-86dd92b03c47` — one query consumed the available hit budget,
   leaving no useful cross-query coverage. The run returned no grounded claims and kept
   the gap explicit.
2. `evidence-20260809T072151Z-c875e97427be` — four searches returned evidence, but the
   model proposed unrelated technical topics during query planning and still produced no
   grounded claim. The run again stopped without promotion.

No private conversation excerpts are reproduced in this DevLog. The private run receipts
remain under ignored `.soloscale/` storage.

These were useful safe failures: bounded execution stopped, partial evidence remained
inspectable, and neither attempt changed Casebook, BuildLog, a resume, or a publishing
surface.

## Remediation themes

The failures and adversarial review motivated bounded changes rather than a blind retry:

- distribute hits fairly across queries instead of letting the first query monopolize the
  budget;
- reserve capacity across rounds and combine model suggestions with deterministic query
  seeds derived from the operator's question;
- suppress equivalent text within one search result while retaining distinct role/source
  lineage across queries and neighbor expansion, and prevent document-title matches from
  filling every result slot in a long conversation;
- require multi-term exact coverage and preserve distant matched terms in excerpts;
- add bounded adjacent-turn context so a user question can retrieve its assistant answer;
- preserve active ChatGPT ancestry while excluding conflicting sibling branches, and split
  long messages/artifacts into deterministic overlapping segments;
- split mixed Latin/CJK query runs and add bounded CJK bigrams;
- persist the exact fitted excerpt visible to the model, rather than an unseen longer
  search excerpt, and recheck current chunk lineage before accepting a citation;
- preserve first/middle/tail representatives for long adjacent answers, use query-focused
  multilingual windows under fair context pressure, and fall back to lean model records
  while keeping full canonical lineage in receipts;
- bind cited evidence to role, timestamp, locator, title, and a searchable-metadata digest,
  not only its body hash;
- integrity-check stored bodies and their FTS projections, with approved resync rebuilding
  a mismatch even when the raw source hash is unchanged;
- project BuildLog events, plans, evaluations, run metadata, and timeline steps through
  schema-specific safe allowlists instead of indexing arbitrary bodies;
- strengthen control-plane, credential, and hidden-message filtering while continuing to
  describe redaction as best-effort;
- reject stored-body integrity mismatches; and
- keep the optional local model call loopback-only and independent of ambient proxy
  routing.

Control Tower was extended with a Conversation RAG section that shows indexed document/
chunk counts, source coverage, completed/failed/pending agent runs, current state, and one
deterministic exact next action. It does not embed conversation bodies or source locators.

## Safety truth

Retrieved text is untrusted. Code limits its possible effects to bounded local search and
verifies that each declared claim cites an in-context chunk from the same run. Prompt
injection, irrelevant citations, and omitted gaps remain possible, so human review is
required.

## Current verification state

The stable local v0.2 tree passed `229` tests, Ruff, strict `mypy src tests`, and
`git diff --check`. A direct setuptools backend build produced both
`soloscale_ai_os-0.2.0.tar.gz` and `soloscale_ai_os-0.2.0-py3-none-any.whl` in an isolated
temporary output directory. Package inspection confirmed the Conversation RAG guide,
ADR, intake, store, models, Evidence Agent, and tests in the source distribution, and all
runtime modules in the wheel. Fresh adversarial and security reviews both returned PASS
with no unresolved P0/P1. No push, PR, deployment, publication, or automatic content
promotion is implied by this DevLog.

The final private index rebuild discovered and imported `107` documents with `14,025`
chunks and zero source failures: `92` Codex sessions and `15` BuildLog runs. The index and
Control Tower remained mode `0600`; their parent private directories remained `0700`.

A final Evidence Agent dogfood attempt could not cross the current execution sandbox's
Python loopback-network boundary even though the local Ollama health endpoint was
reachable separately. Run `evidence-20260809T091342Z-e45f3f8edad0` stopped during query
planning, wrote a sanitized `EvidenceAgentToolError` receipt, persisted no raw model
response, and created no candidate result. The failure exposed and fixed an observability
gap: transport failures and invalid structured model output now retain distinct sanitized
failure classes and stages. The remaining operational next action is to run the same
scoped CLI command directly from the operator's Mac terminal outside this sandbox, then
review the candidate manually.

A separate retrieval-only golden fixture has run against public synthetic bilingual data:

- Recall@5: `1.0`
- MRR: `1.0`
- store neighbor-expansion recall: `1.0`
- neighbor-expansion forbidden-context precision: `1.0`
- deterministic repeated and rebuilt rankings: `true`

The fixture covers eight queries and three bounded context cases at top-k five. One
targeted local invocation measured `1.863 ms` as the maximum search latency among its
calls. This is one workstation-specific maximum, not a percentile or service commitment.
Semantic faithfulness, answer relevancy, and reasoner-output quality are **not evaluated**;
they remain human-gated or future opt-in evaluation work.

## Remaining limitations

- selected sources are rescanned in full;
- removed sources remain searchable until an explicit index reset and approved resync;
- the index is local single-writer storage;
- the Codex adapter targets an observed internal format;
- ChatGPT has no live-history connector;
- redaction and hidden-message filtering are best-effort;
- retrieval is keyword/metadata based, without vectors or embeddings;
- the golden fixture evaluates retrieval/store neighbor expansion, not the Evidence Agent
  byte allocator, semantic faithfulness, answer relevancy, or local-reasoner output quality;
- citation checks prove same-run membership and current canonical lineage, not semantic
  entailment or factual truth; and
- scheduling, automatic learning plans, content generation, resume generation, and cloud
  operation remain later gates.

## Lesson

Agentic retrieval needs its own evidence and failure model. A useful system does not hide
poor coverage behind fluent text: it preserves what was searched, what was retrieved,
what remained unsupported, and why a human should or should not promote the result.
