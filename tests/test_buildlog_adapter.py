from soloscale.buildlog_adapter import export_buildlog_iteration
from soloscale.models import DecisionRecord, RunSummary


def test_buildlog_contract() -> None:
    summary = RunSummary(
        id="run-001",
        title="Built the first deterministic router",
        goal="Route work to the minimum required execution surface.",
        context="The workflow previously defaulted to coding agents.",
        problem="Reasoning and execution were unnecessarily coupled.",
        actions=["Defined a Task Envelope.", "Implemented deterministic routing."],
        decisions=[
            DecisionRecord(
                decision="Use code-based routing.",
                reason="It is inspectable and predictable.",
                alternatives_considered=["Let an LLM route every task."],
            )
        ],
        trade_offs=["Rules need explicit maintenance."],
        result="Representative tasks route to Chat, plugins, Codex, Runtime, or Human.",
        lessons=["Multi-agent value comes from boundaries, not agent count."],
        evidence=["Router unit tests passed."],
    )
    payload = export_buildlog_iteration(summary)
    assert payload["id"] == "run-001"
    decisions = payload["decisions"]
    assert isinstance(decisions, list)
    assert decisions[0]["decision"] == "Use code-based routing."
    assert "schema_version" not in decisions[0]
