import pytest
from pydantic import ValidationError

from soloscale.models import (
    DecisionRecord,
    ExecutionPacket,
    RouteDecision,
    RunEvent,
    RunSummary,
    Surface,
    TaskEnvelope,
    TaskStatus,
)


@pytest.mark.parametrize(
    ("model_type", "payload"),
    [
        (
            TaskEnvelope,
            {
                "title": "Strict task contract",
                "goal": "Reject fields that the task contract does not recognize.",
            },
        ),
        (
            RouteDecision,
            {"primary": Surface.CHAT, "rationale": ["No execution state is needed."]},
        ),
        (
            RunEvent,
            {
                "run_id": "run-001",
                "task_id": "task-001",
                "event_type": "command",
                "status": TaskStatus.EXECUTING,
                "actor": "codex",
            },
        ),
        (
            ExecutionPacket,
            {"task_id": "task-001", "goal": "Make the handoff complete and inspectable."},
        ),
        (DecisionRecord, {"decision": "Use strict models.", "reason": "Typos must fail."}),
        (
            RunSummary,
            {
                "id": "run-001",
                "title": "Strict contract run",
                "goal": "Reject unknown public fields.",
                "context": "Contracts cross execution boundaries.",
                "problem": "Silent extra fields can hide producer mistakes.",
                "actions": ["Enabled extra-field rejection."],
                "decisions": [],
                "trade_offs": ["Schema evolution must be deliberate."],
                "result": "Unknown fields fail validation.",
                "lessons": ["Version contracts explicitly."],
                "evidence": ["Model validation test."],
            },
        ),
    ],
)
def test_public_contracts_are_versioned_and_forbid_unknown_fields(
    model_type: type[TaskEnvelope]
    | type[RouteDecision]
    | type[RunEvent]
    | type[ExecutionPacket]
    | type[DecisionRecord]
    | type[RunSummary],
    payload: dict[str, object],
) -> None:
    model = model_type.model_validate(payload)
    assert model.model_dump()["schema_version"] == "0.1"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model_type.model_validate({**payload, "unexpected_field": True})

    with pytest.raises(ValidationError, match="Input should be '0.1'"):
        model_type.model_validate({**payload, "schema_version": "0.2"})
