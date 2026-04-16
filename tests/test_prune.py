from unittest.mock import patch, MagicMock
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mas import cli, board


@pytest.fixture
def mas_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".mas"
    board.ensure_layout(d)
    return d


@pytest.fixture
def mock_project_root(mas_dir: Path, monkeypatch):
    def mock_root():
        return tmp_path.parent

    tmp_path.parent.joinpath("src").mkdir(parents=True, exist_ok=True)
    return mas_dir


runner = CliRunner()


def test_prune_nominal(monkeypatch, mas_dir: Path, tmp_path: Path):
    done1 = board.task_dir(mas_dir, "done", "task-1")
    done2 = board.task_dir(mas_dir, "done", "task-2")
    failed1 = board.task_dir(mas_dir, "failed", "task-3")

    done1.mkdir(parents=True)
    done2.mkdir(parents=True)
    failed1.mkdir(parents=True)

    (done1 / "worktree").mkdir()
    (done2 / "worktree").mkdir()
    (failed1 / "worktree").mkdir()

    pruned_calls = []

    def mock_prune(root, worktree_dir, *, keep_branch=True):
        pruned_calls.append(worktree_dir.parent.name)

    with patch("mas.cli.project_dir", return_value=mas_dir), \
         patch("mas.cli.project_root", return_value=tmp_path.parent), \
         patch("mas.cli.worktree.prune", side_effect=mock_prune):
        result = runner.invoke(cli.app, ["prune"])

    assert result.exit_code == 0
    assert "Pruned 3 worktrees" in result.output or "Pruned 3 worktrees from 3 completed tasks" in result.output
    assert len(pruned_calls) == 3
    assert "task-1" in pruned_calls
    assert "task-2" in pruned_calls
    assert "task-3" in pruned_calls


def test_prune_skips_task_without_worktree(monkeypatch, mas_dir: Path, tmp_path: Path):
    done1 = board.task_dir(mas_dir, "done", "task-1")
    done1.mkdir(parents=True)

    pruned_calls = []

    def mock_prune(root, worktree_dir, *, keep_branch=True):
        pruned_calls.append(worktree_dir.name)

    with patch("mas.cli.project_dir", return_value=mas_dir), \
         patch("mas.cli.project_root", return_value=tmp_path.parent), \
         patch("mas.cli.worktree.prune", side_effect=mock_prune):
        result = runner.invoke(cli.app, ["prune"])

    assert result.exit_code == 0
    assert len(pruned_calls) == 0
    assert "Pruned 0 worktrees" in result.output


def test_prune_handles_git_failure(monkeypatch, mas_dir: Path, tmp_path: Path):
    from mas import worktree as wt_module

    done1 = board.task_dir(mas_dir, "done", "task-1")
    done2 = board.task_dir(mas_dir, "done", "task-2")
    done1.mkdir(parents=True)
    done2.mkdir(parents=True)
    (done1 / "worktree").mkdir()
    (done2 / "worktree").mkdir()

    def mock_prune(root, worktree_dir, *, keep_branch=True):
        if worktree_dir.parent.name == "task-1":
            raise RuntimeError("git worktree remove failed")
        worktree_dir.rmdir()

    with patch("mas.cli.project_dir", return_value=mas_dir), \
         patch("mas.cli.project_root", return_value=tmp_path.parent), \
         patch.object(wt_module, "prune", side_effect=mock_prune):
        result = runner.invoke(cli.app, ["prune"])

    assert result.exit_code == 0
    assert "⚠" in result.output or "Failed to prune" in result.output
    assert "task-1" in result.output


def test_prune_empty_board(monkeypatch, mas_dir: Path, tmp_path: Path):
    with patch("mas.cli.project_dir", return_value=mas_dir), \
         patch("mas.cli.project_root", return_value=tmp_path.parent):
        result = runner.invoke(cli.app, ["prune"])

    assert result.exit_code == 0
    assert "Pruned 0 worktrees" in result.output
    assert "0 completed tasks" in result.output