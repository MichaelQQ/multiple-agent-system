"""FastAPI app exposing mas board state and CLI-equivalent actions over HTTP.

The app is stateless: each request reads .mas/ directly, mirroring how the CLI
works. Actions shell out to `mas tick` (detached) or call the in-process helpers
used by the CLI (board.move, daemon.start/stop). Bind to 127.0.0.1; no auth.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import board, daemon, transitions, worktree
from ..audit import read_events
from ..config import project_dir, project_root

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _fmt_local(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts[:19]
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _subtask_status(child_dir: Path) -> str:
    r = board.read_result(child_dir)
    if r is None:
        return "pending"
    if r.status == "success":
        if r.verdict == "pass":
            return "pass"
        if r.verdict in ("fail", "needs_revision"):
            return r.verdict
        return "success"
    return r.status


def _board_rows(mas: Path) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = {c: [] for c in board.COLUMNS}
    for col in board.COLUMNS:
        for tdir in board.list_column(mas, col):
            try:
                t = board.read_task(tdir)
                goal = t.goal
                role = t.role
            except Exception:
                goal = "(unreadable)"
                role = "?"
            latest_txn = transitions.read_transitions(tdir, limit=1)
            last_move = _fmt_local(latest_txn[-1].timestamp) if latest_txn else ""
            progress = ""
            if col == "doing":
                progress = _progress_counts(tdir)
            rows[col].append(
                {
                    "id": tdir.name,
                    "role": role,
                    "goal": goal,
                    "last_move": last_move,
                    "progress": progress,
                }
            )
    return rows


def _progress_counts(parent_dir: Path) -> str:
    plan_path = parent_dir / "plan.json"
    if not plan_path.exists():
        return "orchestrator"
    try:
        plan = board.read_plan(parent_dir)
    except Exception:
        return "?"
    subs = parent_dir / "subtasks"
    counts: dict[str, int] = {}
    for spec in plan.subtasks:
        s = _subtask_status(subs / spec.id)
        counts[s] = counts.get(s, 0) + 1
    return ", ".join(f"{v} {k}" for k, v in counts.items()) or "empty"


def _task_detail(mas: Path, task_id: str) -> dict[str, Any]:
    located = board.find_task(mas, task_id)
    if located is None:
        raise HTTPException(404, f"task not found: {task_id}")
    col, tdir = located
    try:
        task = board.read_task(tdir)
    except Exception as e:
        raise HTTPException(500, f"cannot read task: {e}")

    result = board.read_result(tdir)
    plan = None
    subtasks: list[dict[str, Any]] = []
    plan_path = tdir / "plan.json"
    if plan_path.exists():
        try:
            plan = board.read_plan(tdir)
        except Exception:
            plan = None
    if plan is not None:
        subs_root = tdir / "subtasks"
        total_in = total_out = 0
        total_cost = 0.0
        for spec in plan.subtasks:
            child_dir = subs_root / spec.id
            r = board.read_result(child_dir) if child_dir.exists() else None
            if r is not None:
                total_in += r.tokens_in or 0
                total_out += r.tokens_out or 0
                total_cost += r.cost_usd or 0.0
            subtasks.append(
                {
                    "id": spec.id,
                    "role": spec.role,
                    "goal": spec.goal,
                    "status": _subtask_status(child_dir),
                    "summary": (r.summary if r else ""),
                    "tokens_in": r.tokens_in if r else None,
                    "tokens_out": r.tokens_out if r else None,
                    "cost_usd": r.cost_usd if r else None,
                }
            )
        cost_totals = {"tokens_in": total_in, "tokens_out": total_out, "cost_usd": total_cost}
    else:
        cost_totals = None

    txns = [
        {
            "timestamp": _fmt_local(x.timestamp),
            "from_state": x.from_state,
            "to_state": x.to_state,
            "reason": x.reason,
        }
        for x in transitions.read_transitions(tdir, limit=100)
    ]

    try:
        events = read_events(tdir)[-50:]
    except Exception:
        events = []
    audit = [
        {
            "timestamp": _fmt_local(e.get("timestamp") or ""),
            "event": e.get("event") or "",
            "role": e.get("role") or "",
            "provider": e.get("provider") or "",
            "subtask_id": e.get("subtask_id") or "",
            "status": e.get("status") or "",
            "summary": e.get("summary") or "",
        }
        for e in events
    ]

    log_dir = tdir / "logs"
    logs: list[str] = []
    if log_dir.exists():
        logs = sorted(p.name for p in log_dir.glob("*.log"))

    return {
        "column": col,
        "task": task,
        "result": result,
        "subtasks": subtasks,
        "cost_totals": cost_totals,
        "transitions": txns,
        "audit": audit,
        "logs": logs,
        "task_dir": tdir,
    }


def _read_log_tail(log_path: Path, lines: int = 400) -> str:
    if not log_path.exists():
        return ""
    try:
        data = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    tail = data.splitlines()[-lines:]
    return "\n".join(tail)


def _spawn_tick(project: Path) -> int:
    """Launch `mas tick` as a detached subprocess, mirroring daemon's pattern."""
    log_path = project_dir(project) / "logs" / "web-tick.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_f = open(log_path, "ab", buffering=0)
    proc = subprocess.Popen(
        [sys.executable, "-m", "mas.cli", "tick"],
        cwd=str(project),
        stdin=subprocess.DEVNULL,
        stdout=log_f,
        stderr=log_f,
        start_new_session=True,
        close_fds=True,
    )
    return proc.pid


def create_app(project: Path | None = None) -> FastAPI:
    """Build a FastAPI app bound to the given project (or the cwd's mas project)."""
    proj = (project or project_root()).resolve()
    mas = project_dir(proj)

    app = FastAPI(title="mas web", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        pid, running = daemon.status(proj)
        return templates.TemplateResponse(
            request,
            "board.html",
            {
                "rows": _board_rows(mas),
                "columns": list(board.COLUMNS),
                "daemon_pid": pid,
                "daemon_running": running,
                "project": str(proj),
            },
        )

    @app.get("/task/{task_id}", response_class=HTMLResponse)
    def task_view(task_id: str, request: Request):
        detail = _task_detail(mas, task_id)
        return templates.TemplateResponse(
            request,
            "task.html",
            {
                "task_id": task_id,
                **detail,
            },
        )

    @app.get("/task/{task_id}/log/{log_name}", response_class=PlainTextResponse)
    def task_log(task_id: str, log_name: str, lines: int = 400):
        located = board.find_task(mas, task_id)
        if located is None:
            raise HTTPException(404, f"task not found: {task_id}")
        _, tdir = located
        # Prevent traversal; log_name must be a bare filename in logs/.
        if "/" in log_name or ".." in log_name:
            raise HTTPException(400, "invalid log name")
        log_path = tdir / "logs" / log_name
        if not log_path.exists():
            raise HTTPException(404, f"log not found: {log_name}")
        return _read_log_tail(log_path, lines=lines)

    @app.post("/task/{task_id}/promote")
    def promote(task_id: str):
        src = mas / "tasks" / "proposed" / task_id
        if not src.exists():
            raise HTTPException(404, f"not in proposed/: {task_id}")
        dst = mas / "tasks" / "doing" / task_id
        board.move(src, dst, reason="web_promote")
        return RedirectResponse(f"/task/{task_id}", status_code=303)

    @app.post("/task/{task_id}/retry")
    def retry(task_id: str):
        src = mas / "tasks" / "failed" / task_id
        if not src.exists():
            raise HTTPException(404, f"not in failed/: {task_id}")
        dst = mas / "tasks" / "doing" / task_id
        board.move(src, dst, reason="web_retry")
        _reset_task_state(dst)
        return RedirectResponse(f"/task/{task_id}", status_code=303)

    @app.post("/prune")
    def prune():
        pruned = 0
        for col in ("done", "failed"):
            for tdir in board.list_column(mas, col):
                wt = tdir / "worktree"
                if not wt.exists():
                    continue
                try:
                    worktree.prune(proj, wt, keep_branch=True)
                    pruned += 1
                except Exception:
                    pass
        return RedirectResponse(f"/?pruned={pruned}", status_code=303)

    @app.post("/tick")
    def tick():
        pid = _spawn_tick(proj)
        return RedirectResponse(f"/?tick_pid={pid}", status_code=303)

    @app.post("/daemon/start")
    def daemon_start(interval: int = 300):
        existing_pid, running = daemon.status(proj)
        if running:
            raise HTTPException(409, f"daemon already running (pid {existing_pid})")
        daemon.start(proj, interval_seconds=interval)
        return RedirectResponse("/", status_code=303)

    @app.post("/daemon/stop")
    def daemon_stop():
        daemon.stop(proj)
        return RedirectResponse("/", status_code=303)

    return app


def _reset_task_state(task_dir: Path) -> None:
    """Mirror cli._reset_task_state; kept local so web doesn't import cli."""
    (task_dir / "result.json").unlink(missing_ok=True)
    (task_dir / ".orchestrator_attempt").unlink(missing_ok=True)
    (task_dir / ".previous_failure").unlink(missing_ok=True)
    (task_dir / "plan.json").unlink(missing_ok=True)
    subtasks_root = task_dir / "subtasks"
    if subtasks_root.exists():
        for child_dir in subtasks_root.iterdir():
            if not child_dir.is_dir():
                continue
            (child_dir / "result.json").unlink(missing_ok=True)
            (child_dir / ".previous_failure").unlink(missing_ok=True)
            for f in child_dir.glob("result.failed-*.json"):
                f.unlink()
            attempt_file = child_dir / ".attempt"
            if attempt_file.exists():
                attempt_file.write_text("1")
