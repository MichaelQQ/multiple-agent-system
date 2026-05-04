"""Tests for the `mas verify` CLI auditor and underlying audit helper.

Covers TODO #11 from docs/reliability-gaps.md: re-run the recorded
test_command and compare against handoff.final_exit_code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from mas import board
from mas.cli import app
from mas.schemas import Plan, Result, SubtaskSpec, Task
from mas.verify import audit_task_test_command

runner = CliRunner()


@pytest.fixture(autouse=True)
def _wide_console():
    from mas import cli

    original = cli.console
    cli.console = Console(width=200)
    yield
    cli.console = original


def _write_parent(mas_dir: Path, task_id: str, column: str = "done") -> Path:
    task = Task(id=task_id, role="orchestrator", goal="parent goal")
    d = mas_dir / "tasks" / column / task_id
    board.write_task(d, task)
    return d


def _write_plan(parent_dir: Path, subtasks: list[SubtaskSpec]) -> None:
    plan = Plan(parent_id=parent_dir.name, summary="s", subtasks=subtasks)
    (parent_dir / "plan.json").write_text(plan.model_dump_json(indent=2))


def _write_subtask_result(parent_dir: Path, spec_id: str, result: Result) -> Path:
    d = parent_dir / "subtasks" / spec_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "result.json").write_text(result.model_dump_json(indent=2))
    return d


def _make_worktree(parent_dir: Path) -> Path:
    wt = parent_dir / "worktree"
    wt.mkdir(exist_ok=True)
    return wt


# --- audit_task_test_command (helper) ---------------------------------------


def test_audit_no_plan_returns_empty(tmp_path: Path):
    parent_dir = tmp_path / "parent"
    parent_dir.mkdir()
    assert audit_task_test_command(parent_dir) == []


def test_audit_match_when_command_returns_claimed_zero(tmp_board: Path):
    parent = _write_parent(tmp_board, "20260504-verify-aaaa")
    _write_plan(parent, [
        SubtaskSpec(id="impl-1", role="implementer", goal="i"),
    ])
    _write_subtask_result(parent, "impl-1", Result(
        task_id="x", status="success", summary="impl",
        handoff={"final_exit_code": 0, "test_command": "true"},
    ))
    _make_worktree(parent)

    records = audit_task_test_command(parent)
    assert len(records) == 1
    r = records[0]
    assert r["status"] == "match"
    assert r["claimed_exit_code"] == 0
    assert r["actual_exit_code"] == 0


def test_audit_mismatch_when_actual_differs(tmp_board: Path):
    parent = _write_parent(tmp_board, "20260504-verify-bbbb")
    _write_plan(parent, [
        SubtaskSpec(id="impl-1", role="implementer", goal="i"),
    ])
    _write_subtask_result(parent, "impl-1", Result(
        task_id="x", status="success", summary="impl",
        handoff={"final_exit_code": 0, "test_command": "false"},
    ))
    _make_worktree(parent)

    records = audit_task_test_command(parent)
    assert len(records) == 1
    r = records[0]
    assert r["status"] == "mismatch"
    assert r["claimed_exit_code"] == 0
    assert r["actual_exit_code"] == 1
    assert "claimed 0" in r["detail"]


def test_audit_falls_back_to_tester_test_command(tmp_board: Path):
    """Implementer omits test_command; helper walks back to most recent tester."""
    parent = _write_parent(tmp_board, "20260504-verify-cccc")
    _write_plan(parent, [
        SubtaskSpec(id="tester-1", role="tester", goal="t"),
        SubtaskSpec(id="impl-1", role="implementer", goal="i"),
    ])
    _write_subtask_result(parent, "tester-1", Result(
        task_id="x", status="success", summary="t",
        handoff={"test_command": "true", "initial_exit_code": 1},
    ))
    _write_subtask_result(parent, "impl-1", Result(
        task_id="x", status="success", summary="i",
        handoff={"final_exit_code": 0},  # no test_command here
    ))
    _make_worktree(parent)

    records = audit_task_test_command(parent)
    assert records[0]["status"] == "match"
    assert records[0]["test_command"] == "true"


def test_audit_skipped_when_no_handoff_exit_code(tmp_board: Path):
    parent = _write_parent(tmp_board, "20260504-verify-dddd")
    _write_plan(parent, [
        SubtaskSpec(id="impl-1", role="implementer", goal="i"),
    ])
    _write_subtask_result(parent, "impl-1", Result(
        task_id="x", status="success", summary="impl",
        handoff={"test_command": "true"},  # no final_exit_code
    ))
    _make_worktree(parent)

    records = audit_task_test_command(parent)
    assert records[0]["status"] == "skipped"
    assert "final_exit_code" in records[0]["detail"]


def test_audit_skipped_when_no_test_command_anywhere(tmp_board: Path):
    parent = _write_parent(tmp_board, "20260504-verify-eeee")
    _write_plan(parent, [
        SubtaskSpec(id="impl-1", role="implementer", goal="i"),
    ])
    _write_subtask_result(parent, "impl-1", Result(
        task_id="x", status="success", summary="impl",
        handoff={"final_exit_code": 0},
    ))
    _make_worktree(parent)

    records = audit_task_test_command(parent)
    assert records[0]["status"] == "skipped"
    assert "test_command" in records[0]["detail"]


def test_audit_error_when_worktree_missing(tmp_board: Path):
    parent = _write_parent(tmp_board, "20260504-verify-ffff")
    _write_plan(parent, [
        SubtaskSpec(id="impl-1", role="implementer", goal="i"),
    ])
    _write_subtask_result(parent, "impl-1", Result(
        task_id="x", status="success", summary="impl",
        handoff={"final_exit_code": 0, "test_command": "true"},
    ))
    # no worktree created — simulates pruned task

    records = audit_task_test_command(parent)
    assert records[0]["status"] == "error"
    assert "worktree" in records[0]["detail"]


def test_audit_skips_non_implementer_subtasks(tmp_board: Path):
    parent = _write_parent(tmp_board, "20260504-verify-0007")
    _write_plan(parent, [
        SubtaskSpec(id="tester-1", role="tester", goal="t"),
        SubtaskSpec(id="evaluator-1", role="evaluator", goal="e"),
    ])
    _write_subtask_result(parent, "tester-1", Result(
        task_id="x", status="success", summary="t",
        handoff={"test_command": "true", "initial_exit_code": 1},
    ))
    _make_worktree(parent)

    records = audit_task_test_command(parent)
    assert records == []


def test_audit_audits_each_revision_cycle(tmp_board: Path):
    parent = _write_parent(tmp_board, "20260504-verify-0008")
    _write_plan(parent, [
        SubtaskSpec(id="impl-1", role="implementer", goal="i"),
        SubtaskSpec(id="rev-1-tester", role="tester", goal="t"),
        SubtaskSpec(id="rev-1-implementer", role="implementer", goal="ri"),
    ])
    _write_subtask_result(parent, "impl-1", Result(
        task_id="x", status="success", summary="i1",
        handoff={"final_exit_code": 0, "test_command": "true"},
    ))
    _write_subtask_result(parent, "rev-1-tester", Result(
        task_id="x", status="success", summary="rt",
        handoff={"test_command": "false", "initial_exit_code": 1},
    ))
    _write_subtask_result(parent, "rev-1-implementer", Result(
        task_id="x", status="success", summary="ri",
        handoff={"final_exit_code": 0},  # falls back to rev-1-tester's "false"
    ))
    _make_worktree(parent)

    records = audit_task_test_command(parent)
    assert [r["subtask_id"] for r in records] == ["impl-1", "rev-1-implementer"]
    assert records[0]["status"] == "match"
    assert records[1]["status"] == "mismatch"


# --- CLI ---------------------------------------------------------------------


class TestVerifyCommand:
    def test_command_registered(self, tmp_board, monkeypatch):
        monkeypatch.chdir(tmp_board.parent)
        result = runner.invoke(app, ["verify", "--help"])
        assert result.exit_code == 0

    def test_unknown_task_exits_nonzero(self, tmp_board, monkeypatch):
        monkeypatch.chdir(tmp_board.parent)
        result = runner.invoke(app, ["verify", "20260504-noexist-zzzz"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_match_exits_zero(self, tmp_board, monkeypatch):
        monkeypatch.chdir(tmp_board.parent)
        parent = _write_parent(tmp_board, "20260504-verify-0009")
        _write_plan(parent, [
            SubtaskSpec(id="impl-1", role="implementer", goal="i"),
        ])
        _write_subtask_result(parent, "impl-1", Result(
            task_id="x", status="success", summary="impl",
            handoff={"final_exit_code": 0, "test_command": "true"},
        ))
        _make_worktree(parent)

        result = runner.invoke(app, ["verify", "20260504-verify-0009"])
        assert result.exit_code == 0, result.output
        assert "match" in result.output

    def test_mismatch_exits_nonzero(self, tmp_board, monkeypatch):
        monkeypatch.chdir(tmp_board.parent)
        parent = _write_parent(tmp_board, "20260504-verify-000a")
        _write_plan(parent, [
            SubtaskSpec(id="impl-1", role="implementer", goal="i"),
        ])
        _write_subtask_result(parent, "impl-1", Result(
            task_id="x", status="success", summary="impl",
            handoff={"final_exit_code": 0, "test_command": "false"},
        ))
        _make_worktree(parent)

        result = runner.invoke(app, ["verify", "20260504-verify-000a"])
        assert result.exit_code == 1, result.output
        assert "mismatch" in result.output

    def test_json_output(self, tmp_board, monkeypatch):
        monkeypatch.chdir(tmp_board.parent)
        parent = _write_parent(tmp_board, "20260504-verify-000b")
        _write_plan(parent, [
            SubtaskSpec(id="impl-1", role="implementer", goal="i"),
        ])
        _write_subtask_result(parent, "impl-1", Result(
            task_id="x", status="success", summary="impl",
            handoff={"final_exit_code": 0, "test_command": "true"},
        ))
        _make_worktree(parent)

        result = runner.invoke(app, ["verify", "20260504-verify-000b", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["task_id"] == "20260504-verify-000b"
        assert len(data["records"]) == 1
        assert data["records"][0]["status"] == "match"

    def test_no_implementer_outputs_empty_message(self, tmp_board, monkeypatch):
        monkeypatch.chdir(tmp_board.parent)
        parent = _write_parent(tmp_board, "20260504-verify-000c")
        _write_plan(parent, [
            SubtaskSpec(id="tester-1", role="tester", goal="t"),
        ])
        _make_worktree(parent)

        result = runner.invoke(app, ["verify", "20260504-verify-000c"])
        assert result.exit_code == 0, result.output
        assert "no implementer" in result.output
