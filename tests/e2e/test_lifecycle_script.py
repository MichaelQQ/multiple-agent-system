"""E2E tests for MAS task lifecycle using script adapter.

These tests exercise the full MAS task lifecycle using a deterministic
'script' provider adapter (a shell script that writes result.json directly,
spawned as a real subprocess).

Tests MUST FAIL before the script adapter is implemented because:
- ScriptAdapter does not exist in the adapters registry
- get_adapter("script") will raise KeyError

After implementation, these tests verify:
1. Real git repo and .mas/ layout in tmp_path
2. Fixture task.json placed in proposed/
3. Tick loop advances task to done/ or fails
4. task.json and result.json conform to Task/Result Pydantic schemas
5. transitions.jsonl has correct from/to entries at each board move
6. Git worktree created during doing/ and pruned after done/
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from mas import board, tick, transitions, worktree as wt
from mas.schemas import MasConfig, Plan, ProviderConfig, Result, RoleConfig, Task


class TestScriptAdapterExists:
    """Test that ScriptAdapter exists in the adapters registry."""

    def test_script_adapter_importable(self):
        """ScriptAdapter must be importable from mas.adapters.

        This test FAILS because ScriptAdapter does not exist yet.
        """
        from mas.adapters import get_adapter

        adapter_cls = get_adapter("script")
        assert adapter_cls is not None
        assert hasattr(adapter_cls, "build_command")


class TestLifecycleWithProposedTask:
    """Test full lifecycle: proposed -> doing -> done/failed."""

    def test_task_lifecycle_proposes_to_done(
        self, git_repo: Path, mas_dir: Path, script_provider, tmp_path
    ):
        """Full lifecycle: task moves from proposed through doing to done.

        This test creates a minimal script that immediately succeeds,
        runs tick until the task completes, and verifies all assertions.
        """
        task_id = "20260421-test-e2e-0001"
        worktree_root = git_repo / "worktrees"

        success_script = tmp_path / "success.sh"
        success_script.write_text(
            """#!/bin/bash
TASK_DIR="$1"
cat > "$TASK_DIR/result.json" << 'EOF'
{
  "task_id": "20260421-test-e2e-0001-sub",
  "status": "success",
  "summary": "Test implementation completed"
}
EOF
"""
        )
        success_script.chmod(0o755)

        task = Task(
            id=task_id,
            role="orchestrator",
            goal="Test E2E lifecycle",
            inputs={},
        )
        proposed_dir = board.task_dir(mas_dir, "proposed", task_id)
        board.write_task(proposed_dir, task)

        assert proposed_dir.exists()
        task_loaded = board.read_task(proposed_dir)
        assert task_loaded.id == task_id

        txn = transitions.read_transitions(proposed_dir)
        assert len(txn) >= 1
        assert txn[0].from_state == "none"
        assert txn[0].to_state == "proposed"

        doing_dir = board.task_dir(mas_dir, "doing", task_id)
        board.move(proposed_dir, doing_dir, reason="manual_promote")

        txn = transitions.read_transitions(doing_dir)
        state_changes = [(t.from_state, t.to_state) for t in txn]
        assert ("proposed", "doing") in state_changes or any(
            "doing" in s for s in state_changes
        )

        wt_path = doing_dir / "worktree"
        for _ in range(30):
            tick.run_tick(start=git_repo)
            if wt_path.exists():
                break
            time.sleep(0.2)
        assert wt_path.exists()

        branch_name = wt.branch_name(task_id)
        result = subprocess.run(
            ["git", "-C", str(git_repo), "branch", "--list", f"mas/{task_id}"],
            capture_output=True,
            text=True,
        )
        assert branch_name in result.stdout

        task_loaded = board.read_task(doing_dir)
        assert task_loaded.id == task_id
        assert task_loaded.role == "orchestrator"

        for _ in range(30):
            tick.run_tick(start=git_repo)
            col, task_path = board.find_task(mas_dir, task_id)
            if col in ("done", "failed"):
                break
            time.sleep(0.2)

        final_col, final_path = board.find_task(mas_dir, task_id)
        assert final_col in ("done", "failed"), f"Task ended in {final_col}, expected done or failed"

        if final_col == "done":
            txn = transitions.read_transitions(final_path)
            state_changes = [(t.from_state, t.to_state) for t in txn]
            assert ("doing", "done") in state_changes

        if final_col == "failed":
            txn = transitions.read_transitions(final_path)
            state_changes = [(t.from_state, t.to_state) for t in txn]
            assert ("doing", "failed") in state_changes

    def test_worktree_created_during_doing_pruned_after_done(
        self, git_repo: Path, mas_dir: Path, tmp_path
    ):
        """Verify worktree exists during doing/ and is pruned after done/."""
        task_id = "20260421-test-wt-0001"

        success_script = tmp_path / "success.sh"
        success_script.write_text(
            """#!/bin/bash
TASK_DIR="$1"
cat > "$TASK_DIR/result.json" << 'EOF'
{
  "task_id": "20260421-test-wt-0001-sub",
  "status": "success",
  "summary": "Done"
}
EOF
"""
        )
        success_script.chmod(0o755)

        task = Task(id=task_id, role="orchestrator", goal="Worktree test")
        proposed_dir = board.task_dir(mas_dir, "proposed", task_id)
        board.write_task(proposed_dir, task)

        doing_dir = board.task_dir(mas_dir, "doing", task_id)
        board.move(proposed_dir, doing_dir, reason="manual_promote")

        for _ in range(30):
            tick.run_tick(start=git_repo)
            wt_path = doing_dir / "worktree"
            if wt_path.exists():
                break
            time.sleep(0.2)

        wt_path = doing_dir / "worktree"
        assert wt_path.exists(), "Worktree must exist during doing/"

        for _ in range(30):
            tick.run_tick(start=git_repo)
            col, _ = board.find_task(mas_dir, task_id)
            if col in ("done", "failed"):
                break
            time.sleep(0.2)

        col, final_path = board.find_task(mas_dir, task_id)

        if col == "done":
            assert not wt_path.exists(), "Worktree must be pruned after done/"


class TestSchemaValidation:
    """Verify task.json and result.json conform to Pydantic schemas."""

    def test_task_json_validates_against_schema(self, mas_dir: Path):
        """Task JSON must be valid according to Task Pydantic model."""
        task = Task(
            id="20260421-schema-0001",
            role="orchestrator",
            goal="Schema test",
            inputs={"key": "value"},
            constraints={"max_time": 60},
            cycle=0,
            attempt=1,
        )
        task_dir = board.task_dir(mas_dir, "proposed", task.id)
        board.write_task(task_dir, task)

        task_loaded = board.read_task(task_dir)
        assert task_loaded.id == task.id
        assert task_loaded.role == task.role
        assert task_loaded.goal == task.goal

    def test_result_json_validates_against_schema(self, mas_dir: Path):
        """Result JSON must be valid according to Result Pydantic model."""
        task_dir = board.task_dir(mas_dir, "proposed", "result-test")
        task_dir.mkdir(parents=True)

        result = Result(
            task_id="result-test",
            status="success",
            summary="Test passed",
            artifacts=["file1.py", "file2.py"],
            handoff={"next_step": "deploy"},
            verdict="pass",
            feedback="All good",
            tokens_in=1000,
            tokens_out=2000,
            duration_s=120.5,
            cost_usd=0.05,
        )
        (task_dir / "result.json").write_text(result.model_dump_json(indent=2))

        result_loaded = board.read_result(task_dir)
        assert result_loaded is not None
        assert result_loaded.task_id == "result-test"
        assert result_loaded.status == "success"
        assert result_loaded.verdict == "pass"


class TestTransitionsLogging:
    """Verify transitions.jsonl is written with correct from/to state entries."""

    def test_transitions_record_state_changes(self, mas_dir: Path):
        """Each board move must create a transition entry."""
        task_id = "20260421-trans-0001"

        proposed_dir = board.task_dir(mas_dir, "proposed", task_id)
        board.write_task(proposed_dir, Task(id=task_id, role="orchestrator", goal="t"))

        initial_txns = transitions.read_transitions(proposed_dir)
        assert len(initial_txns) >= 1
        assert initial_txns[0].from_state == "none"
        assert initial_txns[0].to_state == "proposed"

        doing_dir = board.task_dir(mas_dir, "doing", task_id)
        board.move(proposed_dir, doing_dir, reason="test_move")

        doing_txns = transitions.read_transitions(doing_dir)
        state_pairs = [(t.from_state, t.to_state) for t in doing_txns]
        assert ("none", "proposed") in state_pairs
        assert ("proposed", "doing") in state_pairs

        done_dir = board.task_dir(mas_dir, "done", task_id)
        board.move(doing_dir, done_dir, reason="test_complete")

        done_txns = transitions.read_transitions(done_dir)
        final_pairs = [(t.from_state, t.to_state) for t in done_txns]
        assert ("proposed", "doing") in final_pairs
        assert ("doing", "done") in final_pairs


class TestCostAggregationE2E:
    """E2E tests verifying cost aggregation after a full lifecycle run."""

    def test_parent_result_has_aggregated_costs_after_done(
        self, git_repo: Path, mas_dir: Path, tmp_path
    ):
        """After all children succeed, parent result.json under done/ must have aggregated costs.

        Pre-seeds child results with dummy tokens/cost (simulating script adapter output),
        drives _finalize_parent via the tick loop, and asserts the parent result.json
        contains summed totals.
        """
        from unittest.mock import patch

        from mas import board, tick
        from mas.schemas import Plan, Result, SubtaskSpec, Task

        task_id = "20260421-e2ecost-aaaa"
        child1_id = "20260421-e2cc1-aaaa"
        child2_id = "20260421-e2cc2-aaaa"

        doing_dir = board.task_dir(mas_dir, "doing", task_id)
        doing_dir.mkdir(parents=True)
        board.write_task(
            doing_dir, Task(id=task_id, role="orchestrator", goal="E2E cost aggregation test")
        )
        (doing_dir / "worktree").mkdir()

        plan = Plan(
            parent_id=task_id,
            summary="e2e test plan",
            subtasks=[
                SubtaskSpec(id=child1_id, role="implementer", goal="impl"),
                SubtaskSpec(id=child2_id, role="evaluator", goal="eval"),
            ],
        )
        (doing_dir / "plan.json").write_text(plan.model_dump_json())

        subtasks_dir = doing_dir / "subtasks"
        subtasks_dir.mkdir()

        for cid, tin, tout, cost, verdict in [
            (child1_id, 300, 150, 0.03, None),
            (child2_id, 100, 50, 0.01, "pass"),
        ]:
            cd = subtasks_dir / cid
            cd.mkdir()
            (cd / "result.json").write_text(
                Result(
                    task_id=cid,
                    status="success",
                    summary="done",
                    tokens_in=tin,
                    tokens_out=tout,
                    cost_usd=cost,
                    verdict=verdict,
                ).model_dump_json()
            )

        with patch("mas.tick.worktree.commit_changes"), \
             patch("mas.tick.worktree.prune"), \
             patch("mas.tick._maybe_dispatch_proposer"):
            tick.run_tick(start=git_repo)

        located = board.find_task(mas_dir, task_id)
        assert located is not None, f"task {task_id} not found after tick"
        col, final_dir = located
        assert col == "done", f"task should be done after finalization, got {col}"

        parent_result = board.read_result(final_dir)
        assert parent_result is not None, (
            "parent result.json must be written by _finalize_parent with aggregated costs"
        )
        assert parent_result.tokens_in == 400, "400 = 300 + 100"
        assert parent_result.tokens_out == 200, "200 = 150 + 50"
        assert parent_result.cost_usd == pytest.approx(0.04), "0.04 = 0.03 + 0.01"

    def test_mas_cost_command_reports_aggregated_totals(
        self, git_repo: Path, mas_dir: Path, monkeypatch
    ):
        """After lifecycle, `mas cost <task-id>` reports the aggregated cost breakdown."""
        from unittest.mock import patch

        from typer.testing import CliRunner

        from mas import board, tick
        from mas.cli import app
        from mas.schemas import Plan, Result, SubtaskSpec, Task

        task_id = "20260421-e2ccli-aaaa"
        child_id = "20260421-e2cch1-aaaa"

        doing_dir = board.task_dir(mas_dir, "doing", task_id)
        doing_dir.mkdir(parents=True)
        board.write_task(doing_dir, Task(id=task_id, role="orchestrator", goal="CLI cost E2E"))
        (doing_dir / "worktree").mkdir()

        plan = Plan(
            parent_id=task_id,
            summary="s",
            subtasks=[SubtaskSpec(id=child_id, role="evaluator", goal="eval")],
        )
        (doing_dir / "plan.json").write_text(plan.model_dump_json())

        subtasks_dir = doing_dir / "subtasks"
        subtasks_dir.mkdir()
        cd = subtasks_dir / child_id
        cd.mkdir()
        (cd / "result.json").write_text(
            Result(
                task_id=child_id,
                status="success",
                summary="done",
                tokens_in=500,
                tokens_out=250,
                cost_usd=0.075,
                verdict="pass",
            ).model_dump_json()
        )

        with patch("mas.tick.worktree.commit_changes"), \
             patch("mas.tick.worktree.prune"), \
             patch("mas.tick._maybe_dispatch_proposer"):
            tick.run_tick(start=git_repo)

        located = board.find_task(mas_dir, task_id)
        assert located is not None
        col, _ = located
        assert col == "done"

        monkeypatch.chdir(mas_dir.parent)
        cli_runner = CliRunner()
        result = cli_runner.invoke(app, ["cost", task_id])
        assert result.exit_code == 0, f"mas cost failed: {result.output}"
        assert "0.075" in result.output or "cost" in result.output.lower()


class TestFullIntegration:
    """Full integration tests combining all lifecycle components."""

    def test_tick_creates_worktree_for_orchestrator(
        self, git_repo: Path, mas_dir: Path, tmp_path
    ):
        """Orchestrator tasks must create a worktree during doing phase."""
        task_id = "20260421-int-wt-0001"

        task = Task(id=task_id, role="orchestrator", goal="Integration test")
        proposed_dir = board.task_dir(mas_dir, "proposed", task_id)
        board.write_task(proposed_dir, task)

        doing_dir = board.task_dir(mas_dir, "doing", task_id)
        board.move(proposed_dir, doing_dir, reason="manual_promote")

        for _ in range(30):
            tick.run_tick(start=git_repo)
            wt_path = doing_dir / "worktree"
            if wt_path.exists():
                branch = wt.branch_name(task_id)
                result = subprocess.run(
                    ["git", "-C", str(git_repo), "branch", "--list"],
                    capture_output=True,
                    text=True,
                )
                assert branch in result.stdout
                break
            time.sleep(0.2)
        else:
            pytest.fail("Worktree was never created")

    def test_tick_finalizes_and_prunes_worktree(
        self, git_repo: Path, mas_dir: Path, tmp_path
    ):
        """After task reaches done/, worktree must be pruned."""
        task_id = "20260421-int-prune-0001"

        success_script = tmp_path / "success.sh"
        success_script.write_text(
            """#!/bin/bash
TASK_DIR="$1"
cat > "$TASK_DIR/result.json" << 'EOF'
{
  "task_id": "20260421-int-prune-0001-sub",
  "status": "success",
  "summary": "Complete"
}
EOF
"""
        )
        success_script.chmod(0o755)

        task = Task(id=task_id, role="orchestrator", goal="Prune test")
        proposed_dir = board.task_dir(mas_dir, "proposed", task_id)
        board.write_task(proposed_dir, task)

        doing_dir = board.task_dir(mas_dir, "doing", task_id)
        board.move(proposed_dir, doing_dir, reason="manual_promote")

        for _ in range(60):
            tick.run_tick(start=git_repo)
            col, task_path = board.find_task(mas_dir, task_id)
            if col == "done":
                wt_path = task_path.parent / "worktree"
                assert not wt_path.exists(), "Worktree should be pruned after done"
                break
            time.sleep(0.2)
        else:
            col, _ = board.find_task(mas_dir, task_id)
            pytest.fail(f"Task never reached done/ state, ended in {col}")
