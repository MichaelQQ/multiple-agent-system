from __future__ import annotations

import errno
import logging
import os
import signal
import sys
import time
from pathlib import Path

log = logging.getLogger("mas.daemon")

PID_FILENAME = "daemon.pid"
LOG_FILENAME = "logs/daemon.log"


def _log_tick_event(msg: str, **kwargs) -> None:
    extra = {"component": "daemon"}
    extra.update(kwargs)
    log.info(msg, extra=extra)


class DaemonError(RuntimeError):
    pass


def _pid_path(mas: Path) -> Path:
    return mas / PID_FILENAME


def _log_path(mas: Path) -> Path:
    return mas / LOG_FILENAME


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

    stop_flag = {"stop": False}

    def _handle_term(signum, frame):
        stop_flag["stop"] = True

    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)

    try:
        _run_loop(project, interval_seconds, stop_flag)
    finally:
        _clear_pid(mas)
        os._exit(0)


def _run_loop(project: Path, interval_seconds: int, stop_flag: dict) -> None:
    from .tick import run_tick

    while not stop_flag["stop"]:
        started = time.time()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"=== {stamp} tick start ===", flush=True)
        try:
            run_tick(start=project)
            elapsed = time.time() - started
            _log_tick_event("tick completed", duration_s=elapsed)
        except Exception:
            elapsed = time.time() - started
            _log_tick_event("tick failed", duration_s=elapsed)
            log.exception("tick failed")
        print(f"=== tick done in {elapsed:.2f}s ===", flush=True)

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
