"""Regression tests for orphaned-worker detection.

A worker can exit without writing result.json (crash, usage-limit, OOM).
Before the fix, the tick loop redispatched indefinitely since the retry
path only fires on an existing result. These tests verify the orphan is
turned into a synthesized failure and walked through retry→fail-parent."""

from pathlib import Path

import pytest

from mas import board
from mas.schemas import (
    MasConfig,
    Plan,
    ProviderConfig,
    RoleConfig,
    SubtaskSpec,
    Task,
)
from mas.tick import TickEnv, _advance_one


def _cfg(max_retries: int = 2) -> MasConfig:
    return MasConfig(
        providers={"mock": ProviderConfig(cli="sh", max_concurrent=1, extra_args=[])},
        roles={
            "proposer": RoleConfig(provider="mock", max_retries=max_retries),
            "orchestrator": RoleConfig(provider="mock", max_retries=max_retries),
            "implementer": RoleConfig(provider="mock", max_retries=max_retries),
            "tester": RoleConfig(provider="mock", max_retries=max_retries),
            "evaluator": RoleConfig(provider="mock", max_retries=max_retries),
        },
    )


def _seed_parent_with_plan(mas: Path, parent_id: str, child_id: str) -> Path:
    parent = board.task_dir(mas, "doing", parent_id)
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="g"))
    (parent / "worktree").mkdir()
    plan = Plan(
        parent_id=parent_id,
        summary="s",
        subtasks=[SubtaskSpec(id=child_id, role="implementer", goal="do")],
    )
    (parent / "plan.json").write_text(plan.model_dump_json())
    return parent


def test_orphan_child_synthesizes_failure_and_retries(tmp_path: Path):
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = _seed_parent_with_plan(mas, "p1", "impl-1")
    child = parent / "subtasks" / "impl-1"
    child.mkdir(parents=True)
    # Simulate a previous dispatch that died: log exists, no live pid, no result.
    (child / "logs").mkdir()
    (child / "logs" / "implementer-1.log").write_text("ERROR: usage limit\n")

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(max_retries=2))
    _advance_one(env, parent)

    # Retry path rotated the synthesized failure and bumped .attempt.
    assert not (child / "result.json").exists()
    assert (child / "result.failed-1.json").exists()
    assert (child / ".attempt").read_text().strip() == "2"
    assert (child / ".previous_failure").exists()
    # Parent still in doing/ — retries remain.
    assert parent.exists()


def test_orphan_child_fails_parent_after_retries(tmp_path: Path):
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = _seed_parent_with_plan(mas, "p2", "impl-1")
    child = parent / "subtasks" / "impl-1"
    child.mkdir(parents=True)
    (child / "logs").mkdir()
    # Already at final attempt (max_retries=2 → limit=3).
    (child / ".attempt").write_text("3")
    (child / "logs" / "implementer-3.log").write_text("ERROR: still failing\n")

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(max_retries=2))
    _advance_one(env, parent)

    assert not parent.exists()
    assert (mas / "tasks" / "failed" / "p2").exists()


def test_no_orphan_when_log_missing(tmp_path: Path):
    """Fresh subtask (never dispatched) must not trigger orphan synthesis."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = _seed_parent_with_plan(mas, "p3", "impl-1")
    child = parent / "subtasks" / "impl-1"
    child.mkdir(parents=True)

    # Use mock provider pointing at a fixture result so dispatch can run end-to-end.
    fixture = tmp_path / "fx.json"
    fixture.write_text('{"task_id":"impl-1","status":"success","summary":"ok","duration_s":0}')
    cfg = _cfg()
    cfg.providers["mock"] = ProviderConfig(cli="sh", max_concurrent=1, extra_args=[str(fixture)])
    (mas / "prompts").mkdir(exist_ok=True)
    (mas / "prompts" / "implementer.md").write_text("goal=$goal")

    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)
    _advance_one(env, parent)

    # No orphan synthesis; a real dispatch happened and task.json was written.
    assert (child / "task.json").exists()


def test_orphan_proposer_moves_to_failed(tmp_path: Path):
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "prop-1")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="prop-1", role="proposer", goal="propose"))
    (parent / "logs").mkdir()
    (parent / "logs" / "proposer-1.log").write_text("crash\n")

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())
    _advance_one(env, parent)

    assert not parent.exists()
    assert (mas / "tasks" / "failed" / "prop-1").exists()


def test_orphan_orchestrator_retries_then_fails(tmp_path: Path):
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "p4")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="p4", role="orchestrator", goal="g"))
    (parent / "worktree").mkdir()
    (parent / "logs").mkdir()
    (parent / "logs" / "orchestrator-1.log").write_text("crash\n")

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(max_retries=1))
    _advance_one(env, parent)
    # First orphan → bump attempt, keep in doing/.
    assert parent.exists()
    assert (parent / ".orchestrator_attempt").read_text().strip() == "2"
    assert (parent / ".previous_failure").exists()

    # Simulate second attempt that also orphaned.
    (parent / "logs" / "orchestrator-2.log").write_text("crash again\n")
    _advance_one(env, parent)
    # max_retries=1 → limit=2. attempt=2 NOT < 2 → move to failed.
    assert not parent.exists()
    assert (mas / "tasks" / "failed" / "p4").exists()
