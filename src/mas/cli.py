from __future__ import annotations

import difflib
import json
import logging
import re
import shutil
import subprocess
import sys
from datetime import datetime
from importlib import resources
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import board, cron, daemon, transitions, worktree
from .config import PROJECT_DIR_NAME, project_dir, project_root
from .logging import setup_logging

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()


def _templates_dir() -> Path:
    """Locate the packaged templates directory.

    When installed via hatchling, templates live under mas/_templates/.
    When running from source, they live at <repo>/templates/.
    """
    try:
        # importlib.resources for installed package
        tdir = resources.files("mas").joinpath("_templates")
        if tdir.is_dir():
            return Path(str(tdir))
    except (ModuleNotFoundError, AttributeError):
        pass
    # Fall back to source layout.
    return Path(__file__).resolve().parents[2] / "templates"


def _fmt_local_time(ts: str) -> str:
    """Render a UTC ISO timestamp as local HH:MM:SS for the show table."""
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return ts[11:19]
    return dt.astimezone().strftime("%H:%M:%S")


@app.command()
def init(
    path: Path = typer.Argument(Path.cwd(), help="Project root to initialize"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing .mas/"),
) -> None:
    """Create .mas/ with default config + prompts."""
    path = path.resolve()
    mas = path / PROJECT_DIR_NAME
    if mas.exists() and not force:
        typer.echo(f"{mas} already exists (use --force to reinit)")
        raise typer.Exit(1)
    if mas.exists():
        shutil.rmtree(mas)
    board.ensure_layout(mas)
    (mas / "ideas.md").write_text("# Ideas\n\n- (write one idea per bullet)\n")

    tpl = _templates_dir()
    for name in ("config.yaml", "roles.yaml"):
        src = tpl / name
        if src.exists():
            shutil.copy(src, mas / name)
    prompts_src = tpl / "prompts"
    if prompts_src.exists():
        for p in prompts_src.iterdir():
            shutil.copy(p, mas / "prompts" / p.name)
    typer.echo(f"initialized {mas}")


@app.command()
def upgrade(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would change without writing"),
    assume_yes: bool = typer.Option(
        False, "-y", "--yes", help="Skip confirmation prompts (apply all changes, auto-restart daemon)"
    ),
) -> None:
    """Update .mas/ template files from the installed package, preserving tasks and logs."""
    mas = project_dir()
    tpl = _templates_dir()

    board.ensure_layout(mas)

    targets: list[tuple[Path, Path]] = []
    for name in ("config.yaml", "roles.yaml"):
        src = tpl / name
        if src.exists():
            targets.append((src, mas / name))
    prompts_src = tpl / "prompts"
    if prompts_src.exists():
        for p in prompts_src.iterdir():
            targets.append((p, mas / "prompts" / p.name))

    new_files: list[tuple[Path, Path]] = []
    changed_files: list[tuple[Path, Path]] = []
    unchanged = 0
    for src, dst in targets:
        if not dst.exists():
            new_files.append((src, dst))
        elif src.read_bytes() != dst.read_bytes():
            changed_files.append((src, dst))
        else:
            unchanged += 1

    if not new_files and not changed_files:
        typer.echo(f"already up to date ({unchanged} files unchanged)")
        return

    for src, dst in new_files:
        typer.echo(f"  new: {dst.relative_to(mas.parent)}")
    for src, dst in changed_files:
        rel = dst.relative_to(mas.parent)
        typer.echo(f"  update: {rel}")
        diff = difflib.unified_diff(
            dst.read_text().splitlines(keepends=True),
            src.read_text().splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
        for line in diff:
            typer.echo(line.rstrip("\n"))
    if unchanged:
        typer.echo(f"  ({unchanged} files already up to date)")

    if dry_run:
        typer.echo("(dry-run, nothing written)")
        return

    if not assume_yes and not typer.confirm("Apply these changes?", default=False):
        typer.echo("upgrade aborted")
        raise typer.Exit(1)

    for src, dst in new_files + changed_files:
        shutil.copy(src, dst)
        typer.echo(f"  wrote {dst.relative_to(mas.parent)}")
    typer.echo("upgrade complete")

    _maybe_restart_daemon(assume_yes=assume_yes)


def _maybe_restart_daemon(*, assume_yes: bool) -> None:
    """If a daemon is running, offer to restart it so it picks up new templates."""
    from . import daemon as daemon_mod

    proj = project_root()
    pid, running = daemon_mod.status(proj)
    if pid is None or not running:
        return

    typer.echo(f"daemon is running (pid {pid}); it must restart to pick up changes")
    if not assume_yes and not typer.confirm("Restart daemon now?", default=True):
        typer.echo("skipping daemon restart (run `mas daemon stop && mas daemon start` to apply)")
        return

    mas = project_dir(proj)
    interval = daemon_mod.read_interval(mas)
    daemon_mod.stop(proj)
    new_pid = daemon_mod.start(proj, interval_seconds=interval)
    typer.echo(f"daemon restarted (pid {new_pid}, interval {interval}s)")


@app.command()
def tick(
    verbose: bool = typer.Option(False, "--verbose", help="Enable verbose (DEBUG) logging"),
) -> None:
    """Run a single tick of the orchestrator."""
    setup_logging(logging.DEBUG if verbose else logging.INFO)
    from .tick import run_tick

    run_tick()


@app.command()
def validate(
    ctx: typer.Context,
) -> None:
    """Validate mas configuration and environment."""
    from .config import load_config, project_dir, validate_config, validate_environment

    if ctx.obj and "mas" in ctx.obj:
        mas = project_dir(Path(ctx.obj["mas"]))
    else:
        mas = project_dir()

    issues = validate_environment(mas)

    if not issues:
        typer.echo("Validation passed: all providers and prompt templates are available.")
        raise typer.Exit(0)

    for issue in issues:
        typer.echo(f"ERROR: {issue.field}: {issue.message}")

    raise typer.Exit(1)


@app.command()
def show(
    task_id: str = typer.Argument(None, help="If given, render the subtask tree for this task"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead of Rich output"),
) -> None:
    """Print the current board, or a task's subtask tree when an id is given.

    With --json, emits a pretty-printed JSON document on stdout instead of the
    Rich table/tree — suitable for dashboards, CI scripts, or further processing.
    """
    mas = project_dir()
    if json_output:
        if task_id:
            located = board.find_task(mas, task_id)
            if located is None:
                typer.echo(json.dumps({"error": f"not found: {task_id}"}))
                raise typer.Exit(1)
            col, tdir = located
            try:
                t = board.read_task(tdir)
            except Exception:
                typer.echo(json.dumps({"error": f"not found: {task_id}"}))
                raise typer.Exit(1)
            result = board.read_result(tdir)
            result_data = None
            if result is not None:
                result_data = {
                    "status": result.status,
                    "verdict": result.verdict,
                    "summary": result.summary,
                }
            plan_data = None
            plan_path = tdir / "plan.json"
            if plan_path.exists():
                try:
                    plan = board.read_plan(tdir)
                    subtasks = []
                    for spec in plan.subtasks:
                        child_dir = tdir / "subtasks" / spec.id
                        status = _subtask_status(child_dir)
                        r = board.read_result(child_dir)
                        subtasks.append({
                            "id": spec.id,
                            "role": spec.role,
                            "goal": spec.goal,
                            "status": status,
                            "summary": r.summary if r is not None else "",
                        })
                    plan_data = {"subtasks": subtasks}
                except Exception:
                    plan_data = None
            data = {
                "task_id": task_id,
                "column": col,
                "role": t.role,
                "goal": t.goal,
                "result": result_data,
                "plan": plan_data,
            }
            typer.echo(json.dumps(data, indent=2))
            return
        rows = []
        for col in board.COLUMNS:
            for d in board.list_column(mas, col):
                try:
                    t = board.read_task(d)
                    role = t.role
                    goal = t.goal
                except Exception:
                    role = "?"
                    goal = "?"
                progress = _subtask_progress(d) if col == "doing" else ""
                txns = transitions.read_transitions(d)
                rows.append({
                    "column": col,
                    "id": d.name,
                    "role": role,
                    "goal": goal,
                    "progress": progress,
                    "transitions": [
                        {
                            "timestamp": x.timestamp,
                            "from_state": x.from_state,
                            "to_state": x.to_state,
                            "reason": x.reason,
                        }
                        for x in txns
                    ],
                })
        typer.echo(json.dumps(rows, indent=2))
        return
    if task_id:
        _show_task_tree(mas, task_id)
        return

    table = Table("column", "id", "role", "goal", "progress", "recent transitions")
    for col in board.COLUMNS:
        for d in board.list_column(mas, col):
            try:
                t = board.read_task(d)
                goal = t.goal[:60]
            except Exception:
                t = None
                goal = "?"
            progress = _subtask_progress(d) if col == "doing" else ""
            txns = transitions.read_transitions(d, limit=5)
            txn_str = "\n".join(
                f"{_fmt_local_time(x.timestamp)} {x.from_state}→{x.to_state} ({x.reason})"
                for x in txns
            ) if txns else ""
            table.add_row(col, d.name, t.role if t else "?", goal, progress, txn_str)
    console.print(table)


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


def _subtask_progress(parent_dir: Path) -> str:
    """Compact status counts for the subtasks under a parent task, e.g. '2 pass, 1 pending'."""
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
    order = ["pass", "success", "pending", "needs_revision", "environment_error", "failure", "fail"]
    parts = [f"{counts[k]} {k}" for k in order if counts.get(k)]
    for k, v in counts.items():
        if k not in order:
            parts.append(f"{v} {k}")
    return ", ".join(parts) or "empty"


def _show_task_tree(mas: Path, task_id: str) -> None:
    from rich.tree import Tree

    located = board.find_task(mas, task_id)
    if located is None:
        typer.echo(f"not found: {task_id}")
        raise typer.Exit(1)
    col, tdir = located
    try:
        t = board.read_task(tdir)
    except Exception:
        typer.echo(f"cannot read task: {task_id}")
        raise typer.Exit(1)

    root = Tree(f"[bold]{task_id}[/bold]  ({col}, {t.role})  {t.goal[:80]}")
    result = board.read_result(tdir)
    if result is not None:
        root.add(f"result: [cyan]{result.status}[/cyan]  {result.summary[:100]}")

    plan_path = tdir / "plan.json"
    if plan_path.exists():
        try:
            plan = board.read_plan(tdir)
        except Exception:
            plan = None
        if plan is not None:
            for spec in plan.subtasks:
                child_dir = tdir / "subtasks" / spec.id
                status = _subtask_status(child_dir)
                color = {
                    "pass": "green", "success": "green",
                    "pending": "yellow",
                    "needs_revision": "magenta",
                    "fail": "red", "failure": "red",
                    "environment_error": "bright_black",
                }.get(status, "white")
                label = f"[{color}]{status:>16}[/{color}]  {spec.id} ({spec.role})"
                node = root.add(label)
                r = board.read_result(child_dir)
                if r is not None and r.summary:
                    node.add(f"[dim]{r.summary[:120]}[/dim]")
    else:
        root.add("[dim]no plan yet[/dim]")

    console.print(root)


@app.command()
def promote(task_id: str) -> None:
    """Move a proposal from proposed/ to doing/."""
    mas = project_dir()
    src = mas / "tasks" / "proposed" / task_id
    if not src.exists():
        typer.echo(f"not found: {src}")
        raise typer.Exit(1)
    dst = mas / "tasks" / "doing" / task_id
    board.move(src, dst, reason="manual_promote")
    typer.echo(f"promoted {task_id}")


@app.command()
def retry(task_id: str) -> None:
    """Push a failed task back into doing/ with state reset."""
    mas = project_dir()
    src = mas / "tasks" / "failed" / task_id
    if not src.exists():
        typer.echo(f"not found: {src}")
        raise typer.Exit(1)
    dst = mas / "tasks" / "doing" / task_id
    board.move(src, dst, reason="manual_retry")
    _reset_task_state(dst)
    typer.echo(f"retrying {task_id}")


@app.command()
def delete(
    task_ids: list[str] = typer.Argument(..., help="One or more task IDs to delete"),
    assume_yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation prompt"),
) -> None:
    """Permanently remove one or more tasks from any column (proposed/doing/done/failed)."""
    mas = project_dir()
    proj = project_root()

    located: list[tuple[str, str]] = []
    missing: list[str] = []
    for tid in task_ids:
        found = board.find_task(mas, tid)
        if found is None:
            missing.append(tid)
        else:
            col, _ = found
            located.append((tid, col))

    for tid in missing:
        typer.echo(f"not found: {tid}")
    if not located:
        raise typer.Exit(1)

    if not assume_yes:
        typer.echo("Will delete:")
        for tid, col in located:
            typer.echo(f"  - {tid} (from {col}/)")
        if not typer.confirm("Proceed? This cannot be undone.", default=False):
            typer.echo("aborted")
            raise typer.Exit(1)

    deleted = 0
    for tid, col in located:
        try:
            board.delete_task(mas, tid, project_root=proj)
            typer.echo(f"deleted {tid} (from {col}/)")
            deleted += 1
        except FileNotFoundError:
            typer.echo(f"not found: {tid}")
    if missing:
        raise typer.Exit(1)
    typer.echo(f"{deleted} deleted")


@app.command()
def prune() -> None:
    """Remove worktree directories from done/ and failed/ tasks."""
    mas = project_dir()
    root = project_root()
    done_tasks = board.list_column(mas, "done")
    failed_tasks = board.list_column(mas, "failed")
    all_tasks = done_tasks + failed_tasks
    pruned_count = 0
    total_count = len(all_tasks)

    for task_dir in all_tasks:
        worktree_dir = task_dir / "worktree"
        if not worktree_dir.exists():
            continue
        try:
            worktree.prune(root, worktree_dir, keep_branch=True)
            console.print(f"[green]✔[/green] Pruned {task_dir.name}/worktree")
            pruned_count += 1
        except Exception as e:
            console.print(f"[yellow]⚠[/yellow] Failed to prune {task_dir.name}/worktree: {e}")

    console.print(f"Pruned {pruned_count} worktrees from {total_count} completed tasks")


def _reset_task_state(task_dir: Path) -> None:
    """Clear execution state so the tick loop re-runs from scratch."""
    # Remove top-level result (marks the task as done/failed)
    (task_dir / "result.json").unlink(missing_ok=True)
    # Reset orchestrator attempt counter and its previous_failure marker
    (task_dir / ".orchestrator_attempt").unlink(missing_ok=True)
    (task_dir / ".previous_failure").unlink(missing_ok=True)
    # Remove plan so orchestrator re-runs from scratch
    (task_dir / "plan.json").unlink(missing_ok=True)
    # Per-attempt logs gate orphan detection (`{role}-{attempt}.log`); leaving
    # them in place after an attempt-counter reset would make tick treat the
    # next attempt as a stale orphan and re-fail without dispatching.
    _clear_attempt_logs(task_dir / "logs")
    # Reset each subtask: remove results, attempt counter, and failure markers
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


@app.command()
def logs(
    task_id: str,
    follow: bool = typer.Option(False, "-f", "--follow"),
) -> None:
    """Show logs for a task (most recent role log)."""
    mas = project_dir()
    located = board.find_task(mas, task_id)
    if located is None:
        typer.echo(f"not found: {task_id}")
        raise typer.Exit(1)
    _, tdir = located
    log_dir = tdir / "logs"
    if not log_dir.exists():
        typer.echo(f"no logs for {task_id}")
        raise typer.Exit(0)
    latest = max(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, default=None)
    if latest is None:
        typer.echo(f"no logs for {task_id}")
        raise typer.Exit(0)
    if follow:
        subprocess.run(["tail", "-f", str(latest)])
    else:
        sys.stdout.write(latest.read_text())


@app.command()
def tail(
    task_id: str,
    lines: int = typer.Option(10, "-n", "--lines", help="Number of historical lines to show"),
    follow: bool = typer.Option(False, "-f", "--follow", help="Keep stream open until EOF"),
) -> None:
    """Stream task logs (last N lines, optionally follow)."""
    import signal

    mas = project_dir()
    located = board.find_task(mas, task_id)
    if located is None:
        typer.echo(f"not found: {task_id}")
        raise typer.Exit(1)
    _, tdir = located
    log_dir = tdir / "logs"
    if not log_dir.exists():
        typer.echo(f"no logs for {task_id}")
        raise typer.Exit(0)
    latest = max(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, default=None)
    if latest is None:
        typer.echo(f"no logs for {task_id}")
        raise typer.Exit(0)

    cmd = ["tail", f"-n{lines}"]
    if follow:
        cmd.append("-f")
    cmd.append(str(latest))

    proc = subprocess.Popen(cmd)
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.send_signal(signal.SIGTERM)
        proc.wait()


@app.command()
def audit(
    task_id: str,
    role: str | None = typer.Option(None, "--role", help="Filter events by role"),
    status: str | None = typer.Option(None, "--status", help="Filter events by status"),
    since: str | None = typer.Option(None, "--since", help="Show events at or after this ISO timestamp"),
    until: str | None = typer.Option(None, "--until", help="Show events at or before this ISO timestamp"),
) -> None:
    """Display a formatted audit timeline for a task and its subtasks."""
    from . import audit as _audit

    mas = project_dir()
    located = board.find_task(mas, task_id)
    if located is None:
        typer.echo(f"task not found: {task_id}")
        raise typer.Exit(1)

    _, tdir = located
    events = _audit.read_events(tdir, role=role, status=status, since=since, until=until)

    table = Table("timestamp", "event", "role", "provider", "subtask_id", "status", "duration_s", "summary")
    for e in events:
        table.add_row(
            (e.get("timestamp") or "")[:19],
            e.get("event") or "",
            e.get("role") or "",
            e.get("provider") or "",
            e.get("subtask_id") or "",
            e.get("status") or "",
            str(e["duration_s"]) if e.get("duration_s") is not None else "",
            e.get("summary") or "",
        )
    console.print(table)


@app.command()
def cost(task_id: str) -> None:
    """Show per-subtask cost breakdown for a task."""
    mas = project_dir()
    located = board.find_task(mas, task_id)
    if located is None:
        typer.echo(f"error: task not found: {task_id}")
        raise typer.Exit(1)

    _, tdir = located

    table = Table("subtask", "role", "tokens_in", "tokens_out", "cost_usd")
    total_in = 0
    total_out = 0
    total_cost = 0.0

    plan_path = tdir / "plan.json"
    if plan_path.exists():
        try:
            plan = board.read_plan(tdir)
        except Exception:
            plan = None

        if plan is not None:
            subtasks_dir = tdir / "subtasks"
            for spec in plan.subtasks:
                r = board.read_result(subtasks_dir / spec.id) if subtasks_dir.exists() else None
                tin = r.tokens_in if r is not None else None
                tout = r.tokens_out if r is not None else None
                cusd = r.cost_usd if r is not None else None
                total_in += tin or 0
                total_out += tout or 0
                total_cost += cusd or 0.0
                table.add_row(
                    spec.id,
                    spec.role,
                    str(tin) if tin is not None else "-",
                    str(tout) if tout is not None else "-",
                    f"{cusd:.6f}" if cusd is not None else "-",
                )
    else:
        parent_result = board.read_result(tdir)
        if parent_result is not None:
            total_in = parent_result.tokens_in or 0
            total_out = parent_result.tokens_out or 0
            total_cost = parent_result.cost_usd or 0.0
            table.add_row(
                task_id,
                "-",
                str(parent_result.tokens_in) if parent_result.tokens_in is not None else "-",
                str(parent_result.tokens_out) if parent_result.tokens_out is not None else "-",
                f"{parent_result.cost_usd:.6f}" if parent_result.cost_usd is not None else "-",
            )

    table.add_row("TOTAL", "", str(total_in), str(total_out), f"{total_cost:.6f}", style="bold")
    console.print(table)

    parent_task = board.read_task(tdir)
    budget = getattr(parent_task, "cost_budget_usd", None)
    if budget is not None:
        pct = (total_cost / budget * 100) if budget > 0 else 0.0
        console.print(f"Budget: {total_cost:.6f} / {budget:.6f} ({pct:.1f}% utilized)")



@app.command()
def events(
    task: str | None = typer.Option(None, "--task", help="Filter by task id"),
    role: str | None = typer.Option(None, "--role", help="Filter by role"),
    status: str | None = typer.Option(None, "--status", help="Filter by status"),
    event: str | None = typer.Option(None, "--event", help="Filter by event type"),
    since: str | None = typer.Option(None, "--since", help="Show events at or after this ISO timestamp"),
    until: str | None = typer.Option(None, "--until", help="Show events at or before this ISO timestamp"),
    json_output: bool = typer.Option(False, "--json", is_flag=True, help="Emit newline-delimited JSON"),
    follow: bool = typer.Option(False, "--follow", "-f", is_flag=True, help="Follow mode: stream new events"),
    interval: float = typer.Option(2.0, "--interval", help="Poll interval in seconds for --follow"),
) -> None:
    """Stream board-wide audit events across all tasks."""
    import json as _json
    import time

    from .events import read_board_events

    mas = project_dir()

    def _fetch() -> list:
        return read_board_events(
            mas, task=task, role=role, status=status, event=event, since=since, until=until
        )

    def _emit(evts: list) -> None:
        if json_output:
            for e in evts:
                typer.echo(_json.dumps(e))
        else:
            for e in evts:
                typer.echo(
                    f"{(e.get('timestamp') or '')[:19]}  "
                    f"{e.get('task_id') or '':30}  "
                    f"{e.get('event') or '':20}  "
                    f"{e.get('role') or '':15}  "
                    f"{e.get('status') or '':10}  "
                    f"{e.get('summary') or ''}"
                )

    if follow:
        seen = 0
        try:
            while True:
                all_evts = _fetch()
                new_evts = all_evts[seen:]
                seen = len(all_evts)
                _emit(new_evts)
                time.sleep(interval)
        except KeyboardInterrupt:
            pass
        return

    all_evts = _fetch()
    if json_output:
        for e in all_evts:
            typer.echo(_json.dumps(e))
    else:
        table = Table("timestamp", "task_id", "event", "role", "status", "summary")
        for e in all_evts:
            table.add_row(
                (e.get("timestamp") or "")[:19],
                e.get("task_id") or "",
                e.get("event") or "",
                e.get("role") or "",
                e.get("status") or "",
                e.get("summary") or "",
            )
        console.print(table)


@app.command()
def stats(
    since: str | None = typer.Option(
        None,
        "--since",
        help="Limit to tasks with a transition within this window (e.g. 1h, 2d, 1w).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead of Rich table."),
) -> None:
    """Show board statistics: counts, success rate, duration, tokens, and cost."""
    import json as _json
    from .stats import compute_stats, parse_since

    if since is not None:
        try:
            parse_since(since)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="'--since'")

    mas = project_dir()
    data = compute_stats(mas, since=since)

    if json_output:
        typer.echo(_json.dumps(data))
        return

    # Text output — use typer.echo so output is captured by CliRunner
    b = data["board"]
    typer.echo("Board:")
    for col in ("proposed", "doing", "done", "failed"):
        typer.echo(f"  {col}: {b[col]}")

    typer.echo(f"success_rate: {data['success_rate']:.1%}  revision_rate: {data['revision_rate']:.1%}")

    if data["roles"]:
        typer.echo("Durations by role:")
        for role_name, rs in sorted(data["roles"].items()):
            typer.echo(
                f"  {role_name}: count={rs['count']} mean={rs['mean_s']:.1f}s"
                f" p50={rs['p50_s']:.1f}s p95={rs['p95_s']:.1f}s"
            )

    if data["providers"]:
        typer.echo("Providers:")
        for pname, cnt in sorted(data["providers"].items()):
            typer.echo(f"  {pname}: {cnt}")

    tk = data["tokens"]
    typer.echo(
        f"tokens_in: {tk['tokens_in']}  tokens_out: {tk['tokens_out']}  cost_usd: {tk['cost_usd']:.4f}"
    )
    typer.echo(f"env_errors: {data['env_errors']}")


cron_app = typer.Typer(no_args_is_help=True, help="Cron schedule for `mas tick`.")
app.add_typer(cron_app, name="cron")


@cron_app.command("install")
def cron_install(
    interval: int = typer.Option(5, "--interval", help="Minutes between ticks"),
) -> None:
    cron.install(project_root(), interval_minutes=interval)
    typer.echo("cron installed")


@cron_app.command("uninstall")
def cron_uninstall() -> None:
    cron.uninstall(project_root())
    typer.echo("cron uninstalled")


@cron_app.command("status")
def cron_status() -> None:
    typer.echo(cron.status(project_root()))

daemon_app = typer.Typer(
    no_args_is_help=True,
    help="Run `mas tick` on an interval via a detached daemon (no system cron).",
)
app.add_typer(daemon_app, name="daemon")


_SECRET_WORDS = frozenset({"key", "token", "secret", "password"})


def _is_sensitive_key(key: str) -> bool:
    kl = key.lower()
    return any(w in kl for w in _SECRET_WORDS)


def _mask_url_query(url: str) -> str:
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(url)
    if not parsed.query:
        return url
    parts = []
    for segment in parsed.query.split("&"):
        if "=" in segment:
            k, _ = segment.split("=", 1)
            parts.append(f"{k}=***" if _is_sensitive_key(k) else segment)
        else:
            parts.append(segment)
    return urlunparse(parsed._replace(query="&".join(parts)))


def _mask_secrets(obj, parent_key=""):
    if isinstance(obj, dict):
        return {k: _mask_secrets(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_secrets(item, parent_key) for item in obj]
    if isinstance(obj, str):
        if _is_sensitive_key(parent_key):
            return "***"
        if "?" in obj and (obj.startswith("http://") or obj.startswith("https://")):
            return _mask_url_query(obj)
    return obj


config_app = typer.Typer(
    no_args_is_help=True,
    help="Manage and inspect mas configuration.",
)
app.add_typer(config_app, name="config")


@config_app.command("show")
def config_show(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead of YAML"),
    yaml_output: bool = typer.Option(False, "--yaml", help="Emit YAML (default)"),
    field: str | None = typer.Option(None, "--field", help="Print a single field value"),
    unsafe_show_secrets: bool = typer.Option(
        False, "--unsafe-show-secrets", help="Do not mask sensitive values"
    ),
) -> None:
    """Show the effective configuration merged from all sources."""
    import json as _json
    import yaml as _yaml
    from .config import load_config as _load_config
    from .errors import ConfigValidationError as _ConfigValidationError

    if json_output and yaml_output:
        typer.echo("Error: --yaml and --json are mutually exclusive", err=True)
        raise typer.Exit(1)

    mas = project_dir()

    config_path = mas / "config.yaml"
    if config_path.exists():
        try:
            _yaml.safe_load(config_path.read_text())
        except _yaml.YAMLError as exc:
            typer.echo(f"Error: invalid YAML in {config_path}: {exc}", err=True)
            raise typer.Exit(1)

    try:
        config = _load_config(mas)
    except _ConfigValidationError as exc:
        typer.echo(f"Validation error: {exc}", err=True)
        raise typer.Exit(1)

    data = {
        "config": config.model_dump(),
        "roles": {name: rc.model_dump() for name, rc in config.roles.items()},
    }

    if not unsafe_show_secrets:
        data = _mask_secrets(data)

    if field is not None:
        value = data
        for seg in field.split("."):
            if isinstance(value, dict):
                if seg not in value:
                    typer.echo(f"Field not found: {field}", err=True)
                    raise typer.Exit(2)
                value = value[seg]
            elif isinstance(value, list):
                if seg.isdigit() and int(seg) < len(value):
                    value = value[int(seg)]
                else:
                    typer.echo(f"Field not found: {field}", err=True)
                    raise typer.Exit(2)
            else:
                typer.echo(f"Field not found: {field}", err=True)
                raise typer.Exit(2)
        if isinstance(value, (dict, list)):
            if json_output:
                typer.echo(_json.dumps(value, indent=2, sort_keys=False))
            else:
                typer.echo(_yaml.safe_dump(value, sort_keys=False), nl=False)
        else:
            typer.echo(str(value))
        return

    if json_output:
        typer.echo(_json.dumps(data, indent=2, sort_keys=False))
    else:
        typer.echo(_yaml.safe_dump(data, sort_keys=False), nl=False)


@daemon_app.command("start")
def daemon_start(
    interval: int = typer.Option(300, "--interval", help="Seconds between ticks"),
    json_logs: bool = typer.Option(False, "--json-logs", help="Emit structured JSON logs to daemon.log"),
) -> None:
    proj = project_root()
    pid = daemon.start(proj, interval_seconds=interval, json_logs=json_logs)
    typer.echo(f"daemon started (pid {pid}, interval {interval}s)")


@daemon_app.command("stop")
def daemon_stop() -> None:
    stopped = daemon.stop(project_root())
    typer.echo("daemon stopped" if stopped else "no daemon running")


@daemon_app.command("status")
def daemon_status() -> None:
    pid, running = daemon.status(project_root())
    if pid is None:
        typer.echo("no daemon (no pid file)")
    elif running:
        typer.echo(f"daemon running (pid {pid})")
    else:
        typer.echo(f"stale pid file (pid {pid} not alive)")


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", "--host", help="Interface to bind (default loopback)"),
    port: int = typer.Option(8765, "--port", help="Port to bind"),
    reload: bool = typer.Option(False, "--reload", help="Enable uvicorn auto-reload (dev)"),
) -> None:
    """Serve a local web UI for monitoring the board and triggering actions."""
    try:
        import uvicorn
    except ImportError:
        typer.echo('web deps missing — install with: pip install "mas[web]"')
        raise typer.Exit(1)

    from .web.app import create_app

    proj = project_root()
    typer.echo(f"mas web serving {proj} on http://{host}:{port}")
    uvicorn.run(
        create_app(proj),
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


@app.command()
def trace(
    task_id: str = typer.Argument(..., help="Task ID to trace"),
    json_out: bool = typer.Option(False, "--json", "-j", help="Output trace as JSON"),
) -> None:
    """Trace the lifecycle of a task and its subtasks."""
    from . import trace as trace_mod

    mas = project_dir()
    task_dir = None
    for column in ("doing", "done", "failed"):
        candidate = mas / "tasks" / column / task_id
        if candidate.is_dir():
            task_dir = candidate
            break

    if task_dir is None:
        typer.echo(f"Task '{task_id}' not found")
        raise typer.Exit(1)

    trace_data = trace_mod.build_trace(task_dir)
    stages = trace_data["stages"]

    if json_out:
        typer.echo(json.dumps(trace_data))
        return

    if not stages:
        typer.echo("no stage data yet")
        return

    console.print(f"[bold]Task:[/bold] {trace_data['task_id']}")
    console.print(f"[bold]Goal:[/bold] {trace_data['goal']}")
    console.print(f"[bold]Started:[/bold] {trace_data['started_at']}")
    console.print(f"[bold]Ended:[/bold] {trace_data['ended_at']}")
    console.print(f"[bold]Duration:[/bold] {trace_data['total_duration_s']:.1f}s  "
                  f"[bold]Cost:[/bold] ${trace_data['total_cost_usd']:.4f}")
    console.print()

    _COLOR = {
        "success": "green",
        "failure": "red",
        "needs_revision": "yellow",
        "running": "dim",
        "in_progress": "dim",
    }

    table = Table(show_header=True, header_style="bold")
    table.add_column("Stage", style="cyan")
    table.add_column("Status")
    table.add_column("Started")
    table.add_column("Duration")
    table.add_column("Cost")

    from rich.markup import escape as _escape

    for s in stages:
        label = f"{s['role']}[{s['cycle']}]"
        status = s["status"]
        color = _COLOR.get(status, "white")
        started = (s.get("started_at") or "")[:19]
        dur = f"{s['duration_s']:.1f}s" if s["duration_s"] is not None else "..."
        cost = f"${s['cost_usd']:.4f}" if s["cost_usd"] is not None else "-"
        table.add_row(
            f"[{color}]{_escape(label)}[/{color}]",
            f"[{color}]{status}[/{color}]",
            started,
            dur,
            cost,
        )

    console.print(table)


@app.command()
def doctor(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead of Rich table"),
    strict: bool = typer.Option(False, "--strict", help="Exit 1 even if only warnings found"),
) -> None:
    """Run system health checks (Config, Provider, Board/Worktree, Daemon)."""
    import json as _json

    from .config import project_dir
    from .doctor import run_checks

    mas = project_dir()
    checks = run_checks(mas)

    fail_count = sum(1 for c in checks if c["status"] == "FAIL")
    warn_count = sum(1 for c in checks if c["status"] == "WARN")
    ok_count = sum(1 for c in checks if c["status"] == "OK")

    if json_output:
        typer.echo(_json.dumps({"checks": list(checks), "summary": {"ok": ok_count, "warn": warn_count, "fail": fail_count}}))
    else:
        table = Table("Check", "Status", "Detail")
        status_colors = {"OK": "green", "WARN": "yellow", "FAIL": "red"}
        for c in checks:
            status = c["status"]
            color = status_colors.get(status, "white")
            table.add_row(
                f"{c['group']} / {c['name']}",
                f"[{color}]{status}[/{color}]",
                c["detail"],
            )
        console.print(table)

    if fail_count > 0 or (strict and warn_count > 0):
        raise typer.Exit(1)


@app.command()
def pr(
    task_id: str = typer.Argument(..., help="Task ID (must be in done/)"),
    draft: bool = typer.Option(False, "--draft", help="Create as draft PR"),
    base: str | None = typer.Option(None, "--base", help="Base branch (default: repo default)"),
    reviewer: list[str] | None = typer.Option(None, "--reviewer", help="Add a reviewer (repeatable)"),
) -> None:
    """Open a GitHub pull request for a completed task."""
    branch = f"mas/{task_id}"
    mas = project_dir()

    # (1) Check gh is installed
    if shutil.which("gh") is None:
        typer.echo(
            "gh (GitHub CLI) is not installed.\n"
            "Install it from https://cli.github.com/"
        )
        raise typer.Exit(2)

    # (2) Find task — must be in done/
    done_dir = mas / "tasks" / "done" / task_id
    if not done_dir.is_dir():
        for col in ("doing", "proposed", "failed"):
            if (mas / "tasks" / col / task_id).is_dir():
                typer.echo(f"Task {task_id!r} is in {col}/, not done/")
                raise typer.Exit(1)
        typer.echo(f"Task {task_id!r} not found")
        raise typer.Exit(1)

    # (3) Check result.json exists
    result_path = done_dir / "result.json"
    if not result_path.exists():
        typer.echo(f"No result.json found for task {task_id}")
        raise typer.Exit(1)

    # (4) gh auth status
    auth = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    if auth.returncode != 0:
        typer.echo("Not authenticated with GitHub. Please run: gh auth login")
        raise typer.Exit(2)

    # (5) Check local branch exists
    branch_list = subprocess.run(
        ["git", "branch", "--list", branch], capture_output=True, text=True
    )
    if not branch_list.stdout.strip():
        typer.echo(f"Local branch {branch!r} does not exist. Run: git branch {branch}")
        raise typer.Exit(1)

    # (6) Push branch to origin if not already there
    ls_remote = subprocess.run(
        ["git", "ls-remote", "--exit-code", "--heads", "origin", branch],
        capture_output=True, text=True,
    )
    if ls_remote.returncode != 0:
        subprocess.run(["git", "push", "-u", "origin", branch], capture_output=True, text=True)

    # (7) Resolve base branch
    if base is None:
        view = subprocess.run(
            ["gh", "repo", "view", "--json", "defaultBranchRef"],
            capture_output=True, text=True,
        )
        view_data = json.loads(view.stdout)
        base = view_data["defaultBranchRef"]["name"]

    # (8) Build PR title
    result_data = json.loads(result_path.read_text())
    summary = result_data.get("summary") or ""

    task_path = done_dir / "task.json"
    goal = ""
    if task_path.exists():
        task_data = json.loads(task_path.read_text())
        goal = task_data.get("goal") or ""

    title = summary if summary else goal[:70]

    # Locate evaluator data from subtasks/
    eval_summary = ""
    eval_feedback = ""
    subtasks_dir = done_dir / "subtasks"
    if subtasks_dir.exists():
        eval_results = sorted(
            [p for p in subtasks_dir.glob("*/result.json") if "eval" in p.parent.name],
            key=lambda p: p.stat().st_mtime,
        )
        if eval_results:
            eval_data = json.loads(eval_results[-1].read_text())
            eval_summary = eval_data.get("summary") or ""
            eval_feedback = eval_data.get("feedback") or ""

    # Fall back to parent result.json
    if not eval_summary:
        eval_summary = result_data.get("summary") or ""
    if not eval_feedback:
        eval_feedback = result_data.get("feedback") or ""

    cost_usd = result_data.get("cost_usd")
    tokens_in = result_data.get("tokens_in")
    tokens_out = result_data.get("tokens_out")

    # (10) Build PR body
    body_parts = [f"## Goal\n\n{goal}"]
    if eval_summary:
        body_parts.append(f"## Summary\n\n{eval_summary}")
    if eval_feedback:
        body_parts.append(f"## Feedback\n\n{eval_feedback}")

    cost_lines = []
    if cost_usd is not None:
        cost_lines.append(f"cost_usd: {cost_usd}")
    if tokens_in is not None:
        cost_lines.append(f"tokens_in: {tokens_in}")
    if tokens_out is not None:
        cost_lines.append(f"tokens_out: {tokens_out}")
    if cost_lines:
        body_parts.append("## Cost\n\n" + "\n".join(cost_lines))

    body_parts.append("---\n\nGenerated by mas")
    body = "\n\n".join(body_parts)

    # (11) Build gh pr create command
    cmd = [
        "gh", "pr", "create",
        "--title", title,
        "--body", body,
        "--base", base,
        "--head", branch,
    ]
    if draft:
        cmd.append("--draft")
    for r in (reviewer or []):
        cmd.extend(["--reviewer", r])

    # (12) Run gh pr create
    create_result = subprocess.run(cmd, capture_output=True, text=True)

    if create_result.returncode != 0:
        url_match = re.search(r"https://github\.com/[^\s]+/pull/\d+", create_result.stderr)
        if url_match:
            typer.echo(url_match.group(0))
            raise typer.Exit(0)
        typer.echo(create_result.stderr)
        raise typer.Exit(create_result.returncode)

    # (13) Print PR URL on success
    typer.echo(create_result.stdout.strip())


proposals_app = typer.Typer(no_args_is_help=True, help="Proposal management commands.")
app.add_typer(proposals_app, name="proposals")


@proposals_app.command("rejected")
def proposals_rejected(
    since: str | None = typer.Option(None, "--since", help="Filter by time window (e.g. 1h, 2d, 1w)"),
    limit: int = typer.Option(50, "--limit", help="Maximum number of records (newest-first)"),
    json_output: bool = typer.Option(False, "--json", is_flag=True, help="Emit newline-delimited JSON"),
) -> None:
    """Show rejected (duplicate) proposals."""
    import json as _json
    from .proposals import read_rejected_proposals
    from .stats import parse_since

    if since is not None:
        try:
            parse_since(since)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="'--since'")

    mas = project_dir()
    rejected_path = mas / "proposals" / "rejected.jsonl"
    records = read_rejected_proposals(rejected_path, since=since, limit=limit)

    if json_output:
        for rec in records:
            typer.echo(_json.dumps(rec))
        return

    table = Table("timestamp", "summary", "score", "matched_task_id", "matched_column")
    for rec in records:
        table.add_row(
            rec.get("timestamp", ""),
            rec.get("summary", ""),
            f"{rec.get('similarity_score', 0.0):.3f}",
            rec.get("matched_task_id", ""),
            rec.get("matched_column", ""),
        )
    console.print(table)


if __name__ == "__main__":
    app()
