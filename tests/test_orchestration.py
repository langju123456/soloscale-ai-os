from enum import StrEnum
from pathlib import Path

import pytest

from soloscale.event_store import JsonlEventStore
from soloscale.models import RiskLevel, TaskEnvelope, TaskStatus
from soloscale.orchestration import (
    ApprovalRequired,
    OrchestrationService,
    StateContinuityError,
)
from soloscale.state_machine import InvalidTransition


def task_at(status: TaskStatus, **kwargs: object) -> TaskEnvelope:
    return TaskEnvelope.model_validate(
        {
            "id": "task-001",
            "title": "Execute an approved change",
            "goal": "Apply the approved change and preserve inspectable evidence.",
            "status": status,
            **kwargs,
        }
    )


def advance_to_approved(
    service: OrchestrationService,
    task: TaskEnvelope,
    *,
    run_id: str = "run-001",
) -> None:
    for target in (TaskStatus.TRIAGED, TaskStatus.PLANNED, TaskStatus.APPROVED):
        service.transition(
            task,
            target,
            run_id=run_id,
            actor="operator",
            evidence_receipt=f"evidence://{target.value.lower()}",
        )


def test_transition_persists_from_to_and_evidence_receipts(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path / "events.jsonl")
    service = OrchestrationService(store)
    task = task_at(TaskStatus.NEW, public_action=True)
    advance_to_approved(service, task)

    event = service.transition(
        task,
        TaskStatus.EXECUTING,
        run_id="run-001",
        actor="operator",
        evidence_receipt="issue://123#approved-plan",
        approval_receipt="approval://operator/456",
    )

    assert task.status is TaskStatus.EXECUTING
    assert event.from_status is TaskStatus.APPROVED
    assert event.to_status is TaskStatus.EXECUTING
    assert event.evidence_receipt == "issue://123#approved-plan"
    assert event.approval_receipt == "approval://operator/456"
    assert store.replay("run-001")[-1] == event


def test_execution_requires_explicit_approval_receipt(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path / "events.jsonl")
    service = OrchestrationService(store)
    task = task_at(TaskStatus.NEW)
    advance_to_approved(service, task)

    with pytest.raises(ApprovalRequired):
        service.transition(
            task,
            TaskStatus.EXECUTING,
            run_id="run-001",
            actor="codex",
            evidence_receipt="plan://approved",
        )

    assert task.status is TaskStatus.APPROVED
    assert len(store.read_all()) == 3


def test_mutating_risk_after_approval_cannot_bypass_receipt(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path / "events.jsonl")
    service = OrchestrationService(store)
    task = task_at(TaskStatus.NEW, risk="high", public_action=True)
    advance_to_approved(service, task)

    task.risk = RiskLevel.LOW
    task.public_action = False

    with pytest.raises(ApprovalRequired):
        service.transition(
            task,
            TaskStatus.EXECUTING,
            run_id="run-001",
            actor="codex",
            evidence_receipt="plan://approved",
        )


def test_foreign_string_enum_cannot_bypass_execution_approval(tmp_path: Path) -> None:
    class ForeignStatus(StrEnum):
        EXECUTING = "EXECUTING"

    store = JsonlEventStore(tmp_path / "events.jsonl")
    service = OrchestrationService(store)
    task = task_at(TaskStatus.NEW)
    advance_to_approved(service, task)

    with pytest.raises(ApprovalRequired):
        service.transition(
            task,
            ForeignStatus.EXECUTING,  # type: ignore[arg-type]
            run_id="run-001",
            actor="codex",
            evidence_receipt="plan://approved",
        )

    assert task.status is TaskStatus.APPROVED
    assert len(store.read_all()) == 3


def test_forged_or_stale_task_status_fails_continuity_check(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path / "events.jsonl")
    service = OrchestrationService(store)
    forged = task_at(TaskStatus.APPROVED)

    with pytest.raises(StateContinuityError):
        service.transition(
            forged,
            TaskStatus.EXECUTING,
            run_id="run-001",
            actor="codex",
            evidence_receipt="plan://forged",
            approval_receipt="approval://operator/456",
        )

    task = task_at(TaskStatus.NEW)
    service.transition(
        task,
        TaskStatus.TRIAGED,
        run_id="run-002",
        actor="operator",
        evidence_receipt="evidence://triaged",
    )
    task.status = TaskStatus.PLANNED

    with pytest.raises(StateContinuityError):
        service.transition(
            task,
            TaskStatus.APPROVED,
            run_id="run-002",
            actor="operator",
            evidence_receipt="evidence://stale",
        )


def test_blocked_task_cannot_bypass_approval_state(tmp_path: Path) -> None:
    service = OrchestrationService(JsonlEventStore(tmp_path / "events.jsonl"))
    task = task_at(TaskStatus.NEW, public_action=True)
    service.transition(
        task,
        TaskStatus.BLOCKED,
        run_id="run-001",
        actor="operator",
        evidence_receipt="blocker://discovered",
    )

    with pytest.raises(InvalidTransition):
        service.transition(
            task,
            TaskStatus.EXECUTING,
            run_id="run-001",
            actor="operator",
            evidence_receipt="blocker://resolved",
            approval_receipt="approval://operator/456",
        )
