from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from mas import board
from mas.schemas import MasConfig, Plan, ProviderConfig, Result, RoleConfig, SubtaskSpec, Task


def test_finalize_parent_writes_result_with_aggregated_cost(tmp_path):
    """_finalize_parent must write result.json containing cost_usd aggregated from children.

    Simulates the 'dummy-cost path': a script writes cost_usd directly, and after
    _finalize_parent the parent result.json must reflect that cost.
    """
    from mas.tick import TickEnv, _finalize_parent

    mas = tmp_path / ".mas"
    board.ensure_layout(mas)

    cfg = MasConfig(
        providers={"mock": ProviderConfig(cli="sh", max_concurrent=1, extra_args=[])},
        roles={
            "proposer": RoleConfig(provider="mock", max_retries=1),
            "orchestrator": RoleConfig(provider="mock", max_retries=1),
            "implementer": RoleConfig(provider="mock", max_retries=1),
            "tester": RoleConfig(provider="mock", max_retries=1),
            "evaluator": RoleConfig(provider="mock", max_retries=1),
        },
    )
    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)

    parent_id = "20260423-script-aaaa"
    parent_dir = board.task_dir(mas, "doing", parent_id)
    parent_dir.mkdir(parents=True)
    board.write_task(parent_dir, Task(id=parent_id, role="orchestrator", goal="g"))
    (parent_dir / "worktree").mkdir()

    child_id = "20260423-scriptc-aaaa"
    plan = Plan(
        parent_id=parent_id,
        summary="s",
        subtasks=[SubtaskSpec(id=child_id, role="implementer", goal="c")],
    )
    (parent_dir / "plan.json").write_text(plan.model_dump_json())
    subtasks = parent_dir / "subtasks"
    subtasks.mkdir()

    child_dir = subtasks / child_id
    child_dir.mkdir()
    child_result = Result(
        task_id=child_id,
        status="success",
        summary="done",
        tokens_in=100,
        tokens_out=50,
        cost_usd=0.042,
        duration_s=1.0,
    )
    (child_dir / "result.json").write_text(child_result.model_dump_json())

    parent_task = board.read_task(parent_dir)
    with patch("mas.tick.worktree.commit_changes"), patch("mas.tick.worktree.prune"):
        _finalize_parent(env, parent_dir, parent_task)

    done_dir = mas / "tasks" / "done" / parent_id
    parent_result = board.read_result(done_dir)

    assert parent_result is not None, "parent result.json must be written on finalization"
    assert parent_result.cost_usd == pytest.approx(0.042), "cost_usd must be aggregated from child"
    assert parent_result.tokens_in == 100
    assert parent_result.tokens_out == 50
