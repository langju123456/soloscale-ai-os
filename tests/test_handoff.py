from soloscale.handoff import packet_from_task, render_packet_markdown
from soloscale.models import TaskEnvelope


def test_packet_contains_frozen_execution_contract() -> None:
    task = TaskEnvelope(
        id="task-001",
        title="Implement the approved citation change",
        goal="Add source receipts without changing the public report workflow.",
        repository="example/repository",
        branch="feat/source-receipts",
        requested_paths=["src/citations.py"],
        constraints=["Do not change the public response schema."],
        frozen_decisions=["Missing citations remain explicit."],
        required_changes=["Attach a source identifier to each claim."],
        acceptance_criteria=["Every claim has inspectable evidence."],
        tests_to_run=["pytest tests/test_citations.py"],
        non_goals=["Redesign the report UI."],
    )

    packet = packet_from_task(task)

    assert packet.branch == task.branch
    assert packet.constraints == task.constraints
    assert packet.frozen_decisions == task.frozen_decisions
    assert packet.required_changes == task.required_changes
    assert packet.tests_to_run == task.tests_to_run
    markdown = render_packet_markdown(packet)
    assert "Schema version: `0.1`" in markdown
    assert "Do not change the public response schema." in markdown
    assert "feat/source-receipts" in markdown
    assert "Attach a source identifier to each claim." in markdown
    assert "pytest tests/test_citations.py" in markdown
