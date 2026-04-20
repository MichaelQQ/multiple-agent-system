from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from string import Template
from typing import Any

from .schemas import Plan, ProposerSignals, Task

log = logging.getLogger("mas.roles")


_RESULT_SCHEMA_HINT = """
Your last action MUST be writing a valid `result.json` to the path in the
`MAS_TASK_DIR` environment variable (not inside the worktree) matching this schema (pydantic):

{
  "task_id": str,
  "status": "success" | "failure" | "needs_revision",
  "summary": str,
  "artifacts": [str],
  "handoff": {...} | null,
  "verdict": "pass" | "fail" | "needs_revision" | null,
  "feedback": str | null,
  "tokens_in": int | null,
  "tokens_out": int | null,
  "duration_s": float,
  "cost_usd": float | null
}
""".strip()


def render_prompt(template_path: Path, task: Task, **extra: Any) -> str:
    tmpl = Template(template_path.read_text())
    prior_results_json = json.dumps(
        [r.model_dump(mode="json", exclude_none=True) for r in task.prior_results],
        indent=2,
    )
    vars_ = {
        "task_id": task.id,
        "role": task.role,
        "goal": task.goal,
        "cycle": str(task.cycle),
        "attempt": str(task.attempt),
        "parent_id": task.parent_id or "",
        "previous_failure": task.previous_failure or "",
        "inputs_json": json.dumps(task.inputs, indent=2),
        "constraints_json": json.dumps(task.constraints, indent=2),
        "prior_results_json": prior_results_json,
        "result_schema": _RESULT_SCHEMA_HINT,
    }
    vars_.update({k: str(v) for k, v in extra.items()})
    log.debug("rendering prompt", extra={"role": task.role, "task_id": task.id})
    return tmpl.safe_substitute(vars_)


# --- Proposer signal gathering ---------------------------------------------


def gather_proposer_signals(
    project_root: Path,
    *,
    ideas_path: Path | None = None,
    ci_command: list[str] | None = None,
    git_log_limit: int = 20,
    mas_root: Path | None = None,
) -> ProposerSignals:
    signals: dict[str, Any] = {}

    signals["repo_scan"] = _shallow_tree(project_root, max_depth=2, max_entries=200)

    _mas = mas_root or (project_root / ".mas")
    signals["already_proposed"] = _list_goals(_mas, "proposed")
    signals["in_progress"] = _list_goals(_mas, "doing")
    signals["recently_done"] = _list_goals(_mas, "done", limit=30)
    signals["recently_failed"] = _list_goals(_mas, "failed", limit=20)

    log = _run(["git", "-C", str(project_root), "log", f"-{git_log_limit}",
                "--pretty=format:%h %ad %s", "--date=short"], timeout=15)
    signals["git_log"] = log

    diff = _run(["git", "-C", str(project_root), "log", "-5", "-p",
                 "--stat", "--no-color"], timeout=20)
    signals["recent_diffs"] = diff[:20_000]

    if ideas_path and ideas_path.exists():
        signals["ideas"] = ideas_path.read_text()[:20_000]
    else:
        signals["ideas"] = ""

    if ci_command:
        signals["ci_output"] = _run(ci_command, cwd=project_root, timeout=120)[-20_000:]
    else:
        signals["ci_output"] = ""

    return ProposerSignals.model_validate(signals)


def _list_goals(mas_root: Path, column: str, *, limit: int | None = None) -> list[str]:
    """Return task goals from a board column, filtering out proposer's own
    bootstrapping tasks (role=proposer). Sorted newest-first by mtime when
    `limit` is set, else alphabetical."""
    col_dir = mas_root / "tasks" / column
    entries = list(col_dir.glob("*/task.json"))
    if limit is not None:
        entries.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    else:
        entries.sort()

    goals: list[str] = []
    for task_json in entries:
        if limit is not None and len(goals) >= limit:
            break
        try:
            data = json.loads(task_json.read_text())
            known = Task.model_fields.keys()
            data = {k: v for k, v in data.items() if k in known}
            task = Task.model_validate(data)
            if task.role == "proposer":
                continue
            goals.append(task.goal or task_json.parent.name)
        except Exception:
            goals.append(task_json.parent.name)
    return goals


def _list_proposed_tasks(mas_root: Path) -> list[str]:
    """Back-compat shim; prefer _list_goals."""
    return _list_goals(mas_root, "proposed")


# --- Goal similarity --------------------------------------------------------

_GOAL_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is",
    "it", "of", "on", "or", "that", "the", "to", "with", "add", "implement",
    "create", "build", "make", "new", "task", "feature",
})


def _goal_tokens(goal: str) -> set[str]:
    import re as _re
    words = _re.findall(r"[A-Za-z0-9_]+", goal.lower())
    return {w for w in words if len(w) > 2 and w not in _GOAL_STOPWORDS}


def goal_similarity(a: str, b: str) -> float:
    """Jaccard similarity of normalized goal tokens. 0.0 if either is empty."""
    ta, tb = _goal_tokens(a), _goal_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def find_similar_goal(
    new_goal: str, existing: list[str], *, threshold: float = 0.7
) -> tuple[str, float] | None:
    """Return (goal, score) of the closest existing goal at or above threshold,
    else None."""
    best: tuple[str, float] | None = None
    for g in existing:
        s = goal_similarity(new_goal, g)
        if s >= threshold and (best is None or s > best[1]):
            best = (g, s)
    return best


def _shallow_tree(root: Path, *, max_depth: int, max_entries: int) -> str:
    lines: list[str] = []
    count = 0
    skip = {".git", "__pycache__", "node_modules", ".venv", ".mas"}

    def walk(d: Path, depth: int) -> None:
        nonlocal count
        if depth > max_depth or count >= max_entries:
            return
        try:
            entries = sorted(d.iterdir())
        except OSError:
            return
        for e in entries:
            if e.name in skip or e.name.startswith("."):
                continue
            rel = e.relative_to(root)
            lines.append(f"{'  ' * depth}{rel}")
            count += 1
            if count >= max_entries:
                return
            if e.is_dir():
                walk(e, depth + 1)

    walk(root, 0)
    return "\n".join(lines)


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 30) -> str:
    try:
        r = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        return (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"[error running {' '.join(cmd)}: {e}]"


# --- Plan parsing -----------------------------------------------------------


def parse_plan(plan_path: Path, parent_id: str) -> Plan:
    data = json.loads(plan_path.read_text())
    data.setdefault("parent_id", parent_id)
    return Plan.model_validate(data)
