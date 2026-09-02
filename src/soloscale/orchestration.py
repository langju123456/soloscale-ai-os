from __future__ import annotations

from soloscale.event_store import JsonlEventStore
from soloscale.models import RunEvent, TaskEnvelope, TaskStatus
from soloscale.state_machine import validate_transition


class ApprovalRequired(ValueError):
    """Raised when execution starts without an explicit approval receipt."""


class StateContinuityError(ValueError):
    """Raised when caller state does not continue the persisted run history."""


class OrchestrationService:
    """Persist validated state transitions for a task."""

    def __init__(self, event_store: JsonlEventStore) -> None:
        self.event_store = event_store

    def transition(
        self,
        task: TaskEnvelope,
        target: TaskStatus,
        *,
        run_id: str,
        actor: str,
        evidence_receipt: str,
        approval_receipt: str | None = None,
    ) -> RunEvent:
        current = TaskStatus(task.status)
        target = TaskStatus(target)
        prior_transitions = [
            event
            for event in self.event_store.replay(run_id)
            if event.task_id == task.id and event.event_type == "state_transition"
        ]
        if not prior_transitions and current is not TaskStatus.NEW:
            raise StateContinuityError(
                "A run without transition history must start from the NEW state"
            )
        if prior_transitions and prior_transitions[-1].to_status is not current:
            raise StateContinuityError(
                "Task status does not match the last persisted transition for this run"
            )

        validate_transition(current, target)

        evidence_receipt = evidence_receipt.strip()
        if not evidence_receipt:
            raise ValueError("evidence_receipt must not be empty")

        normalized_approval = approval_receipt.strip() if approval_receipt else None
        if target is TaskStatus.EXECUTING and not normalized_approval:
            raise ApprovalRequired(
                "An explicit approval receipt is required before executing this task"
            )

        payload: dict[str, str] = {
            "from_status": current.value,
            "to_status": target.value,
            "evidence_receipt": evidence_receipt,
        }
        if normalized_approval is not None:
            payload["approval_receipt"] = normalized_approval

        event = RunEvent(
            run_id=run_id,
            task_id=task.id,
            event_type="state_transition",
            status=target,
            actor=actor,
            from_status=current,
            to_status=target,
            evidence_receipt=evidence_receipt,
            approval_receipt=normalized_approval,
            payload=payload,
        )
        self.event_store.append(event)
        task.status = target
        return event
