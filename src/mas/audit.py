"""Audit event logging for mas tasks.

Appends JSONL events to {task_dir}/audit.jsonl.
Fields: timestamp (UTC ISO), event, role, provider, task_id, subtask_id,
        status, duration_s, summary, details.
"""
from __future__ import annotations

import json
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def append_event(
    task_dir: Path,
    *,
    event: str,
    task_id: str,
    role: str | None = None,
    provider: str | None = None,
    subtask_id: str | None = None,
    status: str | None = None,
    duration_s: float | None = None,
    summary: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    """Append a JSONL audit event to {task_dir}/audit.jsonl."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "role": role,
        "provider": provider,
        "task_id": task_id,
        "subtask_id": subtask_id,
        "status": status,
        "duration_s": duration_s,
        "summary": summary,
        "details": details if details is not None else {},
    }
    audit_path = task_dir / "audit.jsonl"
    task_dir.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
        f.flush()


def read_events(
    task_dir: Path,
    *,
    role: str | None = None,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict[str, Any]]:
    """Read and parse audit events from {task_dir}/audit.jsonl.

    Corrupt lines are skipped with a UserWarning; remaining valid lines
    are returned.  Optional filters: role, status, since, until (ISO timestamps).
    """
    audit_path = task_dir / "audit.jsonl"
    if not audit_path.exists():
        return []

    since_dt = _parse_ts(since) if since else None
    until_dt = _parse_ts(until) if until else None

    events: list[dict[str, Any]] = []
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            warnings.warn(f"Skipping malformed audit line: {line!r}", UserWarning, stacklevel=2)
            continue

        if role is not None and entry.get("role") != role:
            continue
        if status is not None and entry.get("status") != status:
            continue
        if since_dt is not None:
            ts_dt = _parse_ts(entry.get("timestamp", ""))
            if ts_dt is None or ts_dt < since_dt:
                continue
        if until_dt is not None:
            ts_dt = _parse_ts(entry.get("timestamp", ""))
            if ts_dt is None or ts_dt > until_dt:
                continue

        events.append(entry)

    return events


def _parse_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
