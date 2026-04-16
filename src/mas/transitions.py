from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

_LOG_NAME = ".transitions.log"


def log_transition(task_dir: Path, from_state: str, to_state: str, reason: str) -> None:
    """Atomically append a transition line to task_dir/.transitions.log."""
    ts = datetime.now(timezone.utc).isoformat()
    line = f"{ts}|{from_state}|{to_state}|{reason}\n"
    encoded = line.encode()
    path = task_dir / _LOG_NAME
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, encoded)
    finally:
        os.close(fd)


def read_transitions(task_dir: Path, limit: int | None = None) -> list[dict[str, str]]:
    """Read and parse .transitions.log; returns list of dicts (newest last)."""
    path = task_dir / _LOG_NAME
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    if limit is not None:
        lines = lines[-limit:]
    result = []
    for line in lines:
        parts = line.split("|", 3)
        if len(parts) == 4:
            result.append({"timestamp": parts[0], "from": parts[1], "to": parts[2], "reason": parts[3]})
    return result


def ensure_initial_log(task_dir: Path, state: str) -> None:
    """Create .transitions.log with a 'created' entry if it doesn't exist yet."""
    path = task_dir / _LOG_NAME
    if path.exists():
        return
    log_transition(task_dir, "none", state, "created")
