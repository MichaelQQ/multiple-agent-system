import os
from pathlib import Path

import pytest

from mas import board
from mas.schemas import Task


@pytest.fixture
def mas(tmp_path: Path) -> Path:
    d = tmp_path / ".mas"
    board.ensure_layout(d)
    return d


def test_ensure_layout(mas: Path):
    for col in board.COLUMNS:
        assert (mas / "tasks" / col).is_dir()


def test_move_task_between_columns(mas: Path):
    src = board.task_dir(mas, "proposed", "t1")
    src.mkdir(parents=True)
    board.write_task(src, Task(id="t1", role="proposer", goal="g"))
    dst = board.task_dir(mas, "doing", "t1")
    board.move(src, dst)
    assert not src.exists()
    assert (dst / "task.json").exists()
    assert board.read_task(dst).id == "t1"


def test_count_active_pids_clears_dead(mas: Path):
    tdir = board.task_dir(mas, "doing", "t2")
    tdir.mkdir(parents=True)
    pid_dir = tdir / "pids"
    pid_dir.mkdir()
    # Use a pid that is very unlikely to be alive.
    (pid_dir / "implementer.codex.pid").write_text("999999")
    assert board.count_active_pids(mas) == 0
    assert not (pid_dir / "implementer.codex.pid").exists()


def test_count_active_pids_counts_live(mas: Path):
    tdir = board.task_dir(mas, "doing", "t3")
    tdir.mkdir(parents=True)
    pid_dir = tdir / "pids"
    pid_dir.mkdir()
    (pid_dir / "implementer.codex.pid").write_text(str(os.getpid()))
    assert board.count_active_pids(mas) == 1
    assert board.count_active_pids(mas, "codex") == 1
    assert board.count_active_pids(mas, "gemini") == 0
