from __future__ import annotations

from soloscale.models import RouteDecision, Surface, TaskEnvelope


def route_task(task: TaskEnvelope) -> RouteDecision:
    """Route a task to the smallest surface set that can complete it safely."""

    rationale: list[str] = []
    secondary: list[Surface] = []

    gate = (
        task.high_risk_action
        or task.irreversible_action
        or task.public_action
        or task.risk.value in {"high", "critical"}
    )

    if task.requires_realtime or task.requires_scheduled_execution:
        primary = Surface.RUNTIME
        rationale.append(
            "The task requires realtime, scheduled, repeated, or unattended execution."
        )
        if task.requires_local_files or task.requires_terminal:
            secondary.append(Surface.CODEX)

    elif task.requires_local_files or task.requires_terminal:
        primary = Surface.CODEX
        rationale.append(
            "The task depends on local repository state, filesystem access, terminal commands, "
            "tests, builds, or uncommitted changes."
        )
        secondary.append(Surface.CHAT)

    elif task.plugin_can_complete:
        primary = Surface.PLUGIN
        rationale.append(
            f"The connected {task.plugin_name} plugin can perform the required online action."
        )
        secondary.append(Surface.CHAT)

    else:
        primary = Surface.CHAT
        rationale.append(
            "The task is primarily research, reasoning, planning, review, or communication and "
            "does not require local execution."
        )

    if gate:
        secondary.append(Surface.HUMAN)
        rationale.append(
            "Risk, public visibility, cost, or irreversibility requires an explicit human gate."
        )

    # Stable de-duplication while preserving order.
    deduped = list(dict.fromkeys(secondary))
    return RouteDecision(
        primary=primary,
        secondary=deduped,
        rationale=rationale,
        human_gate_required=gate,
    )
