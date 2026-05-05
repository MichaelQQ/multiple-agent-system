from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import audit as audit_mod
from . import graph as graph_mod


def _parse_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _read_transitions(task_dir: Path) -> list[dict[str, str]]:
    # `.transitions.log` is what tick writes in production; `transitions.jsonl`
    # is preserved as a fallback for older fixtures and tests.
    path = None
    for name in (".transitions.log", "transitions.jsonl"):
        candidate = task_dir / name
        if candidate.exists():
            path = candidate
            break
    if path is None:
        return []
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 3)
        result.append({
            "timestamp": parts[0] if len(parts) > 0 else "",
            "from": parts[1] if len(parts) > 1 else "",
            "to": parts[2] if len(parts) > 2 else "",
            "reason": parts[3] if len(parts) > 3 else "",
        })
    return result


def _read_graph_dict(task_dir: Path) -> dict[str, Any]:
    """Return graph.json as a serialisable dict, with each node/edge trimmed to
    fields useful for trace consumers (omit large `handoff`/`artifacts` blobs).
    """
    g = graph_mod.read_graph(task_dir)
    nodes = []
    for n in g.nodes:
        summary = (n.summary or "").splitlines()[0][:200] if n.summary else None
        feedback = (n.feedback or "").splitlines()[0][:200] if n.feedback else None
        nodes.append({
            "subtask_id": n.subtask_id,
            "role": n.role,
            "cycle": n.cycle,
            "status": n.status,
            "verdict": n.verdict,
            "summary": summary,
            "feedback": feedback,
        })
    edges = [
        {
            "from_id": e.from_id,
            "to_id": e.to_id,
            "kind": e.kind,
            "reason": (e.reason or "").splitlines()[0][:200] if e.reason else None,
        }
        for e in g.edges
    ]
    return {"nodes": nodes, "edges": edges}


def _subtask_result_dir(task_dir: Path, subtask_id: str, cycle: int) -> Path:
    if cycle == 0:
        return task_dir / subtask_id
    return task_dir / f"{subtask_id}-rev-{cycle}"


def build_trace(task_dir: Path, *, now: datetime | None = None) -> dict[str, Any]:
    if now is None:
        now = datetime.now(timezone.utc)

    task_path = task_dir / "task.json"
    task_data: dict[str, Any] = {}
    if task_path.exists():
        task_data = json.loads(task_path.read_text(encoding="utf-8"))

    task_id = task_data.get("id", task_dir.name)
    goal = task_data.get("goal", "")

    events = audit_mod.read_events(task_dir)

    pending: dict[tuple[str, int], dict[str, Any]] = {}
    stages: list[dict[str, Any]] = []

    for ev in events:
        ev_type = ev.get("event")
        subtask_id = ev.get("subtask_id")
        if not subtask_id:
            continue
        cycle = int(ev.get("details", {}).get("cycle", 0))
        key = (subtask_id, cycle)

        if ev_type == "dispatch":
            pending[key] = ev
        elif ev_type == "completion":
            dispatch_ev = pending.pop(key, None)
            if dispatch_ev is None:
                continue

            role = ev.get("role") or dispatch_ev.get("role", "")
            started_at = dispatch_ev.get("timestamp")
            ended_at = ev.get("timestamp")
            duration_s = ev.get("duration_s")
            status = ev.get("status", "success")

            provider = ev.get("provider") or dispatch_ev.get("provider") or ""
            cost_usd: float | None = None
            tokens_in: int | None = None
            tokens_out: int | None = None
            result_path = _subtask_result_dir(task_dir, subtask_id, cycle) / "result.json"
            if not result_path.exists():
                # Also check the subtasks/ subdirectory layout used by board helpers
                alt = task_dir / "subtasks" / subtask_id / "result.json"
                if alt.exists():
                    result_path = alt
            if result_path.exists():
                try:
                    result_data = json.loads(result_path.read_text(encoding="utf-8"))
                    cost_usd = result_data.get("cost_usd")
                    tokens_in = result_data.get("tokens_in")
                    tokens_out = result_data.get("tokens_out")
                except (json.JSONDecodeError, OSError):
                    pass

            stages.append({
                "subtask_id": subtask_id,
                "role": role,
                "cycle": f"rev-{cycle}",
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_s": duration_s,
                "status": status,
                "cost_usd": cost_usd,
                "provider": provider,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
            })

    for (subtask_id, cycle), dispatch_ev in pending.items():
        started_at = dispatch_ev.get("timestamp")
        role = dispatch_ev.get("role", "")
        started_dt = _parse_ts(started_at) if started_at else None
        duration_s = (now - started_dt).total_seconds() if started_dt else None

        stages.append({
            "subtask_id": subtask_id,
            "role": role,
            "cycle": f"rev-{cycle}",
            "started_at": started_at,
            "ended_at": None,
            "duration_s": duration_s,
            "status": "running",
            "cost_usd": None,
            "provider": dispatch_ev.get("provider") or "",
            "tokens_in": None,
            "tokens_out": None,
        })

    stages.sort(key=lambda s: s.get("started_at") or "")

    top_started_at: str | None = stages[0]["started_at"] if stages else None

    trans_list = _read_transitions(task_dir)
    top_ended_at: str | None
    if trans_list:
        last = trans_list[-1]
        if last.get("to", "") in ("done", "failed"):
            top_ended_at = last["timestamp"]
        else:
            top_ended_at = now.isoformat()
    else:
        top_ended_at = now.isoformat()

    total_duration_s = sum(s["duration_s"] for s in stages if s["duration_s"] is not None)
    total_cost_usd = sum(s["cost_usd"] for s in stages if s["cost_usd"] is not None)

    return {
        "task_id": task_id,
        "goal": goal,
        "started_at": top_started_at,
        "ended_at": top_ended_at,
        "total_duration_s": total_duration_s,
        "total_cost_usd": total_cost_usd,
        "stages": stages,
        "graph": _read_graph_dict(task_dir),
        "transitions": trans_list,
    }
