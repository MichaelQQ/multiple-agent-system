"""Integration tests for CLI commands — real temp directories, no external CLIs."""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from mas import board, transitions
from mas.cli import app
from mas.schemas import Task

runner = CliRunner()


@pytest.fixture(autouse=True)
def _wide_console():
    """Ensure Rich doesn't truncate table output in the test runner."""
    from mas import cli

    original = cli.console
    cli.console = Console(width=200)
    yield
    cli.console = original


def _write_task(
    mas_dir: Path,
    column: str,
    task_id: str,
    role: str = "proposer",
    goal: str = "do something",
) -> Path:
    """Helper to create a task in the given column."""
    task = Task(id=task_id, role=role, goal=goal)
    d = mas_dir / "tasks" / column / task_id
    board.write_task(d, task)
    return d


# ── show ──────────────────────────────────────────────────────────


class TestShow:
    def test_empty_board(self, tmp_board, monkeypatch):
        monkeypatch.chdir(tmp_board.parent)
        result = runner.invoke(app, ["show"])
        assert result.exit_code == 0

    def test_shows_task(self, tmp_board, monkeypatch):
        monkeypatch.chdir(tmp_board.parent)
        _write_task(tmp_board, "proposed", "20260416-test-aaaa", goal="build a widget")
        result = runner.invoke(app, ["show"])
        assert result.exit_code == 0
        assert "20260416-test-aaaa" in result.output
        assert "build a widget" in result.output

    def test_shows_transitions(self, tmp_board, monkeypatch):
        monkeypatch.chdir(tmp_board.parent)
        d = _write_task(tmp_board, "proposed", "20260416-txn-bbbb")
        transitions.log_transition(d, "proposed", "doing", "manual_promote")
        result = runner.invoke(app, ["show"])
        assert result.exit_code == 0
        assert "proposed" in result.output
        assert "manual_promote" in result.output

    def test_shows_question_mark_for_bad_task(self, tmp_board, monkeypatch):
        monkeypatch.chdir(tmp_board.parent)
        bad = tmp_board / "tasks" / "doing" / "20260416-bad-cccc"
        bad.mkdir(parents=True)
        (bad / "task.json").write_text("not json")
        result = runner.invoke(app, ["show"])
        assert result.exit_code == 0
        assert "?" in result.output

    def test_multiple_columns(self, tmp_board, monkeypatch):
        monkeypatch.chdir(tmp_board.parent)
        _write_task(tmp_board, "proposed", "20260416-p-1111", goal="propose something")
        _write_task(tmp_board, "doing", "20260416-d-2222", role="implementer", goal="implement it")
        _write_task(tmp_board, "done", "20260416-x-3333", role="evaluator", goal="evaluate it")
        result = runner.invoke(app, ["show"])
        assert result.exit_code == 0
        assert "20260416-p-1111" in result.output
        assert "20260416-d-2222" in result.output
        assert "20260416-x-3333" in result.output

    def test_show_task_tree(self, tmp_board, monkeypatch):
        """`mas show <id>` renders the subtask tree with statuses."""
        from mas.schemas import Plan, Result, SubtaskSpec

        monkeypatch.chdir(tmp_board.parent)
        parent = _write_task(tmp_board, "doing", "20260423-tree-aaaa",
                             role="orchestrator", goal="parent goal")
        plan = Plan(parent_id="20260423-tree-aaaa", summary="s",
                    subtasks=[
                        SubtaskSpec(id="impl-1", role="implementer", goal="g"),
                        SubtaskSpec(id="eval-1", role="evaluator", goal="g"),
                    ])
        (parent / "plan.json").write_text(plan.model_dump_json())
        (parent / "subtasks" / "impl-1").mkdir(parents=True)
        (parent / "subtasks" / "impl-1" / "result.json").write_text(
            Result(task_id="impl-1", status="success", summary="done").model_dump_json()
        )
        (parent / "subtasks" / "eval-1").mkdir(parents=True)

        result = runner.invoke(app, ["show", "20260423-tree-aaaa"])
        assert result.exit_code == 0
        assert "impl-1" in result.output
        assert "eval-1" in result.output
        assert "success" in result.output
        assert "pending" in result.output

    def test_show_task_not_found(self, tmp_board, monkeypatch):
        monkeypatch.chdir(tmp_board.parent)
        result = runner.invoke(app, ["show", "does-not-exist"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_show_board_includes_progress_column(self, tmp_board, monkeypatch):
        """Top-level show table shows a subtask-progress summary for doing/ tasks."""
        from mas.schemas import Plan, Result, SubtaskSpec

        monkeypatch.chdir(tmp_board.parent)
        parent = _write_task(tmp_board, "doing", "20260423-prog-aaaa",
                             role="orchestrator", goal="with progress")
        plan = Plan(parent_id="20260423-prog-aaaa", summary="s",
                    subtasks=[SubtaskSpec(id="a", role="implementer", goal="g")])
        (parent / "plan.json").write_text(plan.model_dump_json())
        (parent / "subtasks" / "a").mkdir(parents=True)
        (parent / "subtasks" / "a" / "result.json").write_text(
            Result(task_id="a", status="success", summary="done").model_dump_json()
        )
        result = runner.invoke(app, ["show"])
        assert result.exit_code == 0
        assert "success" in result.output
        assert "progress" in result.output


# ── init ──────────────────────────────────────────────────────────


class TestInit:
    def test_init_creates_mas_dir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["init", str(tmp_path)])
        assert result.exit_code == 0
        mas = tmp_path / ".mas"
        assert mas.is_dir()
        assert (mas / "ideas.md").exists()
        for col in board.COLUMNS:
            assert (mas / "tasks" / col).is_dir()

    def test_init_refuses_existing(self, tmp_board, monkeypatch):
        monkeypatch.chdir(tmp_board.parent)
        result = runner.invoke(app, ["init", str(tmp_board.parent)])
        assert result.exit_code == 1
        assert "already exists" in result.output

    def test_init_force_reinitializes(self, tmp_board, monkeypatch):
        monkeypatch.chdir(tmp_board.parent)
        marker = tmp_board / "custom_file"
        marker.write_text("hi")
        result = runner.invoke(app, ["init", str(tmp_board.parent), "--force"])
        assert result.exit_code == 0
        assert not marker.exists()


# ── promote ───────────────────────────────────────────────────────


class TestPromote:
    def test_promote_moves_to_doing(self, tmp_board, monkeypatch):
        monkeypatch.chdir(tmp_board.parent)
        _write_task(tmp_board, "proposed", "20260416-pro-dddd")
        result = runner.invoke(app, ["promote", "20260416-pro-dddd"])
        assert result.exit_code == 0
        assert (tmp_board / "tasks" / "doing" / "20260416-pro-dddd").is_dir()
        assert not (tmp_board / "tasks" / "proposed" / "20260416-pro-dddd").exists()

    def test_promote_not_found(self, tmp_board, monkeypatch):
        monkeypatch.chdir(tmp_board.parent)
        result = runner.invoke(app, ["promote", "nonexistent"])
        assert result.exit_code == 1
        assert "not found" in result.output


# ── retry ─────────────────────────────────────────────────────────


class TestRetry:
    def test_retry_moves_to_doing_and_resets(self, tmp_board, monkeypatch):
        monkeypatch.chdir(tmp_board.parent)
        d = _write_task(tmp_board, "failed", "20260416-fail-eeee")
        (d / "result.json").write_text('{"task_id":"x","status":"failure","summary":"boom"}')
        (d / "plan.json").write_text("{}")

        result = runner.invoke(app, ["retry", "20260416-fail-eeee"])
        assert result.exit_code == 0

        new_dir = tmp_board / "tasks" / "doing" / "20260416-fail-eeee"
        assert new_dir.is_dir()
        assert not (new_dir / "result.json").exists()
        assert not (new_dir / "plan.json").exists()

    def test_retry_not_found(self, tmp_board, monkeypatch):
        monkeypatch.chdir(tmp_board.parent)
        result = runner.invoke(app, ["retry", "nonexistent"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_retry_resets_subtasks(self, tmp_board, monkeypatch):
        monkeypatch.chdir(tmp_board.parent)
        d = _write_task(tmp_board, "failed", "20260416-sub-ffff")
        sub = d / "subtasks" / "sub1"
        sub.mkdir(parents=True)
        (sub / "result.json").write_text("{}")
        (sub / ".previous_failure").write_text("old failure")
        (sub / ".attempt").write_text("3")

        runner.invoke(app, ["retry", "20260416-sub-ffff"])

        new_sub = tmp_board / "tasks" / "doing" / "20260416-sub-ffff" / "subtasks" / "sub1"
        assert not (new_sub / "result.json").exists()
        assert not (new_sub / ".previous_failure").exists()
        assert (new_sub / ".attempt").read_text() == "1"


# ── logs ──────────────────────────────────────────────────────────


class TestLogs:
    def test_logs_prints_latest(self, tmp_board, monkeypatch):
        monkeypatch.chdir(tmp_board.parent)
        d = _write_task(tmp_board, "doing", "20260416-log-aaaa")
        log_dir = d / "logs"
        log_dir.mkdir()
        (log_dir / "implementer.log").write_text("line1\nline2\n")

        result = runner.invoke(app, ["logs", "20260416-log-aaaa"])
        assert result.exit_code == 0
        assert "line1" in result.output

    def test_logs_not_found(self, tmp_board, monkeypatch):
        monkeypatch.chdir(tmp_board.parent)
        result = runner.invoke(app, ["logs", "nonexistent"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_logs_no_logs(self, tmp_board, monkeypatch):
        monkeypatch.chdir(tmp_board.parent)
        _write_task(tmp_board, "doing", "20260416-nolog-bbbb")
        result = runner.invoke(app, ["logs", "20260416-nolog-bbbb"])
        assert result.exit_code == 0
        assert "no logs" in result.output
