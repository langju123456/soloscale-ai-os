# Evidence Agent edge-case evaluation

Run the focused synthetic evaluation from the repository root:

```bash
.venv/bin/python scripts/evaluate_agent_edge_cases.py
```

The command prints one fresh private run directory under
`.soloscale/agent-edge-case-evals/`. Each run is non-overwriting (`0700`) and contains
`report.json`, `report.md`, a pytest JUnit XML report, and a pytest log (`0600`). The report
records the source HEAD, source/test hashes, dirty state, actual pytest command and exit code,
per-test status, and coverage of five required categories. It reports PASS only when pytest
exits zero, every category is present and passed, no test was skipped, and the JUnit report was
parsed successfully. A report is written even when pytest fails.

If the pre-existing `.soloscale` directory or its `agent-edge-case-evals` child is a symlink
or an unsafe non-directory, the command rejects the output location before running pytest or
writing a report.

The checks use synthetic evidence, scripted control-flow fault injection, and an injected
Ollama response reader. They make zero live model, provider, or network calls. They cover:

- abstention when no fitted evidence exists, including no grounded-draft call;
- preservation of conflicting source records and an attributed, unresolved scripted candidate;
- UTF-8 context budgeting that keeps a focused critical tail;
- sanitized timeout receipts for reasoner, retrieval, and neighbor operations;
- malformed JSON and schema-invalid Ollama output that fail closed without an accepted result.

The conflict test proves record retention, attribution, and human-review routing. It does not
claim automatic semantic contradiction detection. This evaluation also does not measure semantic
accuracy, load, P95 latency, or production stability.

## Development record

This slice adds a deterministic abstention draft when no retrieved evidence fits model context,
so the agent produces empty claims and references plus an explicit information-insufficient gap
without calling the grounded-drafting reasoner. The current `PROMPT_VERSION` is
`evidence-agent-v4`: disputed statements must be attributed to their sources, unresolved conflicts
must be stated explicitly, and empty gap lists must not contain filler. It does not change result
schemas or add semantic conflict resolution.

`EvidenceAgentTimeoutError` subclasses `EvidenceAgentToolError`, preserving safe failure
receipts while distinguishing injected reasoner, search, neighbor-expansion, and citation-lineage
timeouts. There are no automatic retries or fabricated wall-clock deadlines.

Validation for this slice is the focused evaluator above, `tests/test_evidence_agent.py`, Ruff,
and strict mypy on the affected code paths.

## Opt-in local-model structural run

When a human has verified an already-installed loopback Ollama model, run:

```bash
.venv/bin/python scripts/evaluate_agent_live.py
```

This executes three public synthetic development cases in isolated temporary indexes and saves
actual candidates, safe errors, model inventory identity, and call profiles under
`.soloscale/agent-live-evals/`. It performs structural checks only; every actual answer remains
`PENDING_HUMAN_REVIEW` for semantic quality, conflict interpretation, and usefulness.

`--case` is a diagnostic subset: its receipt records `selected_structural_passed`, but marks the
run PARTIAL and returns nonzero. Only the default completed three-case suite can set top-level
`passed` and `full_suite_passed` to true.

### Observed local run: 2026-10-01

An installed `qwen3:8b` completed all three structural gates with `evidence-agent-v4`:
empty evidence returned zero claims/references; conflicting migration records retained both
citations and explicitly stated their disagreement; a 700-byte UTF-8 context preserved the
critical tail statement and its citation. The run made eight local model completions.

The v3 baseline had repeated the conflict question as a gap and emitted a no-gap filler.
The v4 run expressed the conflict explicitly. These are qualitative observations on three
curated development cases; the answers remain candidates requiring human review, and no
general semantic accuracy or production reliability result is inferred.

The long-context candidate also listed unspecified implementation details as unsupported.
Whether that gap is useful for an identifier-only question remains a qualitative review item;
the structural gate does not assess gap wording or prove complete prompt compliance.
