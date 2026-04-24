from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("mas.stats")

_COLUMNS = ("proposed", "doing", "done", "failed")


def parse_since(value: str) -> timedelta:
    """Parse a --since string like '1h', '2d', '1w' into a timedelta.

    Raises ValueError for unrecognised suffixes.
    """
    if not value:
        raise ValueError("empty --since value")
    suffix = value[-1]
    try:
        amount = int(value[:-1])
    except ValueError:
        raise ValueError(f"invalid --since value: {value!r}")
    if suffix == "h":
        return timedelta(hours=amount)
    elif suffix == "d":
        return timedelta(days=amount)
    elif suffix == "w":
        return timedelta(weeks=amount)
    else:
        raise ValueError(f"unrecognised --since suffix {suffix!r} in {value!r}; use h, d, or w")


def _latest_transition_ts(task_dir: Path) -> datetime | None:
    """Return the timestamp of the most recent line in .transitions.log, or None."""
    log_path = task_dir / ".transitions.log"
    if not log_path.exists():
        return None
    try:
        text = log_path.read_text()
    except OSError:
        return None
    latest: datetime | None = None
    for line in text.splitlines():
        parts = line.split("|", 1)
        if not parts:
            continue
        ts_str = parts[0].strip()
        if not ts_str:
            continue
        try:
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if latest is None or dt > latest:
                latest = dt
        except ValueError:
            continue
    return latest


def compute_stats(
    mas_dir: Path,
    *,
    since: str | None = None,
) -> dict[str, Any]:
    """Compute board statistics."""
    cutoff: datetime | None = None
    if since is not None:
        delta = parse_since(since)
        # Subtract 1s tolerance so tasks at the exact boundary are always included.
        cutoff = datetime.now(timezone.utc) - delta - timedelta(seconds=1)

    board_counts: dict[str, int] = {col: 0 for col in _COLUMNS}
    role_durations: dict[str, list[float]] = {}
    provider_counts: dict[str, int] = {}
    tokens_in_total = 0
    tokens_out_total = 0
    cost_usd_total = 0.0
    env_errors = 0
    terminal_total = 0
    terminal_success = 0
    terminal_revised = 0

    tasks_root = mas_dir / "tasks"

    for col in _COLUMNS:
        col_dir = tasks_root / col
        if not col_dir.exists():
            continue
        for task_dir in col_dir.iterdir():
            if not task_dir.is_dir():
                continue

            # --since filter: use most recent transition timestamp
            if cutoff is not None:
                ts = _latest_transition_ts(task_dir)
                if ts is None or ts < cutoff:
                    continue

            board_counts[col] += 1

            # Read task.json for role and provider
            task_json_path = task_dir / "task.json"
            role: str | None = None
            provider: str | None = None
            cycle: int = 0
            if task_json_path.exists():
                try:
                    raw = json.loads(task_json_path.read_text())
                    role = raw.get("role")
                    cycle = int(raw.get("cycle", 0))
                    inputs = raw.get("inputs") or {}
                    provider = inputs.get("provider")
                except Exception:
                    pass

            if provider:
                provider_counts[provider] = provider_counts.get(provider, 0) + 1

            # Read result.json
            result_json_path = task_dir / "result.json"
            result_status: str | None = None
            duration_s: float | None = None
            if result_json_path.exists():
                try:
                    raw_result = json.loads(result_json_path.read_text())
                    result_status = raw_result.get("status")
                    duration_s = raw_result.get("duration_s")
                    tin = raw_result.get("tokens_in")
                    tout = raw_result.get("tokens_out")
                    cusd = raw_result.get("cost_usd")
                    if tin is not None:
                        tokens_in_total += int(tin)
                    if tout is not None:
                        tokens_out_total += int(tout)
                    if cusd is not None:
                        cost_usd_total += float(cusd)
                except Exception:
                    log.warning("skipping malformed result.json in %s", task_dir)

            # env errors: status==environment_error OR .env_retries marker OR result.env-*.json
            is_env_error = (
                result_status == "environment_error"
                or (task_dir / ".env_retries").exists()
                or bool(list(task_dir.glob("result.env-*.json")))
            )
            if is_env_error:
                env_errors += 1

            # Terminal tasks: done/ and failed/
            if col in ("done", "failed"):
                terminal_total += 1
                if col == "done":
                    terminal_success += 1
                if cycle > 0:
                    terminal_revised += 1

            # Per-role duration
            if role and duration_s is not None:
                role_durations.setdefault(role, []).append(float(duration_s))

    success_rate = (terminal_success / terminal_total) if terminal_total > 0 else 0.0
    revision_rate = (terminal_revised / terminal_total) if terminal_total > 0 else 0.0

    roles: dict[str, Any] = {}
    for r, durations in role_durations.items():
        durations_sorted = sorted(durations)
        n = len(durations_sorted)
        mean_s = sum(durations_sorted) / n
        p50_s = _percentile(durations_sorted, 50)
        p95_s = _percentile(durations_sorted, 95)
        roles[r] = {"mean_s": mean_s, "p50_s": p50_s, "p95_s": p95_s, "count": n}

    return {
        "board": board_counts,
        "success_rate": success_rate,
        "revision_rate": revision_rate,
        "roles": roles,
        "providers": provider_counts,
        "tokens": {
            "tokens_in": tokens_in_total,
            "tokens_out": tokens_out_total,
            "cost_usd": cost_usd_total,
        },
        "env_errors": env_errors,
    }


def _percentile(sorted_values: list[float], p: int) -> float:
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    idx = (p / 100) * (n - 1)
    lo = int(idx)
    hi = lo + 1
    if hi >= n:
        return sorted_values[-1]
    frac = idx - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac
