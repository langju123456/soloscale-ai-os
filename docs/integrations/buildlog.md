# BuildLog Integration

The user's existing BuildLog project already accepts structured development evidence with these fields:

```text
id
title
goal
context
problem
actions
decisions
trade_offs
result
lessons
evidence
audience
metadata
```

SoloScale's `RunSummary` intentionally mirrors that contract.

## Export

```bash
soloscale buildlog-export .soloscale/tasks/<task-id>/run-summary.json
```

This creates:

```text
.soloscale/tasks/<task-id>/buildlog-iteration.json
```

Copy that reviewed file into the BuildLog project and run the existing BuildLog workflow.

## Product boundary

SoloScale owns task routing, execution evidence, and workflow control.

BuildLog owns evidence-aware story transformation, evaluation, publishing packages, and human-controlled delivery.

The integration should remain adapter-based rather than merging both products into one codebase.
