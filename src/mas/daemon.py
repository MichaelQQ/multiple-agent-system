from __future__ import annotations

import errno
import logging
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ConfigWatcher as ConfigWatcher
from .config import load_config as _load_config
from .schemas import MasConfig

log = logging.getLogger("mas.daemon")

PID_FILENAME = "daemon.pid"
INTERVAL_FILENAME = "daemon.interval"
LOG_FILENAME = "logs/daemon.log"
DEFAULT_INTERVAL_SECONDS = 300


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _say(msg: str) -> None:
    print(f"[{_stamp()}] {msg}", flush=True)


class DaemonError(RuntimeError):
    pass


def _pid_path(mas: Path) -> Path:
    return mas / PID_FILENAME


def _interval_path(mas: Path) -> Path:
    return mas / INTERVAL_FILENAME


def _log_path(mas: Path) -> Path:
    return mas / LOG_FILENAME


def read_interval(mas: Path) -> int:
    """Return the interval recorded by the last `start`, or the default."""
    p = _interval_path(mas)
    if not p.exists():
        return DEFAULT_INTERVAL_SECONDS
    try:
        value = int(p.read_text().strip())
    except (ValueError, OSError):
        return DEFAULT_INTERVAL_SECONDS
    return value if value > 0 else DEFAULT_INTERVAL_SECONDS


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM
    return True


def _read_pid(mas: Path) -> int | None:
    p = _pid_path(mas)
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except (ValueError, OSError):
        return None


def _clear_pid(mas: Path) -> None:
    _pid_path(mas).unlink(missing_ok=True)
    _interval_path(mas).unlink(missing_ok=True)


def start(project: Path, interval_seconds: int = 300) -> int:
    """Fork a detached daemon that runs tick every interval_seconds.

    Returns the daemon PID. Raises DaemonError if one is already running.
    """
    from .config import project_dir, validate_config

    mas = project_dir(project)
    mas.mkdir(parents=True, exist_ok=True)
    (mas / "logs").mkdir(exist_ok=True)

    from .config import load_config as load_cfg
    cfg = load_cfg(mas)
    issues = validate_config(cfg, mas)
    if issues:
        raise DaemonError("validation failed: " + "; ".join(f"{i.field}: {i.message}" for i in issues))

    existing = _read_pid(mas)
    if existing is not None and _pid_alive(existing):
        raise DaemonError(f"daemon already running (pid {existing})")
    if existing is not None:
        _clear_pid(mas)

    # First fork
    pid = os.fork()
    if pid > 0:
        # Parent: wait briefly for child to write pid file
        for _ in range(50):
            time.sleep(0.02)
            child_pid = _read_pid(mas)
            if child_pid is not None:
                return child_pid
        raise DaemonError("daemon failed to start (no pid file written)")

    # Child: detach
    os.setsid()
    pid = os.fork()
    if pid > 0:
        os._exit(0)

    # Grandchild: real daemon
    os.chdir(str(project))
    os.umask(0o022)

    log_file = open(_log_path(mas), "ab", buffering=0)
    devnull = open(os.devnull, "rb")
    os.dup2(devnull.fileno(), sys.stdin.fileno())
    os.dup2(log_file.fileno(), sys.stdout.fileno())
    os.dup2(log_file.fileno(), sys.stderr.fileno())

    _pid_path(mas).write_text(f"{os.getpid()}\n")
    _interval_path(mas).write_text(f"{interval_seconds}\n")

    stop_flag = {"stop": False}

    def _handle_term(signum, frame):
        stop_flag["stop"] = True

    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)

    _say(
        f"daemon started (pid={os.getpid()}, interval={interval_seconds}s, project={project})"
    )

    try:
        _run_loop(project, interval_seconds, stop_flag)
    finally:
        _say("daemon stopped")
        _clear_pid(mas)
        os._exit(0)


def _run_loop(project: Path, interval_seconds: int, stop_flag: dict) -> None:
    from .tick import run_tick
    from .config import load_config as load_cfg, project_dir

    ConfigWatcher = __import__("mas.daemon", fromlist=["ConfigWatcher"]).ConfigWatcher

    mas = project_dir(project)
    config_path = mas / "config.yaml"
    watcher = ConfigWatcher(config_path)

    current_config = load_cfg(mas)
    tick_num = 0
    while not stop_flag["stop"]:
        tick_num += 1
        started = time.time()

        if watcher.has_changed():
            new_config, changes = _check_reload_config(project, current_config)
            if new_config is not current_config:
                if changes:
                    summary = ", ".join(f"{c[0]}: {c[1]}->{c[2]}" for c in changes)
                    log.info("config reloaded", summary=summary)
                current_config = new_config
            watcher.mark_checked()

        _say(f"tick #{tick_num} start")
        try:
            run_tick(start=project, cfg=current_config)
            elapsed = time.time() - started
            _say(f"tick #{tick_num} done in {elapsed:.2f}s")
        except Exception as exc:
            elapsed = time.time() - started
            _say(
                f"tick #{tick_num} failed in {elapsed:.2f}s: "
                f"{type(exc).__name__}: {exc}"
            )
            log.exception("tick failed")

        remaining = interval_seconds
        while remaining > 0 and not stop_flag["stop"]:
            time.sleep(min(1, remaining))
            remaining -= 1


def stop(project: Path, timeout: float = 10.0) -> bool:
    """Signal the daemon to exit. Returns True if a daemon was stopped."""
    from .config import project_dir

    mas = project_dir(project)
    pid = _read_pid(mas)
    if pid is None:
        return False
    if not _pid_alive(pid):
        _clear_pid(mas)
        return False

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _clear_pid(mas)
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            _clear_pid(mas)
            return True
        time.sleep(0.1)

    # Still alive after timeout — escalate
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    _clear_pid(mas)
    return True


def status(project: Path) -> tuple[int | None, bool]:
    """Return (pid, running). pid is None if no pid file."""
    from .config import project_dir

    mas = project_dir(project)
    pid = _read_pid(mas)
    if pid is None:
        return None, False
    return pid, _pid_alive(pid)


def _check_reload_config(project: Path, previous_config: "MasConfig") -> tuple["MasConfig", list[tuple[str, str, str]]]:
    """Check if config.yaml changed, reload if valid, otherwise return previous_config.
    
    Returns (new_config, changes) where changes is a list of (field_path, old_value, new_value).
    """
    from .config import load_config as load_cfg, project_dir, validate_config, ConfigValidationError
    from .errors import ConfigValidationError as ConfigValErr

    mas = project_dir(project)
    try:
        new_config = load_cfg(mas)
    except (ConfigValidationError, ConfigValErr) as e:
        log.warning("config reload skipped: failed to load config: %s", e)
        return previous_config, []

    issues = validate_config(new_config, mas)
    if issues:
        log.warning("config reload skipped: validation failed: %s", "; ".join(f"{i.field}: {i.message}" for i in issues))
        return previous_config, []

    from .config import config_diff
    changes = config_diff(previous_config, new_config)

    if changes:
        log.info("config reloaded: %d changes", len(changes))

    return new_config, changes