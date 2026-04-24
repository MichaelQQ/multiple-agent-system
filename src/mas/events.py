from __future__ import annotations

from pathlib import Path
from typing import Any

from . import audit as _audit

_COLUMNS = ("doing", "done", "failed")


def read_board_events(
    mas_dir: Path,
    *,
    task: str | None = None,
    role: str | None = None,
    status: str | None = None,
    event: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict[str, Any]]:
    all_events: list[dict[str, Any]] = []
    tasks_root = mas_dir / "tasks"
    for col in _COLUMNS:
        col_dir = tasks_root / col
        if not col_dir.is_dir():
            continue
        for task_dir in sorted(col_dir.iterdir()):
            if not task_dir.is_dir():
                continue
            task_id = task_dir.name
            evts = _audit.read_events(task_dir, role=role, status=status, since=since, until=until)
            for e in evts:
                if not e.get("task_id"):
                    e = dict(e, task_id=task_id)
                all_events.append(e)

    if task is not None:
        all_events = [e for e in all_events if e.get("task_id") == task]
    if event is not None:
        all_events = [e for e in all_events if e.get("event") == event]

    all_events.sort(key=lambda e: e.get("timestamp") or "")
    return all_events
