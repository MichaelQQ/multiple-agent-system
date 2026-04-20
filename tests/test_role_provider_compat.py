"""Tests verifying that all roles work correctly with both agentic and
non-agentic providers.

Current known bug: the orchestrator role only works with agentic providers
because it relies on plan.json being written by the agent. Non-agentic
providers (e.g. Ollama) can only write result.json, so the tick loop never
finds plan.json and the task stalls. A `_materialize_plan` path is needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mas import board
from mas.adapters import get_adapter
from mas.adapters.base import DispatchHandle
from mas.schemas import (
    MasConfig,
    Plan,
    ProviderConfig,
    Result,
    RoleConfig,
    SubtaskSpec,
    Task,
)
from mas.tick import TickEnv, _advance_one, _dispatch_role


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _agentic_cfg(role: str = "implementer") -> MasConfig:
    """Config using mock (agentic) provider for all roles."""
    return MasConfig(
        providers={"mock": ProviderConfig(cli="sh", max_concurrent=4, extra_args=[])},
        roles={
            r: RoleConfig(provider="mock", max_retries=2)
            for r in ("proposer", "orchestrator", "implementer", "tester", "evaluator")
        },
    )


def _nonagentic_cfg() -> MasConfig:
    """Config using ollama (non-agentic) provider for all roles."""
    return MasConfig(
        providers={"ollama": ProviderConfig(cli="ollama", max_concurrent=4, extra_args=[])},
        roles={
            r: RoleConfig(provider="ollama", model="llama3.2", max_retries=2)
            for r in ("proposer", "orchestrator", "implementer", "tester", "evaluator")
        },
    )


def _fake_handle(role: str = "implementer", provider: str = "mock") -> DispatchHandle:
    return DispatchHandle(pid=99999, provider=provider, role=role, task_dir=Path("/tmp"), log_path=Path("/tmp/x.log"))


def _setup_mas(tmp_path: Path) -> Path:
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    (mas / "prompts").mkdir(exist_ok=True)
    for role in ("proposer", "orchestrator", "implementer", "tester", "evaluator"):
        (mas / "prompts" / f"{role}.md").write_text("goal=$goal task_dir=$task_dir worktree=$worktree mas_dir=$mas_dir")
    return mas


# ---------------------------------------------------------------------------
# stdin_text routing: agentic vs non-agentic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["proposer", "orchestrator", "implementer", "tester", "evaluator"])
def test_agentic_adapter_gets_no_stdin_text(tmp_path: Path, role: str):
    """Agentic adapters must receive stdin_text=None so they don't read stdin."""
    mas = _setup_mas(tmp_path)
    task_dir_ = board.task_dir(mas, "doing", "20260415-t1-aaaa")
    task_dir_.mkdir(parents=True)
    task = Task(id="20260415-t1-aaaa", role=role, goal="do something")

    cfg = _agentic_cfg()
    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)

    captured: list[dict] = []

    def fake_dispatch(self, prompt, task_dir, cwd, log_path, role, stdin_text=None):  # noqa: ARG001
        captured.append({"stdin_text": stdin_text, "prompt": prompt})
        return _fake_handle(role)

    adapter_cls = get_adapter("mock")
    with patch.object(adapter_cls, "dispatch", fake_dispatch):
        _dispatch_role(env, task, task_dir_, tmp_path, role=role)

    assert captured, "dispatch was not called"
    assert captured[0]["stdin_text"] is None, (
        f"agentic adapter for {role} must get stdin_text=None, got {captured[0]['stdin_text']!r}"
    )


@pytest.mark.parametrize("role", ["proposer", "orchestrator", "implementer", "tester", "evaluator"])
def test_nonagentic_adapter_gets_prompt_as_stdin_text(tmp_path: Path, role: str):
    """Non-agentic adapters must receive stdin_text=prompt so the prompt reaches the model."""
    mas = _setup_mas(tmp_path)
    task_dir_ = board.task_dir(mas, "doing", "20260415-t2-aaaa")
    task_dir_.mkdir(parents=True)
    task = Task(id="20260415-t2-aaaa", role=role, goal="do something")

    cfg = _nonagentic_cfg()
    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)

    captured: list[dict] = []

    def fake_dispatch(self, prompt, task_dir, cwd, log_path, role, stdin_text=None):  # noqa: ARG001
        captured.append({"stdin_text": stdin_text, "prompt": prompt})
        return _fake_handle(role, provider="ollama")

    adapter_cls = get_adapter("ollama")
    with patch.object(adapter_cls, "dispatch", fake_dispatch):
        _dispatch_role(env, task, task_dir_, tmp_path, role=role)

    assert captured, "dispatch was not called"
    assert captured[0]["stdin_text"] is not None, (
        f"non-agentic adapter for {role} must get stdin_text=prompt, got None"
    )
    assert captured[0]["stdin_text"] == captured[0]["prompt"], (
        f"stdin_text must equal the rendered prompt for role {role}"
    )


# ---------------------------------------------------------------------------
# Proposer: both agentic and non-agentic materialize proposal via handoff
# ---------------------------------------------------------------------------


def test_proposer_agentic_result_materializes_proposal(tmp_path: Path):
    """Agentic proposer writes result.json with handoff → tick creates proposed task."""
    mas = _setup_mas(tmp_path)
    parent = board.task_dir(mas, "doing", "20260415-prop-1-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-prop-1-aaaa", role="proposer", goal="propose"))

    result = Result(
        task_id="20260415-prop-1-aaaa",
        status="success",
        summary="Add test coverage for roles",
        handoff={
            "goal": "Add unit tests for all roles",
            "rationale": "Coverage gap identified",
            "acceptance": ["tests pass"],
        },
        duration_s=1.0,
    )
    (parent / "result.json").write_text(result.model_dump_json())

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_agentic_cfg())
    _advance_one(env, parent)

    proposed = board.list_column(mas, "proposed")
    assert len(proposed) == 1, "one proposal should be materialized"
    task = board.read_task(proposed[0])
    assert task.goal == "Add unit tests for all roles"
    assert task.role == "orchestrator"


def test_proposer_nonagentic_result_materializes_proposal(tmp_path: Path):
    """Non-agentic proposer writes result.json with handoff → same materialization path."""
    mas = _setup_mas(tmp_path)
    parent = board.task_dir(mas, "doing", "20260415-prop-2-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-prop-2-aaaa", role="proposer", goal="propose"))

    result = Result(
        task_id="20260415-prop-2-aaaa",
        status="success",
        summary="Improve error handling",
        handoff={
            "goal": "Add better error messages to tick loop",
            "rationale": "Errors are opaque",
        },
        duration_s=2.5,
    )
    (parent / "result.json").write_text(result.model_dump_json())

    # Non-agentic config, but the result.json is already written (simulates
    # ollama wrapper having run). Only _advance_one logic matters here.
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_nonagentic_cfg())
    _advance_one(env, parent)

    proposed = board.list_column(mas, "proposed")
    assert len(proposed) == 1
    task = board.read_task(proposed[0])
    assert task.goal == "Add better error messages to tick loop"


def test_proposer_result_without_handoff_goal_uses_summary(tmp_path: Path):
    """If handoff has no goal key, fall back to result.summary."""
    mas = _setup_mas(tmp_path)
    parent = board.task_dir(mas, "doing", "20260415-prop-3-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-prop-3-aaaa", role="proposer", goal="propose"))

    result = Result(
        task_id="20260415-prop-3-aaaa",
        status="success",
        summary="Refactor config loader",
        handoff={},  # no 'goal' key
        duration_s=1.0,
    )
    (parent / "result.json").write_text(result.model_dump_json())

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_agentic_cfg())
    _advance_one(env, parent)

    proposed = board.list_column(mas, "proposed")
    assert len(proposed) == 1
    task = board.read_task(proposed[0])
    assert task.goal == "Refactor config loader"


# ---------------------------------------------------------------------------
# Orchestrator: agentic writes plan.json; non-agentic currently broken
# ---------------------------------------------------------------------------


def test_orchestrator_agentic_reads_plan_json(tmp_path: Path):
    """Agentic orchestrator writes plan.json → tick parses it and moves to subtasks."""
    mas = _setup_mas(tmp_path)
    parent = board.task_dir(mas, "doing", "20260415-orch-1-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-orch-1-aaaa", role="orchestrator", goal="do work"))
    (parent / "worktree").mkdir()

    # Simulate agentic orchestrator having written plan.json + result.json.
    plan = Plan(
        parent_id="20260415-orch-1-aaaa",
        summary="implement and test",
        subtasks=[
            SubtaskSpec(id="20260415-impl-1-aaaa", role="implementer", goal="implement feature"),
            SubtaskSpec(id="20260415-test-1-aaaa", role="tester", goal="test feature"),
        ],
    )
    (parent / "plan.json").write_text(plan.model_dump_json())
    result = Result(task_id="20260415-orch-1-aaaa", status="success", summary="plan emitted", duration_s=1.0)
    (parent / "result.json").write_text(result.model_dump_json())

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_agentic_cfg())
    _advance_one(env, parent)

    # Subtasks directory should be created and first subtask ready to dispatch.
    assert (parent / "subtasks").exists(), "subtasks dir should exist after plan is parsed"
    assert (parent / "subtasks" / "20260415-impl-1-aaaa").exists() or not (parent / "subtasks" / "20260415-impl-1-aaaa" / "result.json").exists()


def test_orchestrator_nonagentic_result_without_plan_json_is_not_lost(tmp_path: Path):
    """Non-agentic orchestrator writes result.json (with plan in handoff) but
    cannot write plan.json. The tick must materialize plan.json from the handoff
    rather than treating the task as still pending.

    Currently FAILS because _advance_one ignores result.json when plan.json is
    absent — it re-dispatches the orchestrator on every tick instead of
    materializing the plan.
    """
    mas = _setup_mas(tmp_path)
    parent = board.task_dir(mas, "doing", "20260415-orch-2-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-orch-2-aaaa", role="orchestrator", goal="do work"))
    (parent / "worktree").mkdir()

    # Non-agentic orchestrator wrote result.json with plan in handoff but no plan.json.
    plan_data = {
        "parent_id": "20260415-orch-2-aaaa",
        "summary": "implement then test",
        "max_revision_cycles": 2,
        "subtasks": [
            {"id": "20260415-impl-1-aaaa", "role": "implementer", "goal": "implement", "inputs": {}, "constraints": {}},
            {"id": "20260415-test-1-aaaa", "role": "tester", "goal": "test", "inputs": {}, "constraints": {}},
        ],
    }
    result = Result(
        task_id="20260415-orch-2-aaaa",
        status="success",
        summary="plan emitted",
        handoff=plan_data,
        duration_s=2.0,
    )
    (parent / "result.json").write_text(result.model_dump_json())
    # Crucially: no plan.json written.
    assert not (parent / "plan.json").exists()

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_nonagentic_cfg())
    _advance_one(env, parent)

    # After _advance_one, plan.json must exist (materialized from handoff).
    assert (parent / "plan.json").exists(), (
        "plan.json must be materialized from result.handoff for non-agentic orchestrator"
    )
    plan = Plan.model_validate_json((parent / "plan.json").read_text())
    assert len(plan.subtasks) == 2
    assert plan.subtasks[0].id == "20260415-impl-1-aaaa"


# ---------------------------------------------------------------------------
# Implementer / Tester: non-agentic result.json is consumed like agentic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["implementer", "tester"])
def test_worker_nonagentic_success_result_consumed(tmp_path: Path, role: str):
    """Non-agentic implementer/tester: result.json with status=success advances
    the plan to the next subtask (no special handling needed — just verify the
    tick reads it the same way it would for an agentic provider)."""
    mas = _setup_mas(tmp_path)
    parent = board.task_dir(mas, "doing", "20260415-p1-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-p1-aaaa", role="orchestrator", goal="g"))
    (parent / "worktree").mkdir()

    plan = Plan(
        parent_id="20260415-p1-aaaa",
        summary="s",
        subtasks=[
            SubtaskSpec(id=f"20260415-{role}-1-aaaa", role=role, goal="do"),
            SubtaskSpec(id="20260415-eval-1-aaaa", role="evaluator", goal="evaluate"),
        ],
    )
    (parent / "plan.json").write_text(plan.model_dump_json())

    child = parent / "subtasks" / f"20260415-{role}-1-aaaa"
    child.mkdir(parents=True)
    result = Result(task_id=f"20260415-{role}-1-aaaa", status="success", summary="done", duration_s=1.0)
    (child / "result.json").write_text(result.model_dump_json())

    # Fixture result for the next subtask dispatch (evaluator).
    fixture = tmp_path / "fx.json"
    fixture.write_text('{"task_id":"20260415-eval-1-aaaa","status":"success","summary":"ok","verdict":"pass","duration_s":0}')
    cfg = _agentic_cfg()
    cfg.providers["mock"] = ProviderConfig(cli="sh", max_concurrent=4, extra_args=[str(fixture)])

    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)
    _advance_one(env, parent)

    # The next subtask (evaluator) should have been dispatched.
    assert (parent / "subtasks" / "20260415-eval-1-aaaa" / "task.json").exists(), (
        f"after {role} success, next subtask must be dispatched"
    )


@pytest.mark.parametrize("role", ["implementer", "tester"])
def test_worker_nonagentic_failure_result_triggers_retry(tmp_path: Path, role: str):
    """Non-agentic failure result triggers retry logic identical to agentic."""
    mas = _setup_mas(tmp_path)
    parent = board.task_dir(mas, "doing", "20260415-p2-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-p2-aaaa", role="orchestrator", goal="g"))
    (parent / "worktree").mkdir()

    plan = Plan(
        parent_id="20260415-p2-aaaa",
        summary="s",
        subtasks=[SubtaskSpec(id=f"20260415-{role}-1-aaaa", role=role, goal="do")],
    )
    (parent / "plan.json").write_text(plan.model_dump_json())

    child = parent / "subtasks" / f"20260415-{role}-1-aaaa"
    child.mkdir(parents=True)
    result = Result(task_id=f"20260415-{role}-1-aaaa", status="failure", summary="broke", duration_s=0.5)
    (child / "result.json").write_text(result.model_dump_json())

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_nonagentic_cfg())
    _advance_one(env, parent)

    assert not (child / "result.json").exists(), "failed result must be rotated"
    assert (child / "result.failed-1.json").exists()
    assert (child / ".attempt").read_text().strip() == "2"
    assert parent.exists(), "parent must stay in doing/ while retries remain"


# ---------------------------------------------------------------------------
# Evaluator: verdict handled correctly for both agentic and non-agentic
# ---------------------------------------------------------------------------


def test_evaluator_pass_verdict_finalizes_parent(tmp_path: Path):
    """Evaluator returning verdict=pass advances to parent finalization."""
    import mas.worktree as wt_mod

    mas = _setup_mas(tmp_path)
    parent = board.task_dir(mas, "doing", "20260415-p3-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-p3-aaaa", role="orchestrator", goal="g"))
    wt = parent / "worktree"
    wt.mkdir()

    plan = Plan(
        parent_id="20260415-p3-aaaa",
        summary="s",
        subtasks=[SubtaskSpec(id="20260415-eval-1-aaaa", role="evaluator", goal="evaluate")],
    )
    (parent / "plan.json").write_text(plan.model_dump_json())

    child = parent / "subtasks" / "20260415-eval-1-aaaa"
    child.mkdir(parents=True)
    result = Result(
        task_id="20260415-eval-1-aaaa", status="success", verdict="pass", summary="looks good", duration_s=1.0
    )
    (child / "result.json").write_text(result.model_dump_json())

    with patch.object(wt_mod, "prune", return_value=None):
        env = TickEnv(repo=tmp_path, mas=mas, cfg=_agentic_cfg())
        _advance_one(env, parent)

    assert not parent.exists(), "parent must move to done/ after evaluator pass"
    assert (mas / "tasks" / "done" / "20260415-p3-aaaa").exists()


def test_evaluator_fail_verdict_triggers_retry(tmp_path: Path):
    """Evaluator returning verdict=fail triggers subtask retry (not revision cycle)."""
    mas = _setup_mas(tmp_path)
    parent = board.task_dir(mas, "doing", "20260415-p4-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-p4-aaaa", role="orchestrator", goal="g"))
    (parent / "worktree").mkdir()

    plan = Plan(
        parent_id="20260415-p4-aaaa",
        summary="s",
        subtasks=[SubtaskSpec(id="20260415-eval-1-aaaa", role="evaluator", goal="evaluate")],
    )
    (parent / "plan.json").write_text(plan.model_dump_json())

    child = parent / "subtasks" / "20260415-eval-1-aaaa"
    child.mkdir(parents=True)
    result = Result(
        task_id="20260415-eval-1-aaaa", status="failure", verdict="fail", summary="tests failed", duration_s=1.0
    )
    (child / "result.json").write_text(result.model_dump_json())

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_agentic_cfg())
    _advance_one(env, parent)

    # Retry: result rotated, attempt bumped.
    assert not (child / "result.json").exists()
    assert (child / "result.failed-1.json").exists()
    assert (child / ".attempt").read_text().strip() == "2"


def test_evaluator_needs_revision_appends_cycle(tmp_path: Path):
    """Evaluator returning verdict=needs_revision appends a revision cycle."""
    mas = _setup_mas(tmp_path)
    parent = board.task_dir(mas, "doing", "20260415-p5-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-p5-aaaa", role="orchestrator", goal="g"))
    (parent / "worktree").mkdir()

    plan = Plan(
        parent_id="20260415-p5-aaaa",
        summary="s",
        subtasks=[SubtaskSpec(id="20260415-eval-1-aaaa", role="evaluator", goal="evaluate")],
    )
    (parent / "plan.json").write_text(plan.model_dump_json())

    child = parent / "subtasks" / "20260415-eval-1-aaaa"
    child.mkdir(parents=True)
    result = Result(
        task_id="20260415-eval-1-aaaa",
        status="success",
        verdict="needs_revision",
        summary="needs work",
        feedback="missing edge cases",
        duration_s=1.0,
    )
    (child / "result.json").write_text(result.model_dump_json())

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_agentic_cfg())
    _advance_one(env, parent)

    updated_plan = Plan.model_validate_json((parent / "plan.json").read_text())
    rev_ids = [s.id for s in updated_plan.subtasks if s.id.startswith("rev-")]
    assert len(rev_ids) == 3, f"expected 3 revision subtasks, got {rev_ids}"
    assert "rev-1-implementer" in rev_ids
    assert "rev-1-tester" in rev_ids
    assert "rev-1-evaluator" in rev_ids


def test_evaluator_nonagentic_pass_verdict_finalizes_parent(tmp_path: Path):
    """Non-agentic evaluator: ollama normalizes verdict in result.json.
    The tick reads verdict the same way regardless of provider."""
    import mas.worktree as wt_mod

    mas = _setup_mas(tmp_path)
    parent = board.task_dir(mas, "doing", "20260415-p6-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-p6-aaaa", role="orchestrator", goal="g"))
    wt = parent / "worktree"
    wt.mkdir()

    plan = Plan(
        parent_id="20260415-p6-aaaa",
        summary="s",
        subtasks=[SubtaskSpec(id="20260415-eval-1-aaaa", role="evaluator", goal="evaluate")],
    )
    (parent / "plan.json").write_text(plan.model_dump_json())

    child = parent / "subtasks" / "20260415-eval-1-aaaa"
    child.mkdir(parents=True)
    # Simulates what the ollama wrapper writes after normalizing the model output.
    result = Result(
        task_id="20260415-eval-1-aaaa", status="success", verdict="pass", summary="all good", duration_s=3.0
    )
    (child / "result.json").write_text(result.model_dump_json())

    with patch.object(wt_mod, "prune", return_value=None):
        env = TickEnv(repo=tmp_path, mas=mas, cfg=_nonagentic_cfg())
        _advance_one(env, parent)

    assert (mas / "tasks" / "done" / "20260415-p6-aaaa").exists(), (
        "non-agentic evaluator pass verdict must finalize parent"
    )
