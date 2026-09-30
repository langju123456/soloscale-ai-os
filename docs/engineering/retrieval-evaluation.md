# Offline retrieval evaluation reports

Run the existing public synthetic Conversation fixture and the existing canonical Resume
fact-ranking fixture locally:

```bash
.venv/bin/python scripts/evaluate_retrieval.py
```

The command creates one new private run directory under `.soloscale/retrieval-evals/`, with
one combined `report.json` and `report.md` for both datasets. The run directory is `0700` and
the reports are `0600`. It uses isolated temporary KnowledgeStore indexes and never opens the
operator's knowledge index. A custom root still receives a new run subdirectory:

```bash
.venv/bin/python scripts/evaluate_retrieval.py --output-root /private/evals
```

To compare a new run with one exact prior run directory (or its exact `report.json`), capture
the printed path. Dataset hash, corpus hash, and retrieval configuration must match; the report
records metric deltas and changed rankings without changing labels or thresholds:

```bash
baseline_run="$(.venv/bin/python scripts/evaluate_retrieval.py)"
.venv/bin/python scripts/evaluate_retrieval.py --compare "$baseline_run"
```

Conversation results are deterministic `KnowledgeStore` retrieval. Context metrics are
explicitly limited to store-neighbor expansion. Resume results are canonical CandidateProfile
fact ranking/compaction, not KnowledgeStore retrieval or end-to-end RAG. Both fixtures are
fixed curated developer regression data; they do not establish semantic faithfulness, answer
correctness, held-out quality, or generalization.
