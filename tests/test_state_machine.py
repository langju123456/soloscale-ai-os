import pytest

from soloscale.models import TaskStatus
from soloscale.state_machine import InvalidTransition, validate_transition


def test_valid_transition() -> None:
    result = validate_transition(TaskStatus.NEW, TaskStatus.TRIAGED)
    assert result.target is TaskStatus.TRIAGED


def test_invalid_transition() -> None:
    with pytest.raises(InvalidTransition):
        validate_transition(TaskStatus.NEW, TaskStatus.CLOSED)


def test_blocked_cannot_jump_directly_to_execution() -> None:
    with pytest.raises(InvalidTransition):
        validate_transition(TaskStatus.BLOCKED, TaskStatus.EXECUTING)


@pytest.mark.parametrize(
    "target",
    [TaskStatus.PLANNED, TaskStatus.APPROVED, TaskStatus.FIXING],
)
def test_blocked_must_return_through_triage(target: TaskStatus) -> None:
    with pytest.raises(InvalidTransition):
        validate_transition(TaskStatus.BLOCKED, target)
