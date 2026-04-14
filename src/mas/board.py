from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Literal

from .schemas import Task

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


def move(src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        raise FileExistsError(f"destination exists: {dst}")
    # os.rename is atomic within a filesystem; shutil.move falls back otherwise.
    shutil.move(str(src), str(dst))
    return dst


def write_task(dir_: Path, task: Task) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    p = dir_ / "task.json"
    p.write_text(task.model_dump_json(indent=2))
    return p


def read_task(dir_: Path) -> Task:
    p = dir_ / "task.json"
    return Task.model_validate_json(p.read_text())


def read_result(dir_: Path):
    from .schemas import Result

    p = dir_ / "result.json"
    if not p.exists():
        return None
    return Result.model_validate_json(p.read_text())


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
    import os
    import errno

    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM
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
