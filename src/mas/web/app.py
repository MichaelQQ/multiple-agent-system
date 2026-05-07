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

import yaml
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import TypeAdapter

from .. import board, cron, current_subtask, daemon, transitions, worktree
from ..audit import read_events
from ..config import load_config, project_dir, project_root, validate_environment
from ..cost_helpers import aggregate_costs_by_role, at_risk_tasks
from ..events import read_board_events
from ..patterns import read_patterns
from ..roles import goal_similarity
from ..schemas import RoleConfig
from ..stats import compute_stats, parse_since
from ..trace import build_trace

_roles_adapter: TypeAdapter = TypeAdapter(dict[str, RoleConfig])


def _validate_roles_yaml(data: object) -> None:
    _roles_adapter.validate_python(data)

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _fmt_local(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts[:19]
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


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
                created_at = t.created_at.astimezone().strftime("%Y-%m-%d %H:%M")
            except Exception:
                goal = "(unreadable)"
                role = "?"
                created_at = ""
            latest_txn = transitions.read_transitions(tdir, limit=1)
            last_move_ts = latest_txn[-1].timestamp if latest_txn else ""
            last_move = _fmt_local(last_move_ts) if last_move_ts else ""
            sort_key = _parse_ts(last_move_ts) or datetime.min
            progress = ""
            if col == "doing":
                progress = _progress_counts(tdir)
            rows[col].append(
                {
                    "id": tdir.name,
                    "role": role,
                    "goal": goal,
                    "created_at": created_at,
                    "last_move": last_move,
                    "sort_key": sort_key,
                    "progress": progress,
                    "stuck": t.stuck,
                }
            )
        rows[col].sort(key=lambda r: r["sort_key"], reverse=True)
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


def _task_detail(mas: Path, task_id: str, failure_filter: str | None = None) -> dict[str, Any]:
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
                    "model": (r.model if r and hasattr(r, "model") else None),
                    "tokens_in": r.tokens_in if r else None,
                    "tokens_out": r.tokens_out if r else None,
                    "duration_s": r.duration_s if r else None,
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
    logs: list[dict[str, str]] = []
    if log_dir.exists():
        for p in sorted(log_dir.glob("*.log")):
            stem = p.name
            if "-" in stem:
                role = stem.split("-", 1)[0]
            else:
                role = stem.split(".", 1)[0]
            logs.append({"name": p.name, "role": role})

    budget = getattr(task, "cost_budget_usd", None)

    marker = _read_current_subtask_marker(tdir)
    current_subtask_info = None
    if marker is not None:
        elapsed = _get_elapsed_s(marker["start_time_iso"])
        current_subtask_info = {**marker, "elapsed_s": elapsed}

    cost_by_role = aggregate_costs_by_role(tdir)

    # Failure patterns
    failure_patterns: list[dict] = []
    try:
        cfg = load_config(mas)
        threshold = cfg.proposal_similarity_threshold
    except Exception:
        threshold = 0.7
    raw_patterns = read_patterns(mas)
    for pat in raw_patterns:
        sim = goal_similarity(task.goal, pat.get("goal_sample", ""))
        if sim >= threshold:
            failure_patterns.append(pat)
    if failure_filter == "blocking":
        failure_patterns = [
            p for p in failure_patterns
            if p.get("count", 0) >= 2 or p.get("terminal_reason") in {
                "revision_cycles_exhausted", "max_retries_exceeded", "convergence_detected"
            }
        ]

    cost_estimate = None
    try:
        from ..cost_helpers import estimate_task_cost
        cost_estimate = estimate_task_cost(mas, col, task.id)
    except Exception:
        cost_estimate = None

    return {
        "column": col,
        "task": task,
        "result": result,
        "subtasks": subtasks,
        "cost_totals": cost_totals,
        "cost_by_role": cost_by_role,
        "cost_estimate": cost_estimate,
        "budget": budget,
        "transitions": txns,
        "audit": audit,
        "logs": logs,
        "task_dir": tdir,
        "current_subtask": current_subtask_info,
        "failure_patterns": failure_patterns,
        "failure_filter": failure_filter,
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


def _spawn_detached(project: Path, argv: list[str], log_name: str) -> int:
    """Launch a `mas` subcommand as a detached subprocess."""
    log_path = project_dir(project) / "logs" / log_name
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_f = open(log_path, "ab", buffering=0)
    proc = subprocess.Popen(
        [sys.executable, "-m", "mas.cli", *argv],
        cwd=str(project),
        stdin=subprocess.DEVNULL,
        stdout=log_f,
        stderr=log_f,
        start_new_session=True,
        close_fds=True,
    )
    return proc.pid


def _spawn_tick(project: Path) -> int:
    return _spawn_detached(project, ["tick"], "web-tick.log")


def _render_markdown(text: str | None) -> str:
    if not text:
        return ""
    try:
        import markdown as _md
        from markupsafe import Markup
    except ImportError:
        from html import escape
        return f"<pre>{escape(text)}</pre>"
    html = _md.markdown(
        text,
        extensions=["fenced_code", "tables", "nl2br", "sane_lists"],
        output_format="html5",
    )
    return Markup(html)


def create_app(project: Path | None = None) -> FastAPI:
    """Build a FastAPI app bound to the given project (or the cwd's mas project)."""
    proj = (project or project_root()).resolve()
    mas = project_dir(proj)

    app = FastAPI(title="mas web", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
    templates.env.filters["md"] = _render_markdown

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, tick_pid: int | None = None, pruned: int | None = None, upgrade_pid: int | None = None, deleted: str | None = None, deleted_count: int | None = None):
        pid, running = daemon.status(proj)
        flash = None
        if tick_pid:
            flash = f"tick dispatched (pid {tick_pid})"
        elif pruned is not None:
            flash = f"pruned {pruned} worktree(s)"
        elif upgrade_pid:
            flash = f"upgrade dispatched (pid {upgrade_pid})"
        elif deleted:
            flash = f"deleted task {deleted}"
        elif deleted_count is not None:
            flash = f"deleted {deleted_count} task(s)"
        from ..cost_helpers import at_risk_tasks as _at_risk_tasks
        at_risk = _at_risk_tasks(mas)
        return templates.TemplateResponse(
            request,
            "board.html",
            {
                "rows": _board_rows(mas),
                "columns": list(board.COLUMNS),
                "daemon_pid": pid,
                "daemon_running": running,
                "project": str(proj),
                "flash": flash,
                "at_risk_tasks": at_risk,
            },
        )

    @app.get("/task/{task_id}", response_class=HTMLResponse)
    def task_view(task_id: str, request: Request, failure_filter: str | None = None):
        detail = _task_detail(mas, task_id, failure_filter=failure_filter)
        return templates.TemplateResponse(
            request,
            "task.html",
            {
                "task_id": task_id,
                "project": str(proj),
                **detail,
            },
        )

    @app.get("/task/{task_id}/log/{log_name}", response_class=PlainTextResponse)
    def task_log(task_id: str, log_name: str, lines: int = 400):
        located = board.find_task(mas, task_id)
        if located is None:
            raise HTTPException(404, f"task not found: {task_id}")
        _, tdir = located
        if "/" in log_name or ".." in log_name:
            raise HTTPException(400, "invalid log name")
        log_path = tdir / "logs" / log_name
        if not log_path.exists():
            raise HTTPException(404, f"log not found: {log_name}")
        return _read_log_tail(log_path, lines=lines)

    @app.get("/task/{task_id}/logs")
    def task_logs_list(task_id: str, role: str | None = None):
        located = board.find_task(mas, task_id)
        if located is None:
            raise HTTPException(404, f"task not found: {task_id}")
        _, tdir = located
        log_dir = tdir / "logs"
        logs = []
        if log_dir.exists():
            for p in sorted(log_dir.glob("*.log")):
                # Extract role: first segment before '-' or '.'
                stem = p.name
                if "-" in stem:
                    extracted_role = stem.split("-", 1)[0]
                else:
                    extracted_role = stem.split(".", 1)[0]
                logs.append({"name": p.name, "role": extracted_role, "size": p.stat().st_size})
        if role is not None:
            logs = [entry for entry in logs if entry["role"] == role]
        return {"logs": logs}

    @app.get("/events", response_class=HTMLResponse)
    def events_view(
        request: Request,
        task: str | None = None,
        role: str | None = None,
        status: str | None = None,
        event: str | None = None,
        limit: int = 200,
    ):
        try:
            evts = read_board_events(mas, task=task, role=role, status=status, event=event)
        except Exception:
            evts = []
        evts = list(reversed(evts))[:limit]
        rendered = [
            {
                "timestamp": _fmt_local(e.get("timestamp") or ""),
                "task_id": e.get("task_id") or "",
                "event": e.get("event") or "",
                "role": e.get("role") or "",
                "provider": e.get("provider") or "",
                "status": e.get("status") or "",
                "summary": e.get("summary") or "",
            }
            for e in evts
        ]
        return templates.TemplateResponse(
            request,
            "events.html",
            {
                "events": rendered,
                "filters": {"task": task or "", "role": role or "", "status": status or "", "event": event or ""},
                "limit": limit,
                "project": str(proj),
            },
        )

    @app.get("/validate", response_class=HTMLResponse)
    def validate_view(request: Request):
        try:
            issues = validate_environment(mas)
        except Exception as e:
            issues = []
            error = str(e)
        else:
            error = None
        try:
            cfg = load_config(mas)
            cfg_summary = {
                "providers": sorted(cfg.providers.keys()) if hasattr(cfg, "providers") and cfg.providers else [],
                "roles": sorted(cfg.roles.keys()) if hasattr(cfg, "roles") and cfg.roles else [],
            }
        except Exception:
            cfg_summary = None
        return templates.TemplateResponse(
            request,
            "validate.html",
            {
                "issues": [{"field": i.field, "message": i.message} for i in issues],
                "error": error,
                "cfg_summary": cfg_summary,
                "project": str(proj),
            },
        )

    @app.get("/cron", response_class=HTMLResponse)
    def cron_view(request: Request, msg: str | None = None):
        try:
            status_text = cron.status(proj)
        except Exception as e:
            status_text = f"error: {e}"
        return templates.TemplateResponse(
            request,
            "cron.html",
            {
                "status_text": status_text,
                "msg": msg,
                "project": str(proj),
            },
        )

    @app.post("/cron/install")
    def cron_install(interval: int = 5):
        try:
            cron.install(proj, interval_minutes=interval)
        except Exception as e:
            raise HTTPException(500, f"cron install failed: {e}")
        return RedirectResponse(f"/cron?msg=installed+({interval}m)", status_code=303)

    @app.post("/cron/uninstall")
    def cron_uninstall():
        try:
            cron.uninstall(proj)
        except Exception as e:
            raise HTTPException(500, f"cron uninstall failed: {e}")
        return RedirectResponse("/cron?msg=uninstalled", status_code=303)

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

    @app.post("/task/{task_id}/delete")
    def delete_task(task_id: str):
        try:
            board.delete_task(mas, task_id, project_root=proj)
        except FileNotFoundError:
            raise HTTPException(404, f"task not found: {task_id}")
        return RedirectResponse("/?deleted=" + task_id, status_code=303)

    @app.post("/tasks/delete")
    def delete_tasks(task_ids: list[str] = Form(default=[])):
        if not task_ids:
            return RedirectResponse("/", status_code=303)
        deleted = 0
        for tid in task_ids:
            try:
                board.delete_task(mas, tid, project_root=proj)
                deleted += 1
            except FileNotFoundError:
                continue
        return RedirectResponse(f"/?deleted_count={deleted}", status_code=303)

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

    @app.post("/upgrade")
    def upgrade():
        pid = _spawn_detached(proj, ["upgrade", "--yes"], "web-upgrade.log")
        return RedirectResponse(f"/?upgrade_pid={pid}", status_code=303)

    @app.get("/stats", response_class=HTMLResponse)
    def stats_view(request: Request, since: str | None = None):
        error = None
        since_param: str | None = since
        if since:
            try:
                parse_since(since)
            except ValueError as e:
                error = str(e)
                since_param = None
        stats = compute_stats(mas, since=since_param)
        # Compute per-role cost breakdown from all tasks
        cost_by_role: dict[str, dict] = {}
        for col in ("proposed", "doing", "done", "failed"):
            col_dir = mas / "tasks" / col
            if not col_dir.exists():
                continue
            for task_dir in col_dir.iterdir():
                if not task_dir.is_dir():
                    continue
                rollup = aggregate_costs_by_role(task_dir)
                for role, info in rollup.items():
                    if role not in cost_by_role:
                        cost_by_role[role] = {"count": 0, "cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0}
                    cost_by_role[role]["count"] += info["count"]
                    cost_by_role[role]["cost_usd"] = float(cost_by_role[role]["cost_usd"]) + float(info["cost_usd"])
                    cost_by_role[role]["tokens_in"] = int(cost_by_role[role]["tokens_in"]) + int(info["tokens_in"])
                    cost_by_role[role]["tokens_out"] = int(cost_by_role[role]["tokens_out"]) + int(info["tokens_out"])
        return templates.TemplateResponse(
            request,
            "stats.html",
            {
                "stats": stats,
                "cost_by_role": cost_by_role,
                "since": since or "",
                "error": error,
                "project": str(proj),
            },
        )

    @app.get("/costs")
    def costs_json():
        from ..board import list_column
        from ..cost_helpers import aggregate_costs_by_role

        roles: dict[str, dict] = {}
        total = {"cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0}
        for col in ("proposed", "doing", "done", "failed"):
            col_dir = mas / "tasks" / col
            if not col_dir.exists():
                continue
            for task_dir in col_dir.iterdir():
                if not task_dir.is_dir():
                    continue
                rollup = aggregate_costs_by_role(task_dir)
                for role, info in rollup.items():
                    if role not in roles:
                        roles[role] = {"count": 0, "cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0}
                    roles[role]["count"] += info["count"]
                    roles[role]["cost_usd"] = float(roles[role]["cost_usd"]) + float(info["cost_usd"])
                    roles[role]["tokens_in"] = int(roles[role]["tokens_in"]) + int(info["tokens_in"])
                    roles[role]["tokens_out"] = int(roles[role]["tokens_out"]) + int(info["tokens_out"])
        for role, info in roles.items():
            total["cost_usd"] = float(total["cost_usd"]) + float(info["cost_usd"])
            total["tokens_in"] = int(total["tokens_in"]) + int(info["tokens_in"])
            total["tokens_out"] = int(total["tokens_out"]) + int(info["tokens_out"])
        return {"roles": roles, "total": total}

    @app.get("/costs/at-risk")
    def costs_at_risk_json():
        from ..cost_helpers import at_risk_tasks
        return at_risk_tasks(mas)

    @app.get("/health")
    def health():
        from datetime import datetime, timezone as _tz
        heartbeat_path = mas / "tick_heartbeat"
        now = datetime.now(_tz.utc)
        now_iso = now.isoformat()
        if not heartbeat_path.exists():
            return JSONResponse(
                status_code=503,
                content={"status": "degraded", "timestamp": now_iso, "reason": "tick stalled"},
            )
        try:
            ts_str = heartbeat_path.read_text().strip()
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, OSError):
            return JSONResponse(
                status_code=503,
                content={"status": "degraded", "timestamp": now_iso, "reason": "tick stalled"},
            )
        interval = daemon.read_interval(mas)
        threshold = 2 * interval
        elapsed = (now - ts).total_seconds()
        if elapsed > threshold:
            return JSONResponse(
                status_code=503,
                content={"status": "degraded", "timestamp": now_iso, "reason": "tick stalled"},
            )
        return {"status": "ok", "timestamp": now_iso}

    @app.get("/daemon/status")
    def daemon_status_endpoint():
        pid, running = daemon.status(proj)
        return {"pid": pid, "running": running}

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

    @app.get("/trace/{task_id}", response_class=HTMLResponse)
    def trace_view(task_id: str, request: Request):
        located = board.find_task(mas, task_id)
        if located is None:
            raise HTTPException(404, f"task not found: {task_id}")
        _, tdir = located

        trace_data = build_trace(tdir)

        try:
            task = board.read_task(tdir)
            role = task.role
        except Exception:
            role = ""

        total_tokens_in = 0
        total_tokens_out = 0
        subs_root = tdir / "subtasks"
        if subs_root.exists():
            for sub_dir in subs_root.iterdir():
                if sub_dir.is_dir():
                    r = board.read_result(sub_dir)
                    if r is not None:
                        total_tokens_in += r.tokens_in or 0
                        total_tokens_out += r.tokens_out or 0

        return templates.TemplateResponse(
            request,
            "trace.html",
            {
                "task_id": task_id,
                "role": role,
                "goal": trace_data["goal"],
                "total_duration_s": trace_data["total_duration_s"],
                "total_tokens_in": total_tokens_in,
                "total_tokens_out": total_tokens_out,
                "total_cost_usd": trace_data["total_cost_usd"],
                "stages": trace_data["stages"],
            },
        )

    @app.get("/config/roles", response_class=HTMLResponse)
    def config_roles_get(request: Request):
        roles_path = mas / "roles.yaml"
        content = roles_path.read_text() if roles_path.exists() else ""
        return templates.TemplateResponse(
            request,
            "config_roles.html",
            {"content": content, "banner": None, "error": None},
        )

    @app.post("/config/roles", response_class=HTMLResponse)
    def config_roles_post(request: Request, content: str = Form(...)):
        roles_path = mas / "roles.yaml"
        try:
            data = yaml.safe_load(content)
            _validate_roles_yaml(data)
        except Exception as e:
            return templates.TemplateResponse(
                request,
                "config_roles.html",
                {"content": content, "banner": None, "error": str(e)},
                status_code=400,
            )

        tmp_path = mas / f"roles.yaml.{os.getpid()}.tmp"
        try:
            tmp_path.write_text(content)
            os.replace(str(tmp_path), str(roles_path))
        except Exception as e:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            return templates.TemplateResponse(
                request,
                "config_roles.html",
                {"content": content, "banner": None, "error": str(e)},
                status_code=200,
            )

        mtime = int(roles_path.stat().st_mtime)
        return templates.TemplateResponse(
            request,
            "config_roles.html",
            {"content": content, "banner": f"Saved (mtime: {mtime})", "error": None},
        )

    return app


def _reset_task_state(task_dir: Path) -> None:
    """Mirror cli._reset_task_state; kept local so web doesn't import cli."""
    (task_dir / "result.json").unlink(missing_ok=True)
    (task_dir / ".orchestrator_attempt").unlink(missing_ok=True)
    (task_dir / ".previous_failure").unlink(missing_ok=True)
    (task_dir / "plan.json").unlink(missing_ok=True)
    # Per-attempt logs gate orphan detection (`{role}-{attempt}.log`); leaving
    # them in place after an attempt-counter reset would make tick treat the
    # next attempt as a stale orphan and re-fail without dispatching.
    _clear_attempt_logs(task_dir / "logs")
    (task_dir / ".current_subtask").unlink(missing_ok=True)
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
            _clear_attempt_logs(child_dir / "logs")


def _clear_attempt_logs(log_dir: Path) -> None:
    if not log_dir.exists():
        return
    for f in log_dir.glob("*.log"):
        f.unlink(missing_ok=True)


def _read_current_subtask_marker(tdir: Path) -> dict | None:
    return current_subtask._read_current_subtask_marker(tdir)


def _get_elapsed_s(start_time_iso: str) -> float:
    return current_subtask._get_elapsed_s(start_time_iso)
