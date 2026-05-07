"""Failure-pattern index aggregating recurring `failed/` task signatures.

The proposer reads `.mas/patterns.jsonl` to avoid re-proposing tasks that
previously failed for the same reason. Each line is a `FailurePattern` JSON
record produced from the `tasks/failed/` directory (task goal + transition
log + state.json `rejected_attempts`). Two failures with the same normalized
goal-token set and the same terminal transition reason collapse into one
record, with `count` and `task_ids` accumulating across runs.

Computed as a tick suffix; safe to regenerate from scratch on every tick.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from . import board
from . import state as _state
from . import transitions as _transitions
from .roles import _goal_tokens

log = logging.getLogger("mas.patterns")

_PATTERNS_FILE = "patterns.jsonl"
_REJECTED_SAMPLE_LIMIT = 3
_GOAL_SAMPLE_MAX_CHARS = 200


class FailurePattern(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signature: str
    terminal_reason: str
    goal_sample: str
    count: int
    last_seen: str
    task_ids: list[str] = Field(default_factory=list)
    rejected_attempts_sample: list[str] = Field(default_factory=list)


class SuccessPattern(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signature: str
    goal_sample: str
    count: int
    avg_duration_s: float | None = None
    avg_cost_usd: float | None = None
    task_ids: list[str] = Field(default_factory=list)
    success_context: dict = Field(default_factory=dict)


def patterns_path(mas_dir: Path) -> Path:
    return mas_dir / _PATTERNS_FILE


def success_patterns_path(mas_dir: Path) -> Path:
    return mas_dir / "success_patterns.jsonl"


def compute_success_patterns(mas_dir: Path) -> list[SuccessPattern]:
    """Walk `tasks/done/` and aggregate successful task patterns.

    Groups tasks by normalized goal tokens (signature). Skips proposer tasks.
    Computes avg duration/cost from result.json, collects task_ids and
    success_context from evaluator verdict.
    """
    done_dir = mas_dir / "tasks" / "done"
    if not done_dir.exists():
        return []

    by_sig: dict[str, SuccessPattern] = {}

    for task_dir in done_dir.iterdir():
        if not task_dir.is_dir():
            continue
        try:
            t = board.read_task(task_dir)
        except Exception:
            continue
        if getattr(t, "role", None) == "proposer":
            continue

        # Use directory name as task ID to match test expectations
        task_id = task_dir.name

        goal = (t.goal or "").strip()
        tokens = sorted(_goal_tokens(goal or ""))
        if tokens:
            canonical = " ".join(tokens)
        else:
            canonical = goal[:80].lower()
        sig = canonical

        # Read result.json for duration, cost, and evaluator verdict
        result_path = task_dir / "result.json"
        duration_s = None
        cost_usd = None
        success_context = {}
        if result_path.exists():
            try:
                result_data = json.loads(result_path.read_text(encoding="utf-8"))
                duration_s = result_data.get("duration_s")
                cost_usd = result_data.get("cost_usd")
                # Extract success_context from evaluator verdict if available
                verdict = result_data.get("verdict")
                if verdict:
                    success_context["verdict"] = verdict
                handoff = result_data.get("handoff")
                if handoff and isinstance(handoff, dict):
                    notes = handoff.get("notes")
                    if notes:
                        success_context["notes"] = notes
            except Exception:
                pass

        existing = by_sig.get(sig)
        goal_sample = goal[:_GOAL_SAMPLE_MAX_CHARS]
        if existing is None:
            existing = SuccessPattern(
                signature=sig,
                goal_sample=goal_sample,
                count=1,
                avg_duration_s=duration_s,
                avg_cost_usd=cost_usd,
                task_ids=[task_id],
                success_context=success_context,
            )
            by_sig[sig] = existing
            continue

        existing.count += 1
        if task_id not in existing.task_ids:
            existing.task_ids.append(task_id)
        # Update running averages
        if duration_s is not None:
            if existing.avg_duration_s is None:
                existing.avg_duration_s = duration_s
            else:
                existing.avg_duration_s = (existing.avg_duration_s * (existing.count - 1) + duration_s) / existing.count
        if cost_usd is not None:
            if existing.avg_cost_usd is None:
                existing.avg_cost_usd = cost_usd
            else:
                existing.avg_cost_usd = (existing.avg_cost_usd * (existing.count - 1) + cost_usd) / existing.count

    patterns = list(by_sig.values())
    return patterns


def write_success_patterns(mas_dir: Path, patterns: list[SuccessPattern]) -> None:
    """Atomically rewrite `mas_dir/success_patterns.jsonl`."""
    target = success_patterns_path(mas_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + f".{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for p in patterns:
            f.write(p.model_dump_json() + "\n")
    tmp.replace(target)


def read_success_patterns(mas_dir: Path, *, limit: int | None = None) -> list[dict]:
    """Read `success_patterns.jsonl` for consumption by the proposer.

    Malformed lines are skipped with a WARNING; the file may not exist on a
    fresh project, in which case an empty list is returned.

    Results are sorted by count descending (highest first) when a limit is
    provided, so the caller gets the top-N patterns.
    """
    path = success_patterns_path(mas_dir)
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            SuccessPattern.model_validate(data)
            out.append(data)
        except Exception as e:
            log.warning("skipping malformed success pattern line: %s", e)
            continue
    out.sort(key=lambda d: d.get("count", 0), reverse=True)
    if limit is not None:
        out = out[:limit]
    return out


def success_refresh(mas_dir: Path) -> list[SuccessPattern]:
    """Recompute and persist the success-pattern index. Best-effort: errors
    are logged at WARNING and never raised — pattern-index failures must not
    abort a tick."""
    try:
        patterns = compute_success_patterns(mas_dir)
        write_success_patterns(mas_dir, patterns)
        return patterns
    except Exception as e:
        log.warning("success pattern refresh failed: %s", e)
        return []


# Backward-compatible alias
refresh_success = success_refresh


def _terminal_reason(task_dir: Path) -> str | None:
    txns = _transitions.read_transitions(task_dir)
    for t in reversed(txns):
        if t.to_state == "failed":
            return t.reason or "unknown"
    return None


def _signature(goal: str, terminal_reason: str) -> str:
    tokens = sorted(_goal_tokens(goal or ""))
    if tokens:
        canonical = " ".join(tokens)
    else:
        canonical = (goal or "").strip().lower()[:80]
    return f"{terminal_reason}|{canonical}"


def compute_patterns(mas_dir: Path) -> list[FailurePattern]:
    """Walk `tasks/failed/` and aggregate recurring failure signatures.

    Two failures sharing a normalized goal-token set and a terminal transition
    reason collapse to one record. Sorted newest-first by `last_seen`, then
    by `count` descending.
    """
    failed_dir = mas_dir / "tasks" / "failed"
    if not failed_dir.exists():
        return []

    by_sig: dict[str, FailurePattern] = {}

    for task_dir in failed_dir.iterdir():
        if not task_dir.is_dir():
            continue
        try:
            t = board.read_task(task_dir)
        except Exception:
            continue
        if getattr(t, "role", None) == "proposer":
            continue

        terminal_reason = _terminal_reason(task_dir) or "unknown"
        goal = (t.goal or "").strip()
        sig = _signature(goal, terminal_reason)

        txns = _transitions.read_transitions(task_dir)
        last_ts = txns[-1].timestamp if txns else ""

        attempts: list[str] = []
        try:
            ps = _state.read_state(task_dir)
            for a in ps.rejected_attempts[:_REJECTED_SAMPLE_LIMIT]:
                first_line = (a.summary or "").splitlines()
                snippet = first_line[0][:200] if first_line else ""
                attempts.append(f"[{a.role}/{a.status}] {snippet}".rstrip())
        except Exception:
            pass

        existing = by_sig.get(sig)
        goal_sample = goal[:_GOAL_SAMPLE_MAX_CHARS]
        if existing is None:
            by_sig[sig] = FailurePattern(
                signature=sig,
                terminal_reason=terminal_reason,
                goal_sample=goal_sample,
                count=1,
                last_seen=last_ts,
                task_ids=[t.id],
                rejected_attempts_sample=attempts,
            )
            continue

        existing.count += 1
        if t.id not in existing.task_ids:
            existing.task_ids.append(t.id)
        if last_ts and last_ts > existing.last_seen:
            existing.last_seen = last_ts
            existing.goal_sample = goal_sample or existing.goal_sample
        # Preserve a small, deduped sample to keep prompts compact
        cap = _REJECTED_SAMPLE_LIMIT * 2
        for a in attempts:
            if a in existing.rejected_attempts_sample:
                continue
            if len(existing.rejected_attempts_sample) >= cap:
                break
            existing.rejected_attempts_sample.append(a)

    patterns = list(by_sig.values())
    patterns.sort(key=lambda p: (p.last_seen or "", p.count), reverse=True)
    return patterns


def write_patterns(mas_dir: Path, patterns: list[FailurePattern]) -> None:
    """Atomically rewrite `mas_dir/patterns.jsonl`."""
    target = patterns_path(mas_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + f".{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for p in patterns:
            f.write(p.model_dump_json() + "\n")
    tmp.replace(target)


def read_patterns(mas_dir: Path, *, limit: int | None = None) -> list[dict]:
    """Read `patterns.jsonl` for consumption by the proposer (or CLI tools).

    Malformed lines are skipped with a WARNING; the file may not exist on a
    fresh project, in which case an empty list is returned.
    """
    path = patterns_path(mas_dir)
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            FailurePattern.model_validate(data)
            out.append(data)
        except Exception as e:
            log.warning("skipping malformed pattern line: %s", e)
            continue
    if limit is not None:
        out = out[:limit]
    return out


def refresh(mas_dir: Path) -> list[FailurePattern]:
    """Recompute and persist the failure-pattern index. Best-effort: errors
    are logged at WARNING and never raised — pattern-index failures must not
    abort a tick."""
    try:
        patterns = compute_patterns(mas_dir)
        write_patterns(mas_dir, patterns)
        return patterns
    except Exception as e:
        log.warning("failure pattern refresh failed: %s", e)
        return []
