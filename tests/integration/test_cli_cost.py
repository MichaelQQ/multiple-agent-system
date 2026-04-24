"""Integration tests for `mas cost <task-id>` CLI command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from mas import board
from mas.cli import app
from mas.schemas import Plan, Result, SubtaskSpec, Task

runner = CliRunner()


@pytest.fixture(autouse=True)
def _wide_console():
    from mas import cli

    original = cli.console
    cli.console = Console(width=200)
    yield
    cli.console = original


class TestCost:
    def test_cost_shows_per_subtask_breakdown(self, tmp_board, monkeypatch):
        """mas cost <task-id> prints per-subtask token and USD breakdown."""
        monkeypatch.chdir(tmp_board.parent)

        task_id = "20260423-cost-aaaa"
        sub1_id = "20260423-costsub-aaaa"

        parent_dir = tmp_board / "tasks" / "done" / task_id
        parent_dir.mkdir(parents=True)
        board.write_task(parent_dir, Task(id=task_id, role="orchestrator", goal="cost test"))

        plan = Plan(
            parent_id=task_id,
            summary="s",
            subtasks=[SubtaskSpec(id=sub1_id, role="implementer", goal="impl")],
        )
        (parent_dir / "plan.json").write_text(plan.model_dump_json())

        subtasks_dir = parent_dir / "subtasks"
        subtasks_dir.mkdir()
        sub1_dir = subtasks_dir / sub1_id
        sub1_dir.mkdir()

        sub1_result = Result(
            task_id=sub1_id,
            status="success",
            summary="done",
            tokens_in=1000,
            tokens_out=500,
            cost_usd=0.05,
        )
        (sub1_dir / "result.json").write_text(sub1_result.model_dump_json())

        parent_result = Result(
            task_id=task_id,
            status="success",
            summary="done",
            tokens_in=1000,
            tokens_out=500,
            cost_usd=0.05,
        )
        (parent_dir / "result.json").write_text(parent_result.model_dump_json())

        result = runner.invoke(app, ["cost", task_id])
        assert result.exit_code == 0
        assert sub1_id in result.output
        assert "0.05" in result.output
        assert "1000" in result.output

    def test_cost_shows_cumulative_total(self, tmp_board, monkeypatch):
        """mas cost output includes a cumulative total line for tokens and USD."""
        monkeypatch.chdir(tmp_board.parent)

        task_id = "20260423-total-aaaa"
        sub1_id = "20260423-totals1-aaaa"
        sub2_id = "20260423-totals2-aaaa"

        parent_dir = tmp_board / "tasks" / "done" / task_id
        parent_dir.mkdir(parents=True)
        board.write_task(parent_dir, Task(id=task_id, role="orchestrator", goal="total test"))

        plan = Plan(
            parent_id=task_id,
            summary="s",
            subtasks=[
                SubtaskSpec(id=sub1_id, role="implementer", goal="i1"),
                SubtaskSpec(id=sub2_id, role="evaluator", goal="e1"),
            ],
        )
        (parent_dir / "plan.json").write_text(plan.model_dump_json())

        subtasks_dir = parent_dir / "subtasks"
        subtasks_dir.mkdir()

        for sub_id, tin, tout, cost in [
            (sub1_id, 1000, 500, 0.05),
            (sub2_id, 200, 100, 0.01),
        ]:
            sd = subtasks_dir / sub_id
            sd.mkdir()
            r = Result(
                task_id=sub_id,
                status="success",
                summary="done",
                tokens_in=tin,
                tokens_out=tout,
                cost_usd=cost,
            )
            (sd / "result.json").write_text(r.model_dump_json())

        parent_result = Result(
            task_id=task_id,
            status="success",
            summary="done",
            tokens_in=1200,
            tokens_out=600,
            cost_usd=0.06,
        )
        (parent_dir / "result.json").write_text(parent_result.model_dump_json())

        result = runner.invoke(app, ["cost", task_id])
        assert result.exit_code == 0
        output_lower = result.output.lower()
        assert "total" in output_lower or "1200" in result.output
        assert "0.06" in result.output

    def test_cost_unknown_id_exits_nonzero(self, tmp_board, monkeypatch):
        """mas cost <unknown-id> exits non-zero with a clear error message."""
        monkeypatch.chdir(tmp_board.parent)
        result = runner.invoke(app, ["cost", "20260423-notfound-aaaa"])
        assert result.exit_code != 0
        output_lower = result.output.lower()
        assert "not found" in output_lower or "error" in output_lower or "unknown" in output_lower

    def test_cost_task_without_subtasks_shows_own_cost(self, tmp_board, monkeypatch):
        """mas cost on a task with no plan.json still shows the task's own cost."""
        monkeypatch.chdir(tmp_board.parent)

        task_id = "20260423-nopsub-aaaa"
        task_dir = tmp_board / "tasks" / "done" / task_id
        task_dir.mkdir(parents=True)
        board.write_task(task_dir, Task(id=task_id, role="implementer", goal="leaf task"))

        result_obj = Result(
            task_id=task_id,
            status="success",
            summary="done",
            tokens_in=50,
            tokens_out=25,
            cost_usd=0.001,
        )
        (task_dir / "result.json").write_text(result_obj.model_dump_json())

        result = runner.invoke(app, ["cost", task_id])
        assert result.exit_code == 0
        assert "0.001" in result.output or "50" in result.output

    def test_cost_shows_budget_when_set(self, tmp_board, monkeypatch):
        """mas cost shows a budget column (spent/budget + %) when cost_budget_usd is set."""
        monkeypatch.chdir(tmp_board.parent)

        task_id = "20260424-cstbdgt-aaaa"
        sub1_id = "20260424-cstbsub-aaaa"

        parent_dir = tmp_board / "tasks" / "done" / task_id
        parent_dir.mkdir(parents=True)

        # Write task.json manually: cost_budget_usd not in schema yet, but the
        # implementation will add it and read it via board.read_task.
        task_dict = {
            "id": task_id,
            "role": "orchestrator",
            "goal": "budget display test",
            "inputs": {},
            "constraints": {},
            "cost_budget_usd": 1.0,
            "cycle": 0,
            "attempt": 1,
            "created_at": "2026-04-24T00:00:00+00:00",
        }
        (parent_dir / "task.json").write_text(json.dumps(task_dict))

        plan = Plan(
            parent_id=task_id,
            summary="s",
            subtasks=[SubtaskSpec(id=sub1_id, role="implementer", goal="impl")],
        )
        (parent_dir / "plan.json").write_text(plan.model_dump_json())

        subtasks_dir = parent_dir / "subtasks"
        subtasks_dir.mkdir()
        sub1_dir = subtasks_dir / sub1_id
        sub1_dir.mkdir()
        (sub1_dir / "result.json").write_text(
            Result(
                task_id=sub1_id, status="success", summary="done",
                tokens_in=100, tokens_out=50, cost_usd=0.05,
            ).model_dump_json()
        )
        (parent_dir / "result.json").write_text(
            Result(task_id=task_id, status="success", summary="done", cost_usd=0.05).model_dump_json()
        )

        result = runner.invoke(app, ["cost", task_id])
        assert result.exit_code == 0, f"exit_code={result.exit_code}: {result.output}"
        # After implementation: budget column must appear (with spent/budget and % utilization)
        assert "budget" in result.output.lower(), (
            "budget column should appear when cost_budget_usd is set on the task"
        )

    def test_cost_omits_budget_when_not_set(self, tmp_board, monkeypatch):
        """mas cost omits the budget column when cost_budget_usd is not set."""
        monkeypatch.chdir(tmp_board.parent)

        task_id = "20260424-nobdgt2-aaaa"
        sub1_id = "20260424-nobsub2-aaaa"

        parent_dir = tmp_board / "tasks" / "done" / task_id
        parent_dir.mkdir(parents=True)
        board.write_task(parent_dir, Task(id=task_id, role="orchestrator", goal="no budget"))

        plan = Plan(
            parent_id=task_id,
            summary="s",
            subtasks=[SubtaskSpec(id=sub1_id, role="implementer", goal="impl")],
        )
        (parent_dir / "plan.json").write_text(plan.model_dump_json())

        subtasks_dir = parent_dir / "subtasks"
        subtasks_dir.mkdir()
        sub1_dir = subtasks_dir / sub1_id
        sub1_dir.mkdir()
        (sub1_dir / "result.json").write_text(
            Result(task_id=sub1_id, status="success", summary="done", cost_usd=0.05).model_dump_json()
        )
        (parent_dir / "result.json").write_text(
            Result(task_id=task_id, status="success", summary="done", cost_usd=0.05).model_dump_json()
        )

        result = runner.invoke(app, ["cost", task_id])
        assert result.exit_code == 0
        # No budget column when cost_budget_usd is not set
        assert "budget" not in result.output.lower()
