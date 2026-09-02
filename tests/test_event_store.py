from pathlib import Path

from soloscale.event_store import JsonlEventStore
from soloscale.models import RunEvent, TaskStatus


def test_event_store_persists_and_replays_events(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path / "nested" / "events.jsonl")
    first = RunEvent(
        run_id="run-001",
        task_id="task-001",
        event_type="command",
        status=TaskStatus.EXECUTING,
        actor="codex",
        payload={"command": "pytest"},
    )
    second = RunEvent(
        run_id="run-002",
        task_id="task-002",
        event_type="command",
        status=TaskStatus.EXECUTING,
        actor="codex",
        payload={"command": "ruff check ."},
    )

    store.append(first)
    store.append(second)

    assert store.read_all() == [first, second]
    assert store.replay("run-001") == [first]
    assert store.replay("missing-run") == []
    assert '"schema_version":"0.1"' in store.path.read_text(encoding="utf-8")
