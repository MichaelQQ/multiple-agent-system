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
    src = board.task_dir(mas, "proposed", "20260415-t1-aaaa")
    src.mkdir(parents=True)
    board.write_task(src, Task(id="20260415-t1-aaaa", role="proposer", goal="g"))
    dst = board.task_dir(mas, "doing", "20260415-t1-aaaa")
    board.move(src, dst)
    assert not src.exists()
    assert (dst / "task.json").exists()
    assert board.read_task(dst).id == "20260415-t1-aaaa"


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


def test_delete_task_removes_from_any_column(mas: Path, tmp_path: Path):
    for col in board.COLUMNS:
        task_id = f"20260424-del{col[0]}-aaaa"
        tdir = board.task_dir(mas, col, task_id)
        board.write_task(tdir, Task(id=task_id, role="implementer", goal="g"))
        assert tdir.exists()
        result_col, _ = board.delete_task(mas, task_id, project_root=tmp_path)
        assert result_col == col
        assert not tdir.exists()


def test_delete_task_missing_raises(mas: Path, tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        board.delete_task(mas, "20260424-nope-0000", project_root=tmp_path)


def test_delete_task_clears_stale_pid_files(mas: Path, tmp_path: Path):
    task_id = "20260424-delp-bbbb"
    tdir = board.task_dir(mas, "doing", task_id)
    board.write_task(tdir, Task(id=task_id, role="implementer", goal="g"))
    pid_dir = tdir / "pids"
    pid_dir.mkdir()
    (pid_dir / "implementer.codex.pid").write_text("999999")
    board.delete_task(mas, task_id, project_root=tmp_path)
    assert not tdir.exists()
