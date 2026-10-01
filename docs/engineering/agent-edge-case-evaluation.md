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
without calling the grounded-drafting reasoner. `PROMPT_VERSION` is `evidence-agent-v3`; its
grounded prompt asks the reasoner to preserve attribution and record uncertainty when supplied
records conflict. It does not change result schemas or add semantic conflict resolution.

`EvidenceAgentTimeoutError` subclasses `EvidenceAgentToolError`, preserving safe failure
receipts while distinguishing injected reasoner, search, neighbor-expansion, and citation-lineage
timeouts. There are no automatic retries or fabricated wall-clock deadlines.

Validation for this slice is the focused evaluator above, `tests/test_evidence_agent.py`, Ruff,
and strict mypy on the affected code paths.
