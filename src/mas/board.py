from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Literal

from pydantic import ValidationError as PydanticValidationError
from .schemas import BoardSummary, Task, Result
from .errors import TaskReadError, ResultReadError

log = logging.getLogger("mas.board")

Column = Literal["proposed", "doing", "done", "failed"]
COLUMNS: tuple[Column, ...] = ("proposed", "doing", "done", "failed")


def tasks_root(mas_dir: Path) -> Path:
    return mas_dir / "tasks"


def column_dir(mas_dir: Path, column: Column) -> Path:
    return tasks_root(mas_dir) / column


def ensure_layout(mas_dir: Path) -> None:
    for col in COLUMNS:
        column_dir(mas_dir, col).mkdir(parents=True, exist_ok=True)
    (mas_dir / "logs").mkdir(parents=True, exist_ok=True)
    (mas_dir / "prompts").mkdir(parents=True, exist_ok=True)


def task_dir(mas_dir: Path, column: Column, task_id: str) -> Path:
    return column_dir(mas_dir, column) / task_id


def find_task(mas_dir: Path, task_id: str) -> tuple[Column, Path] | None:
    for col in COLUMNS:
        d = task_dir(mas_dir, col, task_id)
        if d.is_dir():
            return col, d
    # Nested under a parent in doing/
    for parent in column_dir(mas_dir, "doing").glob("*/subtasks/*"):
        if parent.name == task_id:
            return "doing", parent
    return None


def list_column(mas_dir: Path, column: Column) -> list[Path]:
    d = column_dir(mas_dir, column)
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir() if p.is_dir())


def move(src: Path, dst: Path, *, reason: str = "", webhooks=None) -> Path:
    from . import transitions as _tr
    from . import audit as _audit
    from_state = src.parent.name
    to_state = dst.parent.name
    task_id = src.name
    _tr.log_transition(src, from_state, to_state, reason)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        raise FileExistsError(f"destination exists: {dst}")
    shutil.move(str(src), str(dst))
    log.info("task moved", extra={"task_id": task_id, "from_column": from_state, "to_column": to_state, "reason": reason})
    _audit.append_event(
        dst,
        event="state_transition",
        task_id=task_id,
        summary=f"{from_state} → {to_state} ({reason})" if reason else f"{from_state} → {to_state}",
        details={"reason": reason},
    )
    if webhooks is not None:
        from .notify import fire_webhooks
        try:
            _task = read_task(dst)
            _role = _task.role
            _goal = _task.goal
        except Exception:
            _role = ""
            _goal = ""
        _result = read_result(dst)
        payload = {
            "task_id": task_id,
            "role": _role,
            "goal": _goal,
            "from": from_state,
            "to": to_state,
            "summary": _result.summary if _result else None,
            "status": _result.status if _result else None,
            "task_dir": str(dst),
        }
        fire_webhooks(webhooks, payload)
    return dst


def delete_task(mas_dir: Path, task_id: str, *, project_root: Path | None = None) -> tuple[Column, Path]:
    """Locate a task in any column, kill its live workers, prune its worktree, and remove it.

    Returns (column, original_dir) for the removed task. Raises FileNotFoundError if
    the task is not on the board.
    """
    import os
    import signal
    import time

    located = find_task(mas_dir, task_id)
    if located is None:
        raise FileNotFoundError(f"task not found: {task_id}")
    col, tdir = located

    killed: list[int] = []
    for pid_file in tdir.glob("**/pids/*.pid"):
        try:
            pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            continue
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                killed.append(pid)
            except ProcessLookupError:
                pass
    deadline = time.time() + 3.0
    while killed and time.time() < deadline:
        killed = [p for p in killed if _pid_alive(p)]
        if killed:
            time.sleep(0.1)
    for pid in killed:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    if project_root is not None:
        from . import worktree as _worktree
        for wt in [tdir / "worktree", *tdir.glob("subtasks/*/worktree")]:
            if wt.exists():
                try:
                    _worktree.prune(project_root, wt, keep_branch=True)
                except Exception:
                    log.warning("worktree prune failed", extra={"task_id": task_id, "worktree": str(wt)})

    shutil.rmtree(tdir)
    log.info("task deleted", extra={"task_id": task_id, "from_column": col})
    return col, tdir


def write_task(dir_: Path, task: Task) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    p = dir_ / "task.json"
    p.write_text(task.model_dump_json(indent=2))
    # Create initial transition log entry for new tasks.
    from . import transitions as _tr
    state = dir_.parent.name  # e.g. "proposed", "doing"
    _tr.ensure_initial_log(dir_, state)
    return p


def read_task(dir_: Path) -> Task:
    p = dir_ / "task.json"
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise TaskReadError(
            f"Failed to read task file: encoding error",
            path=str(p),
            cause=e,
        )
    except OSError as e:
        raise TaskReadError(
            f"Failed to read task file: {e}",
            path=str(p),
            cause=e,
        )

    if not text.strip():
        raise TaskReadError(
            "Task file is empty",
            path=str(p),
            raw_snippet=text,
        )

    try:
        return Task.model_validate_json(text)
    except PydanticValidationError as e:
        errors = []
        for err in e.errors():
            field = " -> ".join(str(l) for l in err["loc"])
            errors.append(f"{field}: {err['msg']}")
        raise TaskReadError(
            f"Missing or invalid fields: {'; '.join(errors)}",
            path=str(p),
            raw_snippet=text[:200],
            cause=e,
        )


def read_result(dir_: Path) -> Result | None:
    p = dir_ / "result.json"
    if not p.exists():
        return None

    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise ResultReadError(
            f"Failed to read result file: encoding error",
            path=str(p),
            cause=e,
        )
    except OSError as e:
        raise ResultReadError(
            f"Failed to read result file: {e}",
            path=str(p),
            cause=e,
        )

    if not text.strip():
        raise ResultReadError(
            "Result file is empty",
            path=str(p),
            raw_snippet=text,
        )

    try:
        return Result.model_validate_json(text)
    except PydanticValidationError as e:
        errors = []
        for err in e.errors():
            field = " -> ".join(str(l) for l in err["loc"])
            errors.append(f"{field}: {err['msg']}")
        raise ResultReadError(
            f"Missing or invalid fields: {'; '.join(errors)}",
            path=str(p),
            raw_snippet=text[:200],
            cause=e,
        )


def read_plan(dir_: Path):
    from .schemas import Plan

    p = dir_ / "plan.json"
    return Plan.model_validate_json(p.read_text())


def count_active_pids(mas_dir: Path, provider: str | None = None) -> int:
    """Count live worker PIDs across all doing/ tasks and their subtasks.

    If provider is given, only PID files tagged with that provider are counted
    (tag is encoded in the PID filename suffix, e.g. pids/implementer.codex.pid).
    """
    n = 0
    for doing in column_dir(mas_dir, "doing").glob("**/pids/*.pid"):
        if provider and f".{provider}." not in doing.name:
            continue
        try:
            pid = int(doing.read_text().strip())
        except (ValueError, OSError):
            continue
        if _pid_alive(pid):
            n += 1
        else:
            doing.unlink(missing_ok=True)
    return n


def _pid_alive(pid: int) -> bool:
    import errno
    import os
    import subprocess

    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM
    try:
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "state="],
            capture_output=True, text=True, timeout=2,
        )
        state = r.stdout.strip()
        return bool(state) and state != "Z"
    except Exception:
        return True


def write_pid(pid_dir: Path, role: str, provider: str, pid: int) -> Path:
    pid_dir.mkdir(parents=True, exist_ok=True)
    p = pid_dir / f"{role}.{provider}.pid"
    p.write_text(str(pid))
    return p


def clear_pid(pid_dir: Path, role: str, provider: str) -> None:
    p = pid_dir / f"{role}.{provider}.pid"
    p.unlink(missing_ok=True)


def read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def get_summary(mas_dir: Path) -> BoardSummary:
    return BoardSummary(
        proposed=[p.name for p in list_column(mas_dir, "proposed")],
        doing=[p.name for p in list_column(mas_dir, "doing")],
        done=[p.name for p in list_column(mas_dir, "done")],
        failed=[p.name for p in list_column(mas_dir, "failed")],
    )
