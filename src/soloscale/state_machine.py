from __future__ import annotations

from dataclasses import dataclass

from soloscale.models import TaskStatus

ALLOWED_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.NEW: {TaskStatus.TRIAGED, TaskStatus.BLOCKED, TaskStatus.FAILED},
    TaskStatus.TRIAGED: {TaskStatus.PLANNED, TaskStatus.BLOCKED, TaskStatus.FAILED},
    TaskStatus.PLANNED: {TaskStatus.APPROVED, TaskStatus.BLOCKED, TaskStatus.FAILED},
    TaskStatus.APPROVED: {TaskStatus.EXECUTING, TaskStatus.BLOCKED, TaskStatus.FAILED},
    TaskStatus.EXECUTING: {
        TaskStatus.VERIFYING,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
    },
    TaskStatus.VERIFYING: {
        TaskStatus.REVIEWING,
        TaskStatus.FIXING,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
    },
    TaskStatus.REVIEWING: {
        TaskStatus.ACCEPTED,
        TaskStatus.FIXING,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
    },
    TaskStatus.FIXING: {
        TaskStatus.VERIFYING,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
    },
    TaskStatus.ACCEPTED: {TaskStatus.CLOSED},
    TaskStatus.BLOCKED: {TaskStatus.TRIAGED, TaskStatus.FAILED},
    TaskStatus.FAILED: set(),
    TaskStatus.CLOSED: set(),
}


class InvalidTransition(ValueError):
    pass


@dataclass(frozen=True)
class Transition:
    current: TaskStatus
    target: TaskStatus


def validate_transition(current: TaskStatus, target: TaskStatus) -> Transition:
    current = TaskStatus(current)
    target = TaskStatus(target)
    allowed = ALLOWED_TRANSITIONS[current]
    if target not in allowed:
        raise InvalidTransition(f"Invalid transition: {current} -> {target}")
    return Transition(current=current, target=target)
