import json
from pathlib import Path
from typing import Optional

from .schemas import Result, Task


def aggregate_costs_by_role(task_dir: Path) -> dict:
    """Aggregate subtask costs grouped by role.

    Scans task_dir/subtasks/*/result.json and groups by the role field
    from the sibling task.json.

    Returns dict mapping role -> {"count": int, "cost_usd": float, "tokens_in": int, "tokens_out": int}.
    """
    rollup: dict[str, dict[str, object]] = {}
    subtasks_dir = task_dir / "subtasks"
    if not subtasks_dir.exists():
        return rollup
    for sub_dir in subtasks_dir.iterdir():
        if not sub_dir.is_dir():
            continue
        task_json = sub_dir / "task.json"
        if not task_json.exists():
            continue
        try:
            raw_task = json.loads(task_json.read_text())
            role = raw_task.get("role")
            if not role:
                continue
        except Exception:
            continue
        result_json = sub_dir / "result.json"
        if not result_json.exists():
            continue
        try:
            raw_result = json.loads(result_json.read_text())
            cost_usd = raw_result.get("cost_usd") or 0.0
            tokens_in = raw_result.get("tokens_in") or 0
            tokens_out = raw_result.get("tokens_out") or 0
        except Exception:
            continue
        if role not in rollup:
            rollup[role] = {"count": 0, "cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0}
        rollup[role]["count"] += 1
        rollup[role]["cost_usd"] = float(rollup[role]["cost_usd"]) + float(cost_usd)
        rollup[role]["tokens_in"] = int(rollup[role]["tokens_in"]) + int(tokens_in)
        rollup[role]["tokens_out"] = int(rollup[role]["tokens_out"]) + int(tokens_out)
    return rollup


def at_risk_tasks(board_root: Path, threshold: float = 0.8) -> list:
    """Return list of task IDs in doing/ whose spent budget >= threshold * budget.

    Reads each task in board_root/doing/*/task.json for cost_budget_usd,
    then aggregates actual spend from subtasks/*/result.json.
    Handles missing budgets gracefully (skips those tasks).
    """
    at_risk = []
    # Check both board_root/doing and board_root/tasks/doing
    for base in [board_root, board_root / "tasks"]:
        doing_dir = base / "doing"
        if not doing_dir.exists():
            continue
        for task_dir in doing_dir.iterdir():
            if not task_dir.is_dir():
                continue
            task_json = task_dir / "task.json"
            if not task_json.exists():
                continue
            try:
                raw_task = json.loads(task_json.read_text())
                task_id = raw_task.get("task_id") or raw_task.get("id")
                budget = raw_task.get("cost_budget_usd")
            except Exception:
                continue
            if budget is None or float(budget) <= 0:
                continue
            rollup = aggregate_costs_by_role(task_dir)
            total_spent = sum(float(v["cost_usd"]) for v in rollup.values())
            if total_spent > threshold * float(budget):
                at_risk.append(task_id)
    return at_risk
