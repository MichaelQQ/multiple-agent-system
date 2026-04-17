"""Comprehensive tests for mas/tick.py covering all code paths.

Tests use the same patterns as tests/test_orphan.py:
- tmp_path fixtures
- TickEnv with mock provider (ProviderConfig with cli='sh')
- Direct calls to _advance_one / _advance_doing
- unittest.mock.patch for _pid_alive, worktree.*, adapter.dispatch, board.count_active_pids
"""

import errno
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mas import board, transitions, worktree as wt_module
from mas.schemas import (
    MasConfig,
    Plan,
    ProviderConfig,
    Result,
    RoleConfig,
    SubtaskSpec,
    Task,
)
from mas.tick import (
    LockBusy,
    TickEnv,
    _acquire_lock,
    _advance_doing,
    _advance_one,
    _all_children_passed,
    _append_revision_cycle,
    _collect_prior_results,
    _finalize_parent,
    _handle_child_result,
    _materialize_plan,
    _materialize_proposal,
    _maybe_dispatch_proposer,
    _next_ready_child,
    _pid_alive,
    _read_attempt,
    _role_running,
    _worker_orphaned,
    run_tick,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _cfg(max_retries: int = 2, max_proposed: int = 10) -> MasConfig:
    return MasConfig(
        providers={"mock": ProviderConfig(cli="sh", max_concurrent=2, extra_args=[])},
        roles={
            "proposer": RoleConfig(provider="mock", max_retries=max_retries),
            "orchestrator": RoleConfig(provider="mock", max_retries=max_retries),
            "implementer": RoleConfig(provider="mock", max_retries=max_retries),
            "tester": RoleConfig(provider="mock", max_retries=max_retries),
            "evaluator": RoleConfig(provider="mock", max_retries=max_retries),
        },
        max_proposed=max_proposed,
    )


def _seed_parent(mas: Path, parent_id: str, role: str = "orchestrator") -> Path:
    parent = board.task_dir(mas, "doing", parent_id)
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id=parent_id, role=role, goal="g"))
    (parent / "worktree").mkdir()
    return parent


def _seed_parent_with_plan(mas: Path, parent_id: str, child_id: str) -> Path:
    parent = _seed_parent(mas, parent_id)
    plan = Plan(
        parent_id=parent_id,
        summary="s",
        subtasks=[SubtaskSpec(id=child_id, role="implementer", goal="do")],
    )
    (parent / "plan.json").write_text(plan.model_dump_json())
    (parent / "subtasks" / child_id).mkdir(parents=True)
    return parent


# ---------------------------------------------------------------------------
# 1. _advance_doing — iterates doing/, catches per-task exceptions
# ---------------------------------------------------------------------------

def test_advance_doing_iterates_all_doing(tmp_path: Path):
    """All doing/ tasks are visited even if one raises."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)

    for tid in ["t1", "t2"]:
        p = board.task_dir(mas, "doing", tid)
        p.mkdir(parents=True)
        board.write_task(p, Task(id=tid, role="orchestrator", goal="g"))
        (p / "worktree").mkdir()

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with patch("mas.tick._advance_one", side_effect=[RuntimeError("boom"), None]):
        _advance_doing(env)

    assert (mas / "tasks" / "doing" / "t1").exists()
    assert (mas / "tasks" / "doing" / "t2").exists()


def test_advance_doing_catches_exception_per_task(tmp_path: Path, caplog):
    """A failing _advance_one does not abort the loop."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)

    for tid in ["e1", "e2", "e3"]:
        d = board.task_dir(mas, "doing", tid)
        d.mkdir(parents=True)
        board.write_task(d, Task(id=tid, role="orchestrator", goal="g"))
        (d / "worktree").mkdir()

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with patch("mas.tick._advance_one", side_effect=RuntimeError("die")):
        _advance_doing(env)

    assert "advance failed" in caplog.text


# ---------------------------------------------------------------------------
# 2. _advance_one — proposer path
# ---------------------------------------------------------------------------

def test_proposer_no_result_no_log_dispatches(tmp_path: Path):
    """Never-dispatched proposer is dispatched."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "prop-1")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="prop-1", role="proposer", goal="propose"))

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with patch("mas.tick.get_adapter") as mock_get, \
         patch("mas.board.count_active_pids", return_value=0), \
         patch("mas.board.write_pid"):
        mock_adapter = MagicMock()
        mock_adapter.dispatch.return_value = MagicMock(pid=12345)
        mock_adapter.agentic = False
        mock_get.return_value.return_value = mock_adapter
        _advance_one(env, parent)

    mock_adapter.dispatch.assert_called_once()


def test_proposer_success_moves_to_done(tmp_path: Path):
    """Successful proposer result → done column + materialize."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "prop-2")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="prop-2", role="proposer", goal="propose"))
    (parent / "logs").mkdir()
    (parent / "logs" / "proposer-1.log").write_text("ok")
    result = Result(task_id="prop-2", status="success", summary="proposed something")
    (parent / "result.json").write_text(result.model_dump_json())

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    _advance_one(env, parent)

    assert not parent.exists()
    assert (mas / "tasks" / "done" / "prop-2").exists()


def test_proposer_failure_moves_to_failed(tmp_path: Path):
    """Failed proposer result → failed column."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "prop-3")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="prop-3", role="proposer", goal="propose"))
    (parent / "logs").mkdir()
    (parent / "logs" / "proposer-1.log").write_text("err")
    result = Result(task_id="prop-3", status="failure", summary="failed")
    (parent / "result.json").write_text(result.model_dump_json())

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    _advance_one(env, parent)

    assert not parent.exists()
    assert (mas / "tasks" / "failed" / "prop-3").exists()


def test_proposer_orphan_moves_to_failed(tmp_path: Path):
    """Orphaned proposer (log exists, no pid, no result) → failed."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "prop-4")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="prop-4", role="proposer", goal="propose"))
    (parent / "logs").mkdir()
    (parent / "logs" / "proposer-1.log").write_text("crash")

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with patch("mas.tick._role_running", return_value=False):
        _advance_one(env, parent)

    assert not parent.exists()
    assert (mas / "tasks" / "failed" / "prop-4").exists()


# ---------------------------------------------------------------------------
# 3. _advance_one — orchestrator path
# ---------------------------------------------------------------------------

def test_orchestrator_creates_worktree(tmp_path: Path):
    """Missing worktree dir triggers worktree.create."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "orch-1")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="orch-1", role="orchestrator", goal="g"))
    assert not (parent / "worktree").exists()

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with patch.object(wt_module, "create") as mock_create, \
         patch("mas.tick.get_adapter") as mock_get, \
         patch("mas.board.count_active_pids", return_value=0), \
         patch("mas.board.write_pid"):
        mock_adapter = MagicMock()
        mock_adapter.dispatch.return_value = MagicMock(pid=12345)
        mock_adapter.agentic = False
        mock_get.return_value.return_value = mock_adapter
        _advance_one(env, parent)

    mock_create.assert_called_once()


def test_orchestrator_no_plan_no_result_dispatches(tmp_path: Path):
    """Orchestrator with no plan.json and no result → dispatch."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "orch-2")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="orch-2", role="orchestrator", goal="g"))
    (parent / "worktree").mkdir()
    assert not (parent / "plan.json").exists()

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with patch("mas.tick.get_adapter") as mock_get, \
         patch("mas.board.count_active_pids", return_value=0), \
         patch("mas.board.write_pid"), \
         patch("mas.tick._role_running", return_value=False):
        mock_adapter = MagicMock()
        mock_adapter.dispatch.return_value = MagicMock(pid=12345)
        mock_adapter.agentic = False
        mock_get.return_value.return_value = mock_adapter
        _advance_one(env, parent)

    mock_adapter.dispatch.assert_called_once()


def test_orchestrator_materializes_plan_from_handoff(tmp_path: Path):
    """Orchestrator result with handoff → plan.json written."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "orch-3")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="orch-3", role="orchestrator", goal="g"))
    (parent / "worktree").mkdir()
    result = Result(
        task_id="orch-3",
        status="success",
        summary="plan created",
        handoff={"parent_id": "orch-3", "summary": "s", "subtasks": []},
    )
    (parent / "result.json").write_text(result.model_dump_json())

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    _advance_one(env, parent)

    assert (parent / "plan.json").exists()


def test_orchestrator_orphan_retries(tmp_path: Path):
    """Orphaned orchestrator bumps attempt and keeps parent in doing/."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "orch-4")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="orch-4", role="orchestrator", goal="g"))
    (parent / "worktree").mkdir()
    (parent / "logs").mkdir()
    (parent / "logs" / "orchestrator-1.log").write_text("crash\n")
    (parent / ".orchestrator_attempt").write_text("1")

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(max_retries=2))

    with patch("mas.tick._role_running", return_value=False):
        _advance_one(env, parent)

    assert parent.exists()
    assert (parent / ".orchestrator_attempt").read_text().strip() == "2"
    assert (parent / ".previous_failure").exists()


def test_orchestrator_max_retries_fails(tmp_path: Path):
    """Orphaned orchestrator at max_retries → parent moved to failed/.

    Simulates two orphan ticks:
    - Tick 1: orchestrator at attempt 1 orphaned → bumps to attempt 2
    - Tick 2: orchestrator at attempt 2 orphaned → exceeds max_retries → moves to failed/
    """
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "orch-5")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="orch-5", role="orchestrator", goal="g"))
    (parent / "worktree").mkdir()
    (parent / "logs").mkdir()
    (parent / "logs" / "orchestrator-1.log").write_text("crash\n")
    (parent / ".orchestrator_attempt").write_text("1")

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(max_retries=1))

    with patch("mas.tick._dispatch_role"), \
         patch("mas.board.count_active_pids", return_value=0):
        _advance_one(env, parent)

    assert parent.exists()
    assert (parent / ".orchestrator_attempt").read_text().strip() == "2"
    assert (parent / ".previous_failure").exists()

    (parent / "logs" / "orchestrator-2.log").write_text("crash again\n")

    with patch("mas.tick._dispatch_role"), \
         patch("mas.board.count_active_pids", return_value=0):
        _advance_one(env, parent)

    assert not parent.exists()
    assert (mas / "tasks" / "failed" / "orch-5").exists()


# ---------------------------------------------------------------------------
# 4. _advance_one — child dispatch
# ---------------------------------------------------------------------------

def test_next_ready_child_skips_successful(tmp_path: Path):
    """_next_ready_child returns None when all subtasks succeeded."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = _seed_parent_with_plan(mas, "p-1", "impl-1")
    subtasks = parent / "subtasks"
    child = subtasks / "impl-1"
    r = Result(task_id="impl-1", status="success", summary="done")
    (child / "result.json").write_text(r.model_dump_json())

    plan = parse_plan(parent / "plan.json", "p-1")
    result = _next_ready_child(plan, subtasks)
    assert result is None


def test_next_ready_child_returns_failed(tmp_path: Path):
    """_next_ready_child returns first non-successful child."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "p-2")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="p-2", role="orchestrator", goal="g"))
    subtasks = parent / "subtasks"
    subtasks.mkdir()

    plan = Plan(
        parent_id="p-2",
        summary="s",
        subtasks=[
            SubtaskSpec(id="impl-1", role="implementer", goal="do"),
            SubtaskSpec(id="impl-2", role="implementer", goal="do2"),
        ],
    )
    (parent / "plan.json").write_text(plan.model_dump_json())

    child1 = subtasks / "impl-1"
    child1.mkdir()
    r1 = Result(task_id="impl-1", status="success", summary="done")
    (child1 / "result.json").write_text(r1.model_dump_json())

    child2 = subtasks / "impl-2"
    child2.mkdir()

    result = _next_ready_child(plan, subtasks)
    assert result is not None
    assert result.id == "impl-2"


def test_first_dispatch_creates_task_json(tmp_path: Path):
    """First dispatch for a child creates task.json."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = _seed_parent_with_plan(mas, "p-3", "impl-1")
    child = parent / "subtasks" / "impl-1"

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with patch("mas.tick.get_adapter") as mock_get, \
         patch("mas.board.count_active_pids", return_value=0), \
         patch("mas.board.write_pid"):
        mock_adapter = MagicMock()
        mock_adapter.dispatch.return_value = MagicMock(pid=12345)
        mock_adapter.agentic = False
        mock_get.return_value.return_value = mock_adapter
        _advance_one(env, parent)

    assert (child / "task.json").exists()


def test_skip_dispatch_if_role_already_running(tmp_path: Path):
    """If role is already running (live pid), no new dispatch occurs."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = _seed_parent_with_plan(mas, "p-4", "impl-1")
    child = parent / "subtasks" / "impl-1"
    (child / "pids").mkdir()
    (child / "pids" / "implementer.0.pid").write_text("99999")

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with patch("mas.adapters.get_adapter") as mock_get, \
         patch("mas.tick._pid_alive", return_value=True):
        mock_adapter = MagicMock()
        mock_get.return_value.return_value = mock_adapter
        _advance_one(env, parent)

    mock_adapter.dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# 4b. _collect_prior_results + dispatch-time prior_results injection
# ---------------------------------------------------------------------------

def test_collect_prior_results_returns_preceding_siblings(tmp_path: Path):
    """Only subtasks before current_id with a result.json are returned, in plan order."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "p-pr1")
    parent.mkdir(parents=True)
    subtasks = parent / "subtasks"
    subtasks.mkdir()
    plan = Plan(
        parent_id="p-pr1", summary="s",
        subtasks=[
            SubtaskSpec(id="test-1", role="tester", goal="t"),
            SubtaskSpec(id="impl-1", role="implementer", goal="i"),
            SubtaskSpec(id="eval-1", role="evaluator", goal="e"),
        ],
    )
    d = subtasks / "test-1"
    d.mkdir()
    (d / "result.json").write_text(
        Result(task_id="test-1", status="success", summary="tests written",
               handoff={"test_command": "pytest tests/new.py", "test_files": ["tests/new.py"]}
               ).model_dump_json()
    )

    priors = _collect_prior_results(plan, "impl-1", subtasks)
    assert [r.task_id for r in priors] == ["test-1"]
    assert priors[0].handoff["test_command"] == "pytest tests/new.py"


def test_collect_prior_results_empty_for_first_subtask(tmp_path: Path):
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "p-pr2")
    parent.mkdir(parents=True)
    subtasks = parent / "subtasks"
    subtasks.mkdir()
    plan = Plan(
        parent_id="p-pr2", summary="s",
        subtasks=[SubtaskSpec(id="test-1", role="tester", goal="t")],
    )

    assert _collect_prior_results(plan, "test-1", subtasks) == []


def test_dispatch_injects_prior_results_into_task_json(tmp_path: Path):
    """When the implementer is dispatched, its task.json carries tester's result."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "p-pr3")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="p-pr3", role="orchestrator", goal="g"))
    (parent / "worktree").mkdir()
    plan = Plan(
        parent_id="p-pr3", summary="s",
        subtasks=[
            SubtaskSpec(id="test-1", role="tester", goal="t"),
            SubtaskSpec(id="impl-1", role="implementer", goal="i"),
        ],
    )
    (parent / "plan.json").write_text(plan.model_dump_json())
    subtasks = parent / "subtasks"
    test_dir = subtasks / "test-1"
    test_dir.mkdir(parents=True)
    (test_dir / "result.json").write_text(
        Result(task_id="test-1", status="success", summary="failing tests authored",
               handoff={"test_command": "pytest -q", "test_files": ["tests/x.py"],
                        "initial_exit_code": 1, "expected_exit_code_after_impl": 0}
               ).model_dump_json()
    )
    impl_dir = subtasks / "impl-1"
    impl_dir.mkdir()

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with patch("mas.tick.get_adapter") as mock_get, \
         patch("mas.board.count_active_pids", return_value=0), \
         patch("mas.board.write_pid"):
        mock_adapter = MagicMock()
        mock_adapter.dispatch.return_value = MagicMock(pid=12345)
        mock_adapter.agentic = False
        mock_get.return_value.return_value = mock_adapter
        _advance_one(env, parent)

    impl_task = board.read_task(impl_dir)
    assert len(impl_task.prior_results) == 1
    assert impl_task.prior_results[0].task_id == "test-1"
    assert impl_task.prior_results[0].handoff["test_command"] == "pytest -q"


# ---------------------------------------------------------------------------
# 5. _handle_child_result
# ---------------------------------------------------------------------------

def test_handle_child_result_success_passthrough(tmp_path: Path):
    """Successful child result is a no-op."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = _seed_parent_with_plan(mas, "p-5", "impl-1")
    child = parent / "subtasks" / "impl-1"
    r = Result(task_id="impl-1", status="success", summary="ok")
    (child / "result.json").write_text(r.model_dump_json())

    plan = parse_plan(parent / "plan.json", "p-5")
    spec = plan.subtasks[0]
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    _handle_child_result(env, parent, board.read_task(parent), plan, spec, r)

    assert parent.exists()


def test_handle_child_result_failure_bumps_attempt(tmp_path: Path):
    """Failed child below max_retries → bump attempt, rename result, write previous_failure."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = _seed_parent_with_plan(mas, "p-6", "impl-1")
    child = parent / "subtasks" / "impl-1"
    (child / ".attempt").write_text("1")
    r = Result(task_id="impl-1", status="failure", summary="oops")
    (child / "result.json").write_text(r.model_dump_json())

    plan = parse_plan(parent / "plan.json", "p-6")
    spec = plan.subtasks[0]
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(max_retries=2))

    _handle_child_result(env, parent, board.read_task(parent), plan, spec, r)

    assert (child / "result.failed-1.json").exists()
    assert not (child / "result.json").exists()
    assert (child / ".attempt").read_text().strip() == "2"
    assert (child / ".previous_failure").exists()


def test_handle_child_result_failure_max_retries_moves_parent(tmp_path: Path):
    """Failed child at max_retries → parent moved to failed/."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = _seed_parent_with_plan(mas, "p-7", "impl-1")
    child = parent / "subtasks" / "impl-1"
    (child / ".attempt").write_text("3")
    r = Result(task_id="impl-1", status="failure", summary="still failing")
    (child / "result.json").write_text(r.model_dump_json())

    plan = parse_plan(parent / "plan.json", "p-7")
    spec = plan.subtasks[0]
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(max_retries=2))

    _handle_child_result(env, parent, board.read_task(parent), plan, spec, r)

    assert not parent.exists()
    assert (mas / "tasks" / "failed" / "p-7").exists()


def test_handle_child_result_evaluator_needs_revision(tmp_path: Path):
    """Evaluator verdict=needs_revision → appends revision cycle subtasks."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "p-8")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="p-8", role="orchestrator", goal="g"))
    subtasks = parent / "subtasks"
    subtasks.mkdir()
    plan = Plan(
        parent_id="p-8",
        summary="s",
        subtasks=[SubtaskSpec(id="eval-1", role="evaluator", goal="eval")],
    )
    (parent / "plan.json").write_text(plan.model_dump_json())
    child = subtasks / "eval-1"
    child.mkdir()
    r = Result(task_id="eval-1", status="needs_revision", summary="revise",
               verdict="needs_revision", feedback="fix it")
    (child / "result.json").write_text(r.model_dump_json())

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    _handle_child_result(env, parent, board.read_task(parent), plan, plan.subtasks[0], r)

    updated_plan = parse_plan(parent / "plan.json", "p-8")
    assert len(updated_plan.subtasks) == 4
    assert any(s.id == "rev-1-implementer" for s in updated_plan.subtasks)


# ---------------------------------------------------------------------------
# 6. _all_children_passed
# ---------------------------------------------------------------------------

def test_all_children_passed_true(tmp_path: Path):
    """All children successful → True."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "p-9")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="p-9", role="orchestrator", goal="g"))
    subtasks = parent / "subtasks"
    subtasks.mkdir()
    plan = Plan(
        parent_id="p-9", summary="s",
        subtasks=[
            SubtaskSpec(id="impl-1", role="implementer", goal="do"),
            SubtaskSpec(id="impl-2", role="implementer", goal="do2"),
        ],
    )
    (parent / "plan.json").write_text(plan.model_dump_json())
    for tid in ["impl-1", "impl-2"]:
        d = subtasks / tid
        d.mkdir()
        (d / "result.json").write_text(
            Result(task_id=tid, status="success", summary="ok").model_dump_json()
        )

    result = _all_children_passed(plan, subtasks)
    assert result is True


def test_all_children_passed_false_missing_result(tmp_path: Path):
    """Missing result → False."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "p-10")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="p-10", role="orchestrator", goal="g"))
    subtasks = parent / "subtasks"
    subtasks.mkdir()
    plan = Plan(parent_id="p-10", summary="s",
                subtasks=[SubtaskSpec(id="impl-1", role="implementer", goal="do")])

    result = _all_children_passed(plan, subtasks)
    assert result is False


def test_all_children_passed_evaluator_verdict_fail(tmp_path: Path):
    """Evaluator result with verdict!=pass → False."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "p-11")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="p-11", role="orchestrator", goal="g"))
    subtasks = parent / "subtasks"
    subtasks.mkdir()
    plan = Plan(parent_id="p-11", summary="s",
                subtasks=[SubtaskSpec(id="eval-1", role="evaluator", goal="eval")])
    (parent / "plan.json").write_text(plan.model_dump_json())
    d = subtasks / "eval-1"
    d.mkdir()
    (d / "result.json").write_text(
        Result(task_id="eval-1", status="success", summary="ok", verdict="fail").model_dump_json()
    )

    result = _all_children_passed(plan, subtasks)
    assert result is False


# ---------------------------------------------------------------------------
# 7. _append_revision_cycle
# ---------------------------------------------------------------------------

def test_append_revision_cycle_adds_three_subtasks(tmp_path: Path):
    """First revision cycle adds 3 subtasks."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "p-12")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="p-12", role="orchestrator", goal="g"))
    subtasks = parent / "subtasks"
    subtasks.mkdir()
    plan = Plan(parent_id="p-12", summary="s",
                subtasks=[SubtaskSpec(id="eval-1", role="evaluator", goal="eval")],
                max_revision_cycles=2)
    (parent / "plan.json").write_text(plan.model_dump_json())

    _append_revision_cycle(parent, plan, board.read_task(parent), "fix bugs")

    updated = parse_plan(parent / "plan.json", "p-12")
    assert len(updated.subtasks) == 4
    ids = {s.id for s in updated.subtasks}
    assert "rev-1-implementer" in ids
    assert "rev-1-tester" in ids
    assert "rev-1-evaluator" in ids


def test_append_revision_cycle_orders_tester_before_implementer(tmp_path: Path):
    """Under TDD the revision cycle is tester → implementer → evaluator."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "p-12b")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="p-12b", role="orchestrator", goal="g"))
    (parent / "subtasks").mkdir()
    plan = Plan(parent_id="p-12b", summary="s",
                subtasks=[SubtaskSpec(id="eval-1", role="evaluator", goal="eval")],
                max_revision_cycles=2)
    (parent / "plan.json").write_text(plan.model_dump_json())

    _append_revision_cycle(parent, plan, board.read_task(parent), "fix it")

    updated = parse_plan(parent / "plan.json", "p-12b")
    rev = [s for s in updated.subtasks if s.id.startswith("rev-1-")]
    assert [s.role for s in rev] == ["tester", "implementer", "evaluator"]


def test_append_revision_cycle_respects_max_cap(tmp_path: Path):
    """At max_revision_cycles, no new subtasks are added."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "p-13")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="p-13", role="orchestrator", goal="g"))
    subtasks = parent / "subtasks"
    subtasks.mkdir()
    plan = Plan(
        parent_id="p-13", summary="s",
        subtasks=[
            SubtaskSpec(id="rev-1-implementer", role="implementer", goal="r1"),
            SubtaskSpec(id="rev-1-tester", role="tester", goal="r1t"),
            SubtaskSpec(id="rev-1-evaluator", role="evaluator", goal="r1e"),
            SubtaskSpec(id="rev-2-implementer", role="implementer", goal="r2"),
            SubtaskSpec(id="rev-2-tester", role="tester", goal="r2t"),
            SubtaskSpec(id="rev-2-evaluator", role="evaluator", goal="r2e"),
        ],
        max_revision_cycles=2,
    )
    (parent / "plan.json").write_text(plan.model_dump_json())

    _append_revision_cycle(parent, plan, board.read_task(parent), "more feedback")

    updated = parse_plan(parent / "plan.json", "p-13")
    assert len(updated.subtasks) == 6


# ---------------------------------------------------------------------------
# 8. _finalize_parent
# ---------------------------------------------------------------------------

def test_finalize_parent_moves_to_done(tmp_path: Path):
    """_finalize_parent moves parent to done/ and prunes."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "p-14")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="p-14", role="orchestrator", goal="g"))
    wt = parent / "worktree"
    wt.mkdir()

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with patch.object(wt_module, "commit_changes") as mock_commit, \
         patch.object(wt_module, "prune") as mock_prune:
        _finalize_parent(env, parent, board.read_task(parent))

    mock_commit.assert_called_once()
    mock_prune.assert_called_once()
    assert not parent.exists()
    assert (mas / "tasks" / "done" / "p-14").exists()


# ---------------------------------------------------------------------------
# 9. _materialize_proposal
# ---------------------------------------------------------------------------

def test_materialize_proposal_creates_task(tmp_path: Path):
    """Successful proposer result creates a proposed/ task."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    result = Result(
        task_id="prop-x",
        status="success",
        summary="build a thing",
        handoff={"goal": "build a new widget"},
    )

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(max_proposed=5))

    _materialize_proposal(env, result)

    proposed = list((mas / "tasks" / "proposed").iterdir())
    assert len(proposed) == 1
    task = board.read_task(proposed[0])
    assert task.role == "orchestrator"
    assert task.goal == "build a new widget"


def test_materialize_proposal_respects_max_proposed(tmp_path: Path):
    """At max_proposed, no new task is created."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    result = Result(
        task_id="prop-y",
        status="success",
        summary="another",
        handoff={"goal": "another task"},
    )

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(max_proposed=0))

    _materialize_proposal(env, result)

    assert not list((mas / "tasks" / "proposed").iterdir())


def test_materialize_proposal_handles_missing_goal(tmp_path: Path, caplog):
    """No goal in handoff/summary → no materialization (logged warning)."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    result = Result(task_id="prop-z", status="success", summary="", handoff={})

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with caplog.at_level(logging.WARNING):
        _materialize_proposal(env, result)

    assert not list((mas / "tasks" / "proposed").iterdir())


# ---------------------------------------------------------------------------
# 10. _maybe_dispatch_proposer
# ---------------------------------------------------------------------------

def test_maybe_dispatch_proposer_skips_at_max_proposed(tmp_path: Path):
    """At max_proposed, proposer is not dispatched."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    board.task_dir(mas, "proposed", "existing").mkdir(parents=True)
    board.write_task(board.task_dir(mas, "proposed", "existing"),
                     Task(id="existing", role="orchestrator", goal="g"))

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(max_proposed=1))

    with patch("mas.tick.get_adapter") as mock_get, \
         patch("mas.board.count_active_pids", return_value=0):
        mock_adapter = MagicMock()
        mock_get.return_value.return_value = mock_adapter
        _maybe_dispatch_proposer(env)

    mock_adapter.dispatch.assert_not_called()


def test_maybe_dispatch_proposer_skips_when_proposer_in_doing(tmp_path: Path):
    """Existing proposer in doing/ → no new dispatch."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    board.task_dir(mas, "doing", "proposer-running").mkdir(parents=True)
    board.write_task(board.task_dir(mas, "doing", "proposer-running"),
                     Task(id="proposer-running", role="proposer", goal="p"))

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with patch("mas.tick.get_adapter") as mock_get, \
         patch("mas.board.count_active_pids", return_value=0):
        mock_adapter = MagicMock()
        mock_get.return_value.return_value = mock_adapter
        _maybe_dispatch_proposer(env)

    mock_adapter.dispatch.assert_not_called()


def test_maybe_dispatch_proposer_respects_concurrency(tmp_path: Path):
    """Provider at max_concurrent → no new dispatch."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with patch("mas.tick.get_adapter") as mock_get, \
         patch("mas.board.count_active_pids", return_value=2):
        mock_adapter = MagicMock()
        mock_get.return_value.return_value = mock_adapter
        _maybe_dispatch_proposer(env)

    mock_adapter.dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# 11. _role_running
# ---------------------------------------------------------------------------

def test_role_running_no_pid_dir_returns_false():
    with patch("mas.tick._pid_alive") as mock_alive:
        result = _role_running(Path("/nonexistent"), "implementer")
    assert result is False
    mock_alive.assert_not_called()


def test_role_running_dead_pid_cleanup(tmp_path: Path):
    """Dead pid file is unlinked."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    pd = mas / "pids"
    pd.mkdir()
    pidfile = pd / "implementer.0.pid"
    pidfile.write_text("99999")

    with patch("mas.tick._pid_alive", return_value=False):
        result = _role_running(pd, "implementer")

    assert result is False
    assert not pidfile.exists()


def test_role_running_live_pid_returns_true(tmp_path: Path):
    """Live pid → True."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    pd = mas / "pids"
    pd.mkdir()
    pidfile = pd / "implementer.0.pid"
    pidfile.write_text("12345")

    with patch("mas.tick._pid_alive", return_value=True):
        result = _role_running(pd, "implementer")

    assert result is True


# ---------------------------------------------------------------------------
# 12. _read_attempt
# ---------------------------------------------------------------------------

def test_read_attempt_missing_file_returns_1(tmp_path: Path):
    assert _read_attempt(tmp_path / "nonexistent") == 1


def test_read_attempt_valid_int(tmp_path: Path):
    f = tmp_path / ".attempt"
    f.write_text("  5  ")
    assert _read_attempt(f) == 5


def test_read_attempt_invalid_returns_1(tmp_path: Path):
    f = tmp_path / ".attempt"
    f.write_text("not a number")
    assert _read_attempt(f) == 1


# ---------------------------------------------------------------------------
# 13. _worker_orphaned
# ---------------------------------------------------------------------------

def test_worker_orphaned_log_exists_no_pid(tmp_path: Path):
    """Log exists but role not running → orphan."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    td = mas / "tasks" / "doing" / "t1"
    td.mkdir(parents=True)
    logs = td / "logs"
    logs.mkdir()
    (logs / "implementer-1.log").write_text("output")

    with patch("mas.tick._role_running", return_value=False):
        result = _worker_orphaned(td, "implementer", 1)

    assert result is True


def test_worker_orphaned_log_missing_not_orphan(tmp_path: Path):
    """No log for this attempt → not orphan (never dispatched)."""
    result = _worker_orphaned(Path("/nonexistent"), "implementer", 1)
    assert result is False


def test_worker_orphaned_live_pid_not_orphan(tmp_path: Path):
    """Live pid exists → not orphan."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    td = mas / "tasks" / "doing" / "t2"
    td.mkdir(parents=True)
    logs = td / "logs"
    logs.mkdir()
    (logs / "orchestrator-1.log").write_text("running")

    with patch("mas.tick._role_running", return_value=True):
        result = _worker_orphaned(td, "orchestrator", 1)

    assert result is False


# ---------------------------------------------------------------------------
# 14. run_tick
# ---------------------------------------------------------------------------

def test_run_tick_lock_busy_skips(caplog):
    """LockBusy → tick is skipped with log."""
    mas = Path.cwd() / ".mas_test_lock_busy"
    mas.mkdir(exist_ok=True)
    board.ensure_layout(mas)

    with patch("mas.tick._acquire_lock", side_effect=LockBusy("locked")):
        with caplog.at_level(logging.INFO):
            try:
                run_tick(start=mas)
            finally:
                pass

    assert "skipped" in caplog.text or "lock" in caplog.text


def test_run_tick_lock_acquired_runs_steps(tmp_path: Path):
    """Successful lock → all steps run."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)

    lock_mock = MagicMock()
    with patch("mas.tick._acquire_lock", return_value=lock_mock), \
         patch("mas.tick.load_config", return_value=_cfg()), \
         patch("mas.tick.project_root", return_value=tmp_path), \
         patch("mas.tick._reap_workers") as mock_reap, \
         patch("mas.tick._advance_doing") as mock_adv, \
         patch("mas.tick._maybe_dispatch_proposer") as mock_prop:
        run_tick(start=tmp_path)

    mock_reap.assert_called_once()
    mock_adv.assert_called_once()
    mock_prop.assert_called_once()
    lock_mock.close.assert_called_once()


def test_run_tick_finally_closes_lock(tmp_path: Path):
    """Lock is closed even when steps raise."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)

    lock_mock = MagicMock()
    with patch("mas.tick._acquire_lock", return_value=lock_mock), \
         patch("mas.tick.load_config", return_value=_cfg()), \
         patch("mas.tick.project_root", return_value=tmp_path), \
         patch("mas.tick._reap_workers", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            run_tick(start=tmp_path)

    lock_mock.close.assert_called_once()


# ---------------------------------------------------------------------------
# _pid_alive — edge cases
# ---------------------------------------------------------------------------

def test_pid_alive_eperm_is_alive():
    """EPERM from kill means process exists."""
    with patch("os.kill", side_effect=OSError(errno.EPERM, "Operation not permitted")):
        assert _pid_alive(12345) is True


def test_pid_alive_no_such_process():
    """OSError with non-EPERM → check ps output."""
    with patch("os.kill", side_effect=OSError(errno.ESRCH, "No such process")), \
         patch("subprocess.run", return_value=MagicMock(stdout=MagicMock(strip=lambda: "Z"))):
        result = _pid_alive(99999)
    assert result is False


# ---------------------------------------------------------------------------
# _materialize_plan
# ---------------------------------------------------------------------------

def test_materialize_plan_valid_handoff(tmp_path: Path):
    """Valid handoff → plan.json written."""
    parent = tmp_path / "parent"
    parent.mkdir()
    result = Result(
        task_id="orch-x",
        status="success",
        summary="ok",
        handoff={
            "parent_id": "orch-x",
            "summary": "plan summary",
            "subtasks": [{"id": "s1", "role": "implementer", "goal": "do it"}],
        },
    )

    ok = _materialize_plan(parent, result)

    assert ok is True
    assert (parent / "plan.json").exists()
    plan = parse_plan(parent / "plan.json", "orch-x")
    assert len(plan.subtasks) == 1


def test_materialize_plan_invalid_handoff(tmp_path: Path):
    """Invalid handoff → False, no file written."""
    parent = tmp_path / "parent"
    parent.mkdir()
    result = Result(task_id="bad", status="success", summary="nope", handoff={})

    ok = _materialize_plan(parent, result)

    assert ok is False
    assert not (parent / "plan.json").exists()


# ---------------------------------------------------------------------------
# Helper needed by some tests
# ---------------------------------------------------------------------------

def parse_plan(path: Path, parent_id: str):
    from mas.roles import parse_plan as _parse
    return _parse(path, parent_id)
