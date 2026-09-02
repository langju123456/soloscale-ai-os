from __future__ import annotations

from soloscale.models import RunSummary


def export_buildlog_iteration(summary: RunSummary) -> dict[str, object]:
    """Return the evidence contract already used by the user's BuildLog project."""

    return {
        "id": summary.id,
        "title": summary.title,
        "goal": summary.goal,
        "context": summary.context,
        "problem": summary.problem,
        "actions": summary.actions,
        "decisions": [
            decision.model_dump(exclude={"schema_version"}) for decision in summary.decisions
        ],
        "trade_offs": summary.trade_offs,
        "result": summary.result,
        "lessons": summary.lessons,
        "evidence": summary.evidence,
        "audience": summary.audience,
        "metadata": summary.metadata,
    }
