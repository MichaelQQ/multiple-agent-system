from __future__ import annotations

import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import board, cron, daemon, transitions
from .config import PROJECT_DIR_NAME, project_dir, project_root

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
def tick() -> None:
    """Run a single tick of the orchestrator."""
    from .tick import run_tick

    run_tick()


@app.command()
def show() -> None:
    """Print the current board."""
    mas = project_dir()
    table = Table("column", "id", "role", "goal", "recent transitions")
    for col in board.COLUMNS:
        for d in board.list_column(mas, col):
            try:
                t = board.read_task(d)
                goal = t.goal[:60]
            except Exception:
                t = None
                goal = "?"
            txns = transitions.read_transitions(d, limit=5)
            txn_str = "\n".join(
                f"{x['timestamp'][11:19]} {x['from']}→{x['to']} ({x['reason']})"
                for x in txns
            ) if txns else ""
            table.add_row(col, d.name, t.role if t else "?", goal, txn_str)
    console.print(table)


@app.command()
def promote(task_id: str) -> None:
    """Move a proposal from proposed/ to doing/."""
    mas = project_dir()
    src = mas / "tasks" / "proposed" / task_id
    if not src.exists():
        typer.echo(f"not found: {src}")
        raise typer.Exit(1)
    dst = mas / "tasks" / "doing" / task_id
    board.move(src, dst)
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
    try:
        pid = daemon.start(project_root(), interval_seconds=interval)
    except daemon.DaemonError as e:
        typer.echo(str(e))
        raise typer.Exit(1)
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
