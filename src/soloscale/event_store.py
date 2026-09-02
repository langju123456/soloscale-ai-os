from __future__ import annotations

from pathlib import Path

from soloscale.models import RunEvent


class JsonlEventStore:
    """Append-only local event store suitable for inspection and replay."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, event: RunEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")

    def read_all(self) -> list[RunEvent]:
        if not self.path.exists():
            return []
        events: list[RunEvent] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped:
                    events.append(RunEvent.model_validate_json(stripped))
        return events

    def replay(self, run_id: str | None = None) -> list[RunEvent]:
        """Replay persisted events, optionally limiting them to one run."""

        events = self.read_all()
        if run_id is None:
            return events
        return [event for event in events if event.run_id == run_id]
