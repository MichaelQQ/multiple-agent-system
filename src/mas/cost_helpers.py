import json
import math
import statistics
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


def compute_role_baselines(mas_dir, percentile='median'):
    """Scan all done/ tasks, read each subtask's result.json for cost_usd, group by role.

    Returns dict mapping role -> baseline cost.
    For 'median' uses statistics.median.
    For 'p75' uses the 75th percentile.
    Skip subtasks with null/zero cost.
    """
    mas_path = Path(mas_dir)

    # Scan done/ tasks from both board layouts
    done_dirs = [mas_path / "done", mas_path / "tasks" / "done"]

    costs_by_role: dict[str, list[float]] = {}

    for done_dir in done_dirs:
        if not done_dir.exists():
            continue
        for task_dir in done_dir.iterdir():
            if not task_dir.is_dir():
                continue

            task_json = task_dir / "task.json"
            if not task_json.exists():
                continue
            try:
                raw_task = json.loads(task_json.read_text())
                role = raw_task.get("role")
                if not role:
                    continue
            except Exception:
                continue

            result_json = task_dir / "result.json"
            if not result_json.exists():
                continue
            try:
                raw_result = json.loads(result_json.read_text())
                cost = raw_result.get("cost_usd")
                if cost is None or cost == 0:
                    continue
                cost = float(cost)
            except Exception:
                continue

            if role not in costs_by_role:
                costs_by_role[role] = []
            costs_by_role[role].append(cost)

    if not costs_by_role:
        return {}

    baselines = {}
    for role, costs in costs_by_role.items():
        if not costs:
            continue
        if percentile == 'median':
            baselines[role] = statistics.median(costs)
        elif percentile == 'p75':
            sorted_costs = sorted(costs)
            n = len(sorted_costs)
            rank = math.ceil(0.75 * n)
            baselines[role] = sorted_costs[rank - 1]
        else:
            raise ValueError(f"Unsupported percentile: {percentile}")

    return baselines


def detect_anomalies(mas_dir, multiplier=2.0):
    """Compute baselines, then scan doing/ and done/ tasks.

    For each subtask whose cost exceeds multiplier × baseline for its role,
    emit a dict with keys: task_id, role, actual_cost, baseline, delta, multiplier_exceeded.
    Return list sorted by delta descending.
    """
    mas_path = Path(mas_dir)
    baselines = compute_role_baselines(mas_path)

    if not baselines:
        return []

    anomalies = []

    # Scan both board layouts
    for col in ["doing", "done"]:
        for base in [mas_path, mas_path / "tasks"]:
            col_dir = base / col
            if not col_dir.exists():
                continue

            for task_dir in col_dir.iterdir():
                if not task_dir.is_dir():
                    continue

                task_json = task_dir / "task.json"
                if not task_json.exists():
                    continue
                try:
                    raw_task = json.loads(task_json.read_text())
                    role = raw_task.get("role")
                    if not role:
                        continue
                except Exception:
                    continue

                if role not in baselines:
                    continue

                result_json = task_dir / "result.json"
                if not result_json.exists():
                    continue
                try:
                    raw_result = json.loads(result_json.read_text())
                    cost = raw_result.get("cost_usd")
                    if cost is None or cost == 0:
                        continue
                    cost = float(cost)
                except Exception:
                    continue

                baseline = baselines[role]
                if cost > multiplier * baseline:
                    anomalies.append({
                        "task_id": task_dir.name,
                        "role": role,
                        "actual_cost": cost,
                        "baseline": baseline,
                        "delta": cost - baseline,
                        "multiplier_exceeded": cost / baseline,
                    })

    anomalies.sort(key=lambda x: x["delta"], reverse=True)

    return anomalies


def at_risk_tasks(board_root: Path, threshold: float = 0.8) -> list:
    """Return list of task IDs in doing/ whose spent budget >= threshold * budget.

    Reads each task in board_root/doing/*/task.json for cost_budget_usd,
    then aggregates actual spend from subtasks/*/result.json.
    Handles missing budgets gracefully (skips those tasks).
    """
    at_risk = []
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
