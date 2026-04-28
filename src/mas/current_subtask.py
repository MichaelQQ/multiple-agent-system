"""Helpers for .current_subtask marker file lifecycle."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _write_current_subtask_marker(parent_dir: Path, role: str, provider: str, pid: int, subtask_id: str) -> None:
    marker = {
        "role": role,
        "provider": provider,
        "pid": pid,
        "start_time_iso": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "subtask_id": subtask_id,
    }
    (parent_dir / ".current_subtask").write_text(json.dumps(marker, indent=2))


def _read_current_subtask_marker(parent_dir: Path) -> dict | None:
    marker_path = parent_dir / ".current_subtask"
    if not marker_path.exists():
        return None
    try:
        return json.loads(marker_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _delete_current_subtask_marker(parent_dir: Path) -> None:
    (parent_dir / ".current_subtask").unlink(missing_ok=True)


def _get_elapsed_s(start_time_iso: str) -> float:
    try:
        start = datetime.fromisoformat(start_time_iso.replace("Z", "+00:00"))
    except ValueError:
        start = datetime.fromisoformat(start_time_iso)
    now = datetime.now(timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return (now - start).total_seconds()