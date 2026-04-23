from __future__ import annotations

import difflib
import logging
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
) -> None:
    """Print the current board, or a task's subtask tree when an id is given."""
    mas = project_dir()
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


@daemon_app.command("start")
def daemon_start(
    interval: int = typer.Option(300, "--interval", help="Seconds between ticks"),
) -> None:
    proj = project_root()
    pid = daemon.start(proj, interval_seconds=interval)
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


if __name__ == "__main__":
    app()
