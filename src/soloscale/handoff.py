from __future__ import annotations

from soloscale.models import ExecutionPacket, TaskEnvelope


def packet_from_task(task: TaskEnvelope) -> ExecutionPacket:
    return ExecutionPacket(
        task_id=task.id,
        goal=task.goal,
        constraints=task.constraints,
        frozen_decisions=task.frozen_decisions,
        repository=task.repository,
        branch=task.branch,
        requested_paths=task.requested_paths,
        required_changes=task.required_changes,
        non_goals=task.non_goals,
        acceptance_criteria=task.acceptance_criteria,
        tests_to_run=task.tests_to_run,
        forbidden_changes=[
            "Do not modify unrelated files.",
            (
                "Do not change public APIs, database schemas, authentication boundaries, "
                "dependencies, or deployment configuration unless explicitly approved."
            ),
            "Do not read or expose secrets.",
            "Do not push, deploy, publish, or merge without human approval.",
        ],
        stop_conditions=[
            "The approved plan conflicts with the real repository.",
            "A required change crosses a frozen product or architecture decision.",
            "Tests require destructive data or production access.",
            "A secret, credential, or permission change is required.",
        ],
        expected_return_report=[
            "Files changed and why",
            "Commands executed",
            "Test, lint, type-check, and build results",
            "Any deviation from the packet",
            "Remaining risks and follow-up work",
        ],
    )


def render_packet_markdown(packet: ExecutionPacket) -> str:
    def section(title: str, values: list[str]) -> str:
        if not values:
            return f"## {title}\n\n_None._\n"
        body = "\n".join(f"- {value}" for value in values)
        return f"## {title}\n\n{body}\n"

    lines = [
        f"# Codex Execution Packet — {packet.task_id}",
        "",
        f"Schema version: `{packet.schema_version}`",
        "",
        "## Goal",
        "",
        packet.goal,
        "",
        "## Repository / Branch",
        "",
        f"- Repository: {packet.repository or 'To be selected'}",
        f"- Branch: {packet.branch or 'Create a feature branch'}",
        "",
    ]
    text = "\n".join(lines) + "\n"
    text += section("Constraints", packet.constraints)
    text += section("Frozen Decisions", packet.frozen_decisions)
    text += section("Requested Paths", packet.requested_paths)
    text += section("Required Changes", packet.required_changes)
    text += section("Non-goals", packet.non_goals)
    text += section("Acceptance Criteria", packet.acceptance_criteria)
    text += section("Tests to Run", packet.tests_to_run)
    text += section("Forbidden Changes", packet.forbidden_changes)
    text += section("Stop Conditions", packet.stop_conditions)
    text += section("Expected Return Report", packet.expected_return_report)
    return text
