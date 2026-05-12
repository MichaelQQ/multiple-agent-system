"""Comprehensive tests for mas/tick.py covering all code paths.

Tests use the same patterns as tests/test_orphan.py:
- tmp_path fixtures
- TickEnv with mock provider (ProviderConfig with cli='sh')
- Direct calls to _advance_one / _advance_doing
- unittest.mock.patch for _pid_alive, worktree.*, adapter.dispatch, board.count_active_pids
"""

import errno
import json
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
# Plan-validation stubs (injected into mas.tick until real impl exists)
# ---------------------------------------------------------------------------
import mas.tick as _tick_mod

if not hasattr(_tick_mod, "InvalidPlanError"):

    class _StubInvalidPlanError(ValueError):
        def __init__(self, message: str = ""):
            super().__init__(message)
    _tick_mod.InvalidPlanError = _StubInvalidPlanError

if not hasattr(_tick_mod, "_validate_plan"):

    def _stub_validate_plan(plan, config):
        raise NotImplementedError("_validate_plan stub")
    _tick_mod._validate_plan = _stub_validate_plan

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _cfg(
    max_retries: int = 2,
    max_proposed: int = 10,
    proposal_similarity_threshold: float = 0.7,
) -> MasConfig:
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
        proposal_similarity_threshold=proposal_similarity_threshold,
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

    for tid in ["20260415-t1-aaaa", "20260415-t2-aaaa"]:
        p = board.task_dir(mas, "doing", tid)
        p.mkdir(parents=True)
        board.write_task(p, Task(id=tid, role="orchestrator", goal="g"))
        (p / "worktree").mkdir()

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with patch("mas.tick._advance_one", side_effect=[RuntimeError("boom"), None]):
        _advance_doing(env)

    assert (mas / "tasks" / "doing" / "20260415-t1-aaaa").exists()
    assert (mas / "tasks" / "doing" / "20260415-t2-aaaa").exists()


def test_advance_doing_catches_exception_per_task(tmp_path: Path, caplog):
    """A failing _advance_one does not abort the loop."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)

    for tid in ["20260415-e1-aaaa", "20260415-e2-aaaa", "20260415-e3-aaaa"]:
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
    parent = board.task_dir(mas, "doing", "20260415-prop-1-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-prop-1-aaaa", role="proposer", goal="propose"))

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
    parent = board.task_dir(mas, "doing", "20260415-prop-2-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-prop-2-aaaa", role="proposer", goal="propose"))
    (parent / "logs").mkdir()
    (parent / "logs" / "proposer-1.log").write_text("ok")
    result = Result(task_id="20260415-prop-2-aaaa", status="success", summary="proposed something")
    (parent / "result.json").write_text(result.model_dump_json())

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    _advance_one(env, parent)

    assert not parent.exists()
    assert (mas / "tasks" / "done" / "20260415-prop-2-aaaa").exists()


def test_proposer_failure_moves_to_failed(tmp_path: Path):
    """Failed proposer result → failed column."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "20260415-prop-3-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-prop-3-aaaa", role="proposer", goal="propose"))
    (parent / "logs").mkdir()
    (parent / "logs" / "proposer-1.log").write_text("err")
    result = Result(task_id="20260415-prop-3-aaaa", status="failure", summary="failed")
    (parent / "result.json").write_text(result.model_dump_json())

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    _advance_one(env, parent)

    assert not parent.exists()
    assert (mas / "tasks" / "failed" / "20260415-prop-3-aaaa").exists()


def test_proposer_orphan_moves_to_failed(tmp_path: Path):
    """Orphaned proposer (log exists, no pid, no result) → failed."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "20260415-prop-4-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-prop-4-aaaa", role="proposer", goal="propose"))
    (parent / "logs").mkdir()
    (parent / "logs" / "proposer-1.log").write_text("crash")

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with patch("mas.tick._role_running", return_value=False):
        _advance_one(env, parent)

    assert not parent.exists()
    assert (mas / "tasks" / "failed" / "20260415-prop-4-aaaa").exists()


# ---------------------------------------------------------------------------
# 3. _advance_one — orchestrator path
# ---------------------------------------------------------------------------

def test_orchestrator_creates_worktree(tmp_path: Path):
    """Missing worktree dir triggers worktree.create."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "20260415-orch-1-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-orch-1-aaaa", role="orchestrator", goal="g"))
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
    parent = board.task_dir(mas, "doing", "20260415-orch-2-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-orch-2-aaaa", role="orchestrator", goal="g"))
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
    parent = board.task_dir(mas, "doing", "20260415-orch-3-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-orch-3-aaaa", role="orchestrator", goal="g"))
    (parent / "worktree").mkdir()
    result = Result(
        task_id="20260415-orch-3-aaaa",
        status="success",
        summary="plan created",
        handoff={"parent_id": "20260415-orch-3-aaaa", "summary": "s", "subtasks": []},
    )
    (parent / "result.json").write_text(result.model_dump_json())

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    _advance_one(env, parent)

    assert (parent / "plan.json").exists()


def test_orchestrator_orphan_retries(tmp_path: Path):
    """Orphaned orchestrator bumps attempt and keeps parent in doing/."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "20260415-orch-4-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-orch-4-aaaa", role="orchestrator", goal="g"))
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
    parent = board.task_dir(mas, "doing", "20260415-orch-5-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-orch-5-aaaa", role="orchestrator", goal="g"))
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
    assert (mas / "tasks" / "failed" / "20260415-orch-5-aaaa").exists()


# ---------------------------------------------------------------------------
# 4. _advance_one — child dispatch
# ---------------------------------------------------------------------------

def test_next_ready_child_skips_successful(tmp_path: Path):
    """_next_ready_child returns None when all subtasks succeeded."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = _seed_parent_with_plan(mas, "20260415-p-1-aaaa", "20260415-impl-1-aaaa")
    subtasks = parent / "subtasks"
    child = subtasks / "20260415-impl-1-aaaa"
    r = Result(task_id="20260415-impl-1-aaaa", status="success", summary="done")
    (child / "result.json").write_text(r.model_dump_json())

    plan = parse_plan(parent / "plan.json", "20260415-p-1-aaaa")
    result = _next_ready_child(plan, subtasks)
    assert result is None


def test_next_ready_child_returns_failed(tmp_path: Path):
    """_next_ready_child returns first non-successful child."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "20260415-p-2-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-p-2-aaaa", role="orchestrator", goal="g"))
    subtasks = parent / "subtasks"
    subtasks.mkdir()

    plan = Plan(
        parent_id="20260415-p-2-aaaa",
        summary="s",
        subtasks=[
            SubtaskSpec(id="20260415-impl-1-aaaa", role="implementer", goal="do"),
            SubtaskSpec(id="20260415-impl-2-aaaa", role="implementer", goal="do2"),
        ],
    )
    (parent / "plan.json").write_text(plan.model_dump_json())

    child1 = subtasks / "20260415-impl-1-aaaa"
    child1.mkdir()
    r1 = Result(task_id="20260415-impl-1-aaaa", status="success", summary="done")
    (child1 / "result.json").write_text(r1.model_dump_json())

    child2 = subtasks / "20260415-impl-2-aaaa"
    child2.mkdir()

    result = _next_ready_child(plan, subtasks)
    assert result is not None
    assert result.id == "20260415-impl-2-aaaa"


def test_first_dispatch_creates_task_json(tmp_path: Path):
    """First dispatch for a child creates task.json."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = _seed_parent_with_plan(mas, "20260415-p-3-aaaa", "20260415-impl-1-aaaa")
    child = parent / "subtasks" / "20260415-impl-1-aaaa"

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
    parent = _seed_parent_with_plan(mas, "20260415-p-4-aaaa", "20260415-impl-1-aaaa")
    child = parent / "subtasks" / "20260415-impl-1-aaaa"
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
    parent = board.task_dir(mas, "doing", "20260415-p-pr1-aaaa")
    parent.mkdir(parents=True)
    subtasks = parent / "subtasks"
    subtasks.mkdir()
    plan = Plan(
        parent_id="20260415-p-pr1-aaaa", summary="s",
        subtasks=[
            SubtaskSpec(id="20260415-test-1-aaaa", role="tester", goal="t"),
            SubtaskSpec(id="20260415-impl-1-aaaa", role="implementer", goal="i"),
            SubtaskSpec(id="20260415-eval-1-aaaa", role="evaluator", goal="e"),
        ],
    )
    d = subtasks / "20260415-test-1-aaaa"
    d.mkdir()
    (d / "result.json").write_text(
        Result(task_id="20260415-test-1-aaaa", status="success", summary="tests written",
               handoff={"test_command": "pytest tests/new.py", "test_files": ["tests/new.py"]}
               ).model_dump_json()
    )

    priors = _collect_prior_results(plan, "20260415-impl-1-aaaa", subtasks)
    assert [r.task_id for r in priors] == ["20260415-test-1-aaaa"]
    assert priors[0].handoff["test_command"] == "pytest tests/new.py"


def test_resolve_test_command_prefers_implementer_handoff(tmp_path: Path):
    from mas.tick import _resolve_test_command

    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "20260415-p-rtc1-aaaa")
    parent.mkdir(parents=True)
    subtasks = parent / "subtasks"
    subtasks.mkdir()
    plan = Plan(
        parent_id="20260415-p-rtc1-aaaa", summary="s",
        subtasks=[
            SubtaskSpec(id="20260415-test-1-aaaa", role="tester", goal="t"),
            SubtaskSpec(id="20260415-impl-1-aaaa", role="implementer", goal="i"),
        ],
    )
    d = subtasks / "20260415-test-1-aaaa"
    d.mkdir()
    (d / "result.json").write_text(
        Result(task_id="20260415-test-1-aaaa", status="success", summary="t",
               handoff={"test_command": "pytest tests/from_tester.py", "initial_exit_code": 1}
               ).model_dump_json()
    )
    impl_result = Result(task_id="20260415-impl-1-aaaa", status="success", summary="i",
                         handoff={"test_command": "pytest tests/from_impl.py", "final_exit_code": 0})
    cmd = _resolve_test_command(plan, "20260415-impl-1-aaaa", subtasks, impl_result)
    assert cmd == "pytest tests/from_impl.py"


def test_resolve_test_command_falls_back_to_tester(tmp_path: Path):
    from mas.tick import _resolve_test_command

    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "20260415-p-rtc2-aaaa")
    parent.mkdir(parents=True)
    subtasks = parent / "subtasks"
    subtasks.mkdir()
    plan = Plan(
        parent_id="20260415-p-rtc2-aaaa", summary="s",
        subtasks=[
            SubtaskSpec(id="20260415-test-1-aaaa", role="tester", goal="t"),
            SubtaskSpec(id="20260415-impl-1-aaaa", role="implementer", goal="i"),
        ],
    )
    d = subtasks / "20260415-test-1-aaaa"
    d.mkdir()
    (d / "result.json").write_text(
        Result(task_id="20260415-test-1-aaaa", status="success", summary="t",
               handoff={"test_command": "pytest tests/from_tester.py", "initial_exit_code": 1}
               ).model_dump_json()
    )
    impl_result = Result(task_id="20260415-impl-1-aaaa", status="success", summary="i",
                         handoff={"final_exit_code": 0})
    cmd = _resolve_test_command(plan, "20260415-impl-1-aaaa", subtasks, impl_result)
    assert cmd == "pytest tests/from_tester.py"


def test_resolve_test_command_returns_none_when_no_tester(tmp_path: Path):
    from mas.tick import _resolve_test_command

    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "20260415-p-rtc3-aaaa")
    parent.mkdir(parents=True)
    subtasks = parent / "subtasks"
    subtasks.mkdir()
    plan = Plan(
        parent_id="20260415-p-rtc3-aaaa", summary="s",
        subtasks=[SubtaskSpec(id="20260415-impl-1-aaaa", role="implementer", goal="i")],
    )
    impl_result = Result(task_id="20260415-impl-1-aaaa", status="success", summary="i",
                         handoff={"final_exit_code": 0})
    assert _resolve_test_command(plan, "20260415-impl-1-aaaa", subtasks, impl_result) is None


def test_collect_prior_results_empty_for_first_subtask(tmp_path: Path):
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "20260415-p-pr2-aaaa")
    parent.mkdir(parents=True)
    subtasks = parent / "subtasks"
    subtasks.mkdir()
    plan = Plan(
        parent_id="20260415-p-pr2-aaaa", summary="s",
        subtasks=[SubtaskSpec(id="20260415-test-1-aaaa", role="tester", goal="t")],
    )

    assert _collect_prior_results(plan, "20260415-test-1-aaaa", subtasks) == []


def test_dispatch_injects_prior_results_into_task_json(tmp_path: Path):
    """When the implementer is dispatched, its task.json carries tester's result."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "20260415-p-pr3-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-p-pr3-aaaa", role="orchestrator", goal="g"))
    (parent / "worktree").mkdir()
    plan = Plan(
        parent_id="20260415-p-pr3-aaaa", summary="s",
        subtasks=[
            SubtaskSpec(id="20260415-test-1-aaaa", role="tester", goal="t"),
            SubtaskSpec(id="20260415-impl-1-aaaa", role="implementer", goal="i"),
        ],
    )
    (parent / "plan.json").write_text(plan.model_dump_json())
    subtasks = parent / "subtasks"
    test_dir = subtasks / "20260415-test-1-aaaa"
    test_dir.mkdir(parents=True)
    (test_dir / "result.json").write_text(
        Result(task_id="20260415-test-1-aaaa", status="success", summary="failing tests authored",
               handoff={"test_command": "pytest -q", "test_files": ["tests/x.py"],
                        "initial_exit_code": 1, "expected_exit_code_after_impl": 0}
               ).model_dump_json()
    )
    impl_dir = subtasks / "20260415-impl-1-aaaa"
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
    assert impl_task.prior_results[0].task_id == "20260415-test-1-aaaa"
    assert impl_task.prior_results[0].handoff["test_command"] == "pytest -q"


def test_advance_writes_graph_json_with_nodes_and_sequence_edges(tmp_path: Path):
    """Advancing a parent with a fresh plan writes graph.json with one node
    per subtask and sequence edges between adjacent ones."""
    from mas.graph import read_graph

    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "20260504-p-gw-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260504-p-gw-aaaa", role="orchestrator", goal="g"))
    (parent / "worktree").mkdir()
    plan = Plan(
        parent_id="20260504-p-gw-aaaa", summary="s",
        subtasks=[
            SubtaskSpec(id="20260504-test-1-aaaa", role="tester", goal="t"),
            SubtaskSpec(id="20260504-impl-1-aaaa", role="implementer", goal="i"),
        ],
    )
    (parent / "plan.json").write_text(plan.model_dump_json())
    subtasks = parent / "subtasks"
    test_dir = subtasks / "20260504-test-1-aaaa"
    test_dir.mkdir(parents=True)
    (test_dir / "result.json").write_text(
        Result(task_id="20260504-test-1-aaaa", status="success", summary="green",
               handoff={"test_command": "pytest -q"}).model_dump_json()
    )

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())
    with patch("mas.tick.get_adapter") as mock_get, \
         patch("mas.board.count_active_pids", return_value=0), \
         patch("mas.board.write_pid"):
        mock_adapter = MagicMock()
        mock_adapter.dispatch.return_value = MagicMock(pid=1)
        mock_adapter.agentic = False
        mock_get.return_value.return_value = mock_adapter
        _advance_one(env, parent)

    g = read_graph(parent)
    ids = [n.subtask_id for n in g.nodes]
    assert ids == ["20260504-test-1-aaaa", "20260504-impl-1-aaaa"]
    # Backfill: tester's on-disk result is folded into its node on first sync.
    tester_node = next(n for n in g.nodes if n.subtask_id == "20260504-test-1-aaaa")
    assert tester_node.status == "success"
    assert tester_node.handoff == {"test_command": "pytest -q"}
    seq = [(e.from_id, e.to_id) for e in g.edges if e.kind == "sequence"]
    assert seq == [("20260504-test-1-aaaa", "20260504-impl-1-aaaa")]


def test_collect_prior_results_uses_graph_when_present(tmp_path: Path):
    """When graph.json is populated, `_collect_prior_results` derives priors
    from it (not from result.json on disk) so causality from revision edges
    is reflected in the returned Results."""
    from mas import graph as _graph

    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "20260504-p-cg-aaaa")
    parent.mkdir(parents=True)
    subtasks = parent / "subtasks"
    subtasks.mkdir()
    plan = Plan(
        parent_id="20260504-p-cg-aaaa", summary="s",
        subtasks=[
            SubtaskSpec(id="e-1", role="evaluator", goal="e"),
            SubtaskSpec(id="rev-1-implementer", role="implementer", goal="ri"),
            SubtaskSpec(id="rev-1-evaluator", role="evaluator", goal="re"),
        ],
    )

    g = _graph.Graph()
    _graph.sync_from_plan(g, plan)
    _graph.update_node_from_result(g, plan.subtasks[0],
        Result(task_id="e-1", status="success", verdict="needs_revision",
               summary="thin", feedback="add coverage for Y"))
    _graph.add_revision_link(
        g, from_evaluator_id="e-1",
        new_subtask_ids=["rev-1-implementer", "rev-1-evaluator"],
        feedback="add coverage for Y",
    )
    _graph.update_node_from_result(g, plan.subtasks[1],
        Result(task_id="rev-1-implementer", status="success", summary="patched"))
    _graph.write_graph(parent, g)

    priors = _collect_prior_results(
        plan, "rev-1-evaluator", subtasks, parent_dir=parent
    )
    impl = next(r for r in priors if r.task_id == "rev-1-implementer")
    assert impl.feedback is not None
    assert "[caused by e-1 (revision)" in impl.feedback
    assert "add coverage for Y" in impl.feedback


def test_trigger_replan_archives_graph_json(tmp_path: Path):
    """Replan moves graph.json → graph.replan-{N}.json so the next planning
    pass starts with a fresh graph."""
    from mas.graph import graph_path
    from mas.tick import _trigger_replan

    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent_id = "20260504-p-rp-aaaa"
    parent = board.task_dir(mas, "doing", parent_id)
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="g"))
    (parent / "plan.json").write_text("{}")
    (parent / "subtasks").mkdir()
    graph_path(parent).write_text('{"nodes": [], "edges": []}')

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())
    _trigger_replan(env, parent, board.read_task(parent), reason="r")

    assert not graph_path(parent).exists()
    assert (parent / "graph.replan-1.json").exists()


# ---------------------------------------------------------------------------
# 5. _handle_child_result
# ---------------------------------------------------------------------------

def test_handle_child_result_success_passthrough(tmp_path: Path):
    """Successful child result is a no-op."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = _seed_parent_with_plan(mas, "20260415-p-5-aaaa", "20260415-impl-1-aaaa")
    child = parent / "subtasks" / "20260415-impl-1-aaaa"
    r = Result(task_id="20260415-impl-1-aaaa", status="success", summary="ok")
    (child / "result.json").write_text(r.model_dump_json())

    plan = parse_plan(parent / "plan.json", "20260415-p-5-aaaa")
    spec = plan.subtasks[0]
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    _handle_child_result(env, parent, board.read_task(parent), plan, spec, r)

    assert parent.exists()


def test_handle_child_result_failure_bumps_attempt(tmp_path: Path):
    """Failed child below max_retries → bump attempt, rename result, write previous_failure."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = _seed_parent_with_plan(mas, "20260415-p-6-aaaa", "20260415-impl-1-aaaa")
    child = parent / "subtasks" / "20260415-impl-1-aaaa"
    (child / ".attempt").write_text("1")
    r = Result(task_id="20260415-impl-1-aaaa", status="failure", summary="oops")
    (child / "result.json").write_text(r.model_dump_json())

    plan = parse_plan(parent / "plan.json", "20260415-p-6-aaaa")
    spec = plan.subtasks[0]
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(max_retries=2))

    _handle_child_result(env, parent, board.read_task(parent), plan, spec, r)

    assert (child / "result.failed-1.json").exists()
    assert not (child / "result.json").exists()
    assert (child / ".attempt").read_text().strip() == "2"
    assert (child / ".previous_failure").exists()


def test_handle_child_result_env_error_does_not_bump_attempt(tmp_path: Path):
    """environment_error does not consume retry budget — same attempt is redispatched."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = _seed_parent_with_plan(mas, "20260423-p-env-aaaa", "20260423-impl-1-aaaa")
    child = parent / "subtasks" / "20260423-impl-1-aaaa"
    (child / ".attempt").write_text("1")
    (child / "logs").mkdir()
    (child / "logs" / "implementer-1.log").write_text("blocked by the sandbox")
    r = Result(task_id="20260423-impl-1-aaaa", status="environment_error", summary="sandbox")
    (child / "result.json").write_text(r.model_dump_json())

    plan = parse_plan(parent / "plan.json", "20260423-p-env-aaaa")
    spec = plan.subtasks[0]
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(max_retries=2))

    _handle_child_result(env, parent, board.read_task(parent), plan, spec, r)

    # Attempt counter NOT bumped; result stashed as env-1, not failed-1.
    assert (child / ".attempt").read_text().strip() == "1"
    assert not (child / "result.failed-1.json").exists()
    assert (child / "result.env-1.json").exists()
    assert not (child / "result.json").exists()
    assert (child / ".env_retries").read_text().strip() == "1"
    assert (child / ".previous_failure").exists()


def test_handle_child_result_env_error_caps_after_three(tmp_path: Path):
    """After 3 env retries, env-error falls through to normal failure handling."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = _seed_parent_with_plan(mas, "20260423-p-envc-aaaa", "20260423-impl-1-aaaa")
    child = parent / "subtasks" / "20260423-impl-1-aaaa"
    (child / ".attempt").write_text("1")
    (child / ".env_retries").write_text("3")
    r = Result(task_id="20260423-impl-1-aaaa", status="environment_error", summary="sandbox")
    (child / "result.json").write_text(r.model_dump_json())

    plan = parse_plan(parent / "plan.json", "20260423-p-envc-aaaa")
    spec = plan.subtasks[0]
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(max_retries=2))

    _handle_child_result(env, parent, board.read_task(parent), plan, spec, r)

    # Fell through: attempt bumped as normal failure.
    assert (child / ".attempt").read_text().strip() == "2"
    assert (child / "result.failed-1.json").exists()


def test_handle_child_result_failure_max_retries_moves_parent(tmp_path: Path):
    """Failed child at max_retries → parent moved to failed/."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = _seed_parent_with_plan(mas, "20260415-p-7-aaaa", "20260415-impl-1-aaaa")
    child = parent / "subtasks" / "20260415-impl-1-aaaa"
    (child / ".attempt").write_text("3")
    r = Result(task_id="20260415-impl-1-aaaa", status="failure", summary="still failing")
    (child / "result.json").write_text(r.model_dump_json())

    plan = parse_plan(parent / "plan.json", "20260415-p-7-aaaa")
    spec = plan.subtasks[0]
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(max_retries=2))

    _handle_child_result(env, parent, board.read_task(parent), plan, spec, r)

    assert not parent.exists()
    assert (mas / "tasks" / "failed" / "20260415-p-7-aaaa").exists()


def test_handle_child_result_evaluator_needs_revision(tmp_path: Path):
    """Evaluator verdict=needs_revision → appends revision cycle subtasks."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "20260415-p-8-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-p-8-aaaa", role="orchestrator", goal="g"))
    subtasks = parent / "subtasks"
    subtasks.mkdir()
    plan = Plan(
        parent_id="20260415-p-8-aaaa",
        summary="s",
        subtasks=[SubtaskSpec(id="20260415-eval-1-aaaa", role="evaluator", goal="eval")],
    )
    (parent / "plan.json").write_text(plan.model_dump_json())
    child = subtasks / "20260415-eval-1-aaaa"
    child.mkdir()
    r = Result(task_id="20260415-eval-1-aaaa", status="needs_revision", summary="revise",
               verdict="needs_revision", feedback="fix it")
    (child / "result.json").write_text(r.model_dump_json())

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    _handle_child_result(env, parent, board.read_task(parent), plan, plan.subtasks[0], r)

    updated_plan = parse_plan(parent / "plan.json", "20260415-p-8-aaaa")
    assert len(updated_plan.subtasks) == 4
    assert any(s.id == "rev-1-implementer" for s in updated_plan.subtasks)


# ---------------------------------------------------------------------------
# 6. _all_children_passed
# ---------------------------------------------------------------------------

def test_all_children_passed_true(tmp_path: Path):
    """All children successful → True."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "20260415-p-9-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-p-9-aaaa", role="orchestrator", goal="g"))
    subtasks = parent / "subtasks"
    subtasks.mkdir()
    plan = Plan(
        parent_id="20260415-p-9-aaaa", summary="s",
        subtasks=[
            SubtaskSpec(id="20260415-impl-1-aaaa", role="implementer", goal="do"),
            SubtaskSpec(id="20260415-impl-2-aaaa", role="implementer", goal="do2"),
        ],
    )
    (parent / "plan.json").write_text(plan.model_dump_json())
    for tid in ["20260415-impl-1-aaaa", "20260415-impl-2-aaaa"]:
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
    parent = board.task_dir(mas, "doing", "20260415-p-10-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-p-10-aaaa", role="orchestrator", goal="g"))
    subtasks = parent / "subtasks"
    subtasks.mkdir()
    plan = Plan(parent_id="20260415-p-10-aaaa", summary="s",
                subtasks=[SubtaskSpec(id="20260415-impl-1-aaaa", role="implementer", goal="do")])

    result = _all_children_passed(plan, subtasks)
    assert result is False


def test_next_ready_child_skips_needs_revision_eval_after_cycle_spawned(tmp_path: Path):
    """Evaluator with needs_revision verdict should be skipped once a revision
    cycle has been appended. Otherwise tick loops forever on the same evaluator."""
    from mas.tick import _next_ready_child

    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "20260423-p-nrc-aaaa")
    parent.mkdir(parents=True)
    subtasks = parent / "subtasks"
    subtasks.mkdir()
    plan = Plan(
        parent_id="20260423-p-nrc-aaaa", summary="s",
        subtasks=[
            SubtaskSpec(id="eval-1", role="evaluator", goal="eval"),
            SubtaskSpec(id="rev-1-tester", role="tester", goal="t",
                        inputs={"feedback_cycle": "rev-1"}),
            SubtaskSpec(id="rev-1-implementer", role="implementer", goal="i",
                        inputs={"feedback_cycle": "rev-1"}),
            SubtaskSpec(id="rev-1-evaluator", role="evaluator", goal="e",
                        inputs={"feedback_cycle": "rev-1"}),
        ],
        revision_feedback={"rev-1": "fb"},
    )
    e = subtasks / "eval-1"
    e.mkdir()
    (e / "result.json").write_text(
        Result(task_id="eval-1", status="needs_revision", summary="r",
               verdict="needs_revision", feedback="fb").model_dump_json()
    )

    nxt = _next_ready_child(plan, subtasks)
    assert nxt is not None
    assert nxt.id == "rev-1-tester"


def test_all_children_passed_ignores_superseded_needs_revision_eval(tmp_path: Path):
    """An eval that needs_revision + has a successor cycle should not block finalization
    once the later evaluator passes."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "20260423-p-acp-aaaa")
    parent.mkdir(parents=True)
    subtasks = parent / "subtasks"
    subtasks.mkdir()
    plan = Plan(
        parent_id="20260423-p-acp-aaaa", summary="s",
        subtasks=[
            SubtaskSpec(id="eval-1", role="evaluator", goal="eval"),
            SubtaskSpec(id="rev-1-tester", role="tester", goal="t"),
            SubtaskSpec(id="rev-1-implementer", role="implementer", goal="i"),
            SubtaskSpec(id="rev-1-evaluator", role="evaluator", goal="e"),
        ],
    )
    for tid, status, verdict in [
        ("eval-1", "needs_revision", "needs_revision"),
        ("rev-1-tester", "success", None),
        ("rev-1-implementer", "success", None),
        ("rev-1-evaluator", "success", "pass"),
    ]:
        d = subtasks / tid
        d.mkdir()
        (d / "result.json").write_text(
            Result(task_id=tid, status=status, summary="s", verdict=verdict).model_dump_json()
        )

    assert _all_children_passed(plan, subtasks) is True


def test_all_children_passed_evaluator_verdict_fail(tmp_path: Path):
    """Evaluator result with verdict!=pass → False."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "20260415-p-11-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-p-11-aaaa", role="orchestrator", goal="g"))
    subtasks = parent / "subtasks"
    subtasks.mkdir()
    plan = Plan(parent_id="20260415-p-11-aaaa", summary="s",
                subtasks=[SubtaskSpec(id="20260415-eval-1-aaaa", role="evaluator", goal="eval")])
    (parent / "plan.json").write_text(plan.model_dump_json())
    d = subtasks / "20260415-eval-1-aaaa"
    d.mkdir()
    (d / "result.json").write_text(
        Result(task_id="20260415-eval-1-aaaa", status="success", summary="ok", verdict="fail").model_dump_json()
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
    parent = board.task_dir(mas, "doing", "20260415-p-12-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-p-12-aaaa", role="orchestrator", goal="g"))
    subtasks = parent / "subtasks"
    subtasks.mkdir()
    plan = Plan(parent_id="20260415-p-12-aaaa", summary="s",
                subtasks=[SubtaskSpec(id="20260415-eval-1-aaaa", role="evaluator", goal="eval")],
                max_revision_cycles=2)
    (parent / "plan.json").write_text(plan.model_dump_json())

    _append_revision_cycle(parent, plan, board.read_task(parent), "fix bugs")

    updated = parse_plan(parent / "plan.json", "20260415-p-12-aaaa")
    assert len(updated.subtasks) == 4
    ids = {s.id for s in updated.subtasks}
    assert "rev-1-implementer" in ids
    assert "rev-1-tester" in ids
    assert "rev-1-evaluator" in ids


def test_append_revision_cycle_orders_tester_before_implementer(tmp_path: Path):
    """Under TDD the revision cycle is tester → implementer → evaluator."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "20260415-p-12b-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-p-12b-aaaa", role="orchestrator", goal="g"))
    (parent / "subtasks").mkdir()
    plan = Plan(parent_id="20260415-p-12b-aaaa", summary="s",
                subtasks=[SubtaskSpec(id="20260415-eval-1-aaaa", role="evaluator", goal="eval")],
                max_revision_cycles=2)
    (parent / "plan.json").write_text(plan.model_dump_json())

    _append_revision_cycle(parent, plan, board.read_task(parent), "fix it")

    updated = parse_plan(parent / "plan.json", "20260415-p-12b-aaaa")
    rev = [s for s in updated.subtasks if s.id.startswith("rev-1-")]
    assert [s.role for s in rev] == ["tester", "implementer", "evaluator"]


def test_append_revision_cycle_stores_feedback_once(tmp_path: Path):
    """Feedback is stored once on plan.revision_feedback, not duplicated
    across the three rev-N subtask inputs."""
    from mas.tick import _resolve_feedback_ref

    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "20260423-p-fb1-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260423-p-fb1-aaaa", role="orchestrator", goal="g"))
    (parent / "subtasks").mkdir()
    plan = Plan(parent_id="20260423-p-fb1-aaaa", summary="s",
                subtasks=[SubtaskSpec(id="20260423-eval-1-aaaa", role="evaluator", goal="eval")],
                max_revision_cycles=2)
    (parent / "plan.json").write_text(plan.model_dump_json())

    big_feedback = "x" * 4000
    _append_revision_cycle(parent, plan, board.read_task(parent), big_feedback)

    updated = parse_plan(parent / "plan.json", "20260423-p-fb1-aaaa")
    assert updated.revision_feedback["rev-1"] == big_feedback
    rev = [s for s in updated.subtasks if s.id.startswith("rev-1-")]
    assert len(rev) == 3
    for s in rev:
        assert "feedback" not in s.inputs, "feedback text must not be duplicated in subtask inputs"
        assert s.inputs["feedback_cycle"] == "rev-1"

    # At dispatch time, _resolve_feedback_ref turns the ref back into real feedback.
    resolved = _resolve_feedback_ref(rev[0].inputs, updated)
    assert resolved["feedback"] == big_feedback
    assert "feedback_cycle" not in resolved


def test_append_revision_cycle_respects_max_cap(tmp_path: Path):
    """At max_revision_cycles, no new subtasks are added."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "20260415-p-13-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-p-13-aaaa", role="orchestrator", goal="g"))
    subtasks = parent / "subtasks"
    subtasks.mkdir()
    plan = Plan(
        parent_id="20260415-p-13-aaaa", summary="s",
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

    appended = _append_revision_cycle(parent, plan, board.read_task(parent), "more feedback")

    updated = parse_plan(parent / "plan.json", "20260415-p-13-aaaa")
    assert len(updated.subtasks) == 6
    assert appended is False


def test_append_revision_cycle_returns_true_when_appended(tmp_path: Path):
    """Returns True when a new cycle is appended."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "20260430-p-rt-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260430-p-rt-aaaa", role="orchestrator", goal="g"))
    (parent / "subtasks").mkdir()
    plan = Plan(parent_id="20260430-p-rt-aaaa", summary="s",
                subtasks=[SubtaskSpec(id="20260430-eval-1-aaaa", role="evaluator", goal="eval")],
                max_revision_cycles=2)
    (parent / "plan.json").write_text(plan.model_dump_json())

    appended = _append_revision_cycle(parent, plan, board.read_task(parent), "fb")
    assert appended is True


def test_append_revision_cycle_writes_graph_with_revision_edges(tmp_path: Path):
    """Appending a revision cycle records `revision` edges from the failing
    evaluator to each new subtask, with feedback as the edge reason."""
    from mas.graph import read_graph

    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "20260504-p-gr-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260504-p-gr-aaaa", role="orchestrator", goal="g"))
    (parent / "subtasks").mkdir()
    plan = Plan(parent_id="20260504-p-gr-aaaa", summary="s",
                subtasks=[SubtaskSpec(id="20260504-eval-1-aaaa", role="evaluator", goal="e")],
                max_revision_cycles=2)
    (parent / "plan.json").write_text(plan.model_dump_json())

    feedback = "missing edge case Y"
    _append_revision_cycle(parent, plan, board.read_task(parent), feedback)

    g = read_graph(parent)
    rev_edges = [(e.from_id, e.to_id, e.reason) for e in g.edges if e.kind == "revision"]
    assert ("20260504-eval-1-aaaa", "rev-1-tester", feedback) in rev_edges
    assert ("20260504-eval-1-aaaa", "rev-1-implementer", feedback) in rev_edges
    assert ("20260504-eval-1-aaaa", "rev-1-evaluator", feedback) in rev_edges
    # Sequence edges synced from plan order.
    seq_edges = {(e.from_id, e.to_id) for e in g.edges if e.kind == "sequence"}
    assert ("rev-1-tester", "rev-1-implementer") in seq_edges
    assert ("rev-1-implementer", "rev-1-evaluator") in seq_edges


def test_handle_child_result_moves_parent_to_failed_when_cycles_exhausted(tmp_path: Path):
    """Evaluator needs_revision after max_revision_cycles → parent moves to failed/."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent_id = "20260430-p-ex-aaaa"
    parent = board.task_dir(mas, "doing", parent_id)
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="g"))
    subtasks = parent / "subtasks"
    subtasks.mkdir()

    final_eval_id = "rev-2-evaluator"
    final_eval_dir = subtasks / final_eval_id
    final_eval_dir.mkdir()

    plan = Plan(
        parent_id=parent_id, summary="s",
        subtasks=[
            SubtaskSpec(id="rev-1-tester", role="tester", goal="r1t"),
            SubtaskSpec(id="rev-1-implementer", role="implementer", goal="r1"),
            SubtaskSpec(id="rev-1-evaluator", role="evaluator", goal="r1e"),
            SubtaskSpec(id="rev-2-tester", role="tester", goal="r2t"),
            SubtaskSpec(id="rev-2-implementer", role="implementer", goal="r2"),
            SubtaskSpec(id=final_eval_id, role="evaluator", goal="r2e"),
        ],
        max_revision_cycles=2,
    )
    (parent / "plan.json").write_text(plan.model_dump_json())

    final_spec = next(s for s in plan.subtasks if s.id == final_eval_id)
    result = Result(
        task_id=final_eval_id,
        status="needs_revision",
        summary="still broken",
        verdict="needs_revision",
        feedback="not converging",
    )

    cfg = MasConfig(
        providers={"mock": ProviderConfig(cli="sh", max_concurrent=1)},
        roles={
            "proposer": RoleConfig(provider="mock"),
            "orchestrator": RoleConfig(provider="mock"),
            "implementer": RoleConfig(provider="mock"),
            "tester": RoleConfig(provider="mock"),
            "evaluator": RoleConfig(provider="mock"),
        },
        max_replans=0,
    )
    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)

    _handle_child_result(env, parent, board.read_task(parent), plan, final_spec, result)

    assert not parent.exists(), "parent should have moved out of doing/"
    failed_dir = mas / "tasks" / "failed" / parent_id
    assert failed_dir.exists(), "parent should land in failed/"
    txns = transitions.read_transitions(failed_dir)
    assert any(t.reason == "revision_cycles_exhausted" for t in txns)


# ---------------------------------------------------------------------------
# 7b. Replan trigger
# ---------------------------------------------------------------------------


def _replan_plan(parent_id: str, *, max_revision_cycles: int = 2) -> Plan:
    return Plan(
        parent_id=parent_id, summary="s",
        subtasks=[
            SubtaskSpec(id="test-1", role="tester", goal="t"),
            SubtaskSpec(id="impl-1", role="implementer", goal="i"),
            SubtaskSpec(id="eval-1", role="evaluator", goal="e"),
            SubtaskSpec(id="rev-1-tester", role="tester", goal="r1t"),
            SubtaskSpec(id="rev-1-implementer", role="implementer", goal="r1i"),
            SubtaskSpec(id="rev-1-evaluator", role="evaluator", goal="r1e"),
        ],
        max_revision_cycles=max_revision_cycles,
    )


def test_replan_triggers_when_at_threshold(tmp_path: Path):
    """After max_revision_cycles - 1 cycles fail, replan re-dispatches orchestrator."""
    from mas.tick import _trigger_replan

    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent_id = "20260430-p-rp1-aaaa"
    parent = board.task_dir(mas, "doing", parent_id)
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="g"))
    subtasks = parent / "subtasks"
    subtasks.mkdir()
    eval_dir = subtasks / "rev-1-evaluator"
    eval_dir.mkdir()

    plan = _replan_plan(parent_id)
    (parent / "plan.json").write_text(plan.model_dump_json())
    (parent / "result.json").write_text(
        Result(task_id=parent_id, status="success", summary="orch done").model_dump_json()
    )

    spec = next(s for s in plan.subtasks if s.id == "rev-1-evaluator")
    result = Result(
        task_id="rev-1-evaluator", status="needs_revision",
        summary="not converging", verdict="needs_revision", feedback="try a new strategy",
    )

    cfg = _cfg()
    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)

    _handle_child_result(env, parent, board.read_task(parent), plan, spec, result)

    # plan.json archived, subtasks/ archived, .replan_count bumped, .orchestrator_attempt bumped
    assert not (parent / "plan.json").exists()
    assert (parent / "plan.replan-1.json").exists()
    assert not (parent / "subtasks").exists()
    assert (parent / "subtasks.replan-1").exists()
    assert (parent / "result.replan-1.json").exists()
    assert (parent / ".replan_count").read_text().strip() == "1"
    assert (parent / ".orchestrator_attempt").read_text().strip() == "2"

    # Parent task.json updated with replan_reason in inputs
    refreshed = board.read_task(parent)
    assert refreshed.inputs.get("replan_reason") == "try a new strategy"

    # Parent stays in doing/ — next tick will re-dispatch orchestrator
    assert parent.exists()


def test_replan_skipped_when_max_replans_zero(tmp_path: Path):
    """max_replans=0 disables replan; cycles exhaust normally to failed/."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent_id = "20260430-p-rp2-aaaa"
    parent = board.task_dir(mas, "doing", parent_id)
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="g"))
    (parent / "subtasks").mkdir()

    plan = _replan_plan(parent_id)
    (parent / "plan.json").write_text(plan.model_dump_json())

    spec = next(s for s in plan.subtasks if s.id == "rev-1-evaluator")
    result = Result(
        task_id="rev-1-evaluator", status="needs_revision",
        summary="x", verdict="needs_revision", feedback="fb",
    )

    cfg = _cfg()
    cfg.max_replans = 0
    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)

    _handle_child_result(env, parent, board.read_task(parent), plan, spec, result)

    # No replan files written; plan.json still present (a new revision cycle was appended)
    assert not (parent / ".replan_count").exists()
    assert (parent / "plan.json").exists()


def test_replan_not_triggered_on_first_revision(tmp_path: Path):
    """First failing eval (existing_cycles=0) gets a normal cycle, not a replan."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent_id = "20260430-p-rp3-aaaa"
    parent = board.task_dir(mas, "doing", parent_id)
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="g"))
    (parent / "subtasks").mkdir()

    plan = Plan(
        parent_id=parent_id, summary="s",
        subtasks=[
            SubtaskSpec(id="test-1", role="tester", goal="t"),
            SubtaskSpec(id="impl-1", role="implementer", goal="i"),
            SubtaskSpec(id="eval-1", role="evaluator", goal="e"),
        ],
        max_revision_cycles=2,
    )
    (parent / "plan.json").write_text(plan.model_dump_json())

    spec = next(s for s in plan.subtasks if s.id == "eval-1")
    result = Result(
        task_id="eval-1", status="needs_revision",
        summary="x", verdict="needs_revision", feedback="fb",
    )

    cfg = _cfg()
    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)

    _handle_child_result(env, parent, board.read_task(parent), plan, spec, result)

    # rev-1-* appended; no replan
    assert not (parent / ".replan_count").exists()
    refreshed_plan = parse_plan(parent / "plan.json", parent_id)
    assert any(s.id.startswith("rev-1-") for s in refreshed_plan.subtasks)


def test_replan_respects_max_replans_cap(tmp_path: Path):
    """Once .replan_count >= max_replans, falls back to normal revision/exhaustion path."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent_id = "20260430-p-rp4-aaaa"
    parent = board.task_dir(mas, "doing", parent_id)
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="g"))
    (parent / "subtasks").mkdir()
    (parent / ".replan_count").write_text("1")  # already used the budget

    plan = _replan_plan(parent_id)
    (parent / "plan.json").write_text(plan.model_dump_json())

    spec = next(s for s in plan.subtasks if s.id == "rev-1-evaluator")
    result = Result(
        task_id="rev-1-evaluator", status="needs_revision",
        summary="x", verdict="needs_revision", feedback="fb",
    )

    cfg = _cfg()  # max_replans defaults to 1
    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)

    _handle_child_result(env, parent, board.read_task(parent), plan, spec, result)

    # No new replan; rev-2 cycle appended instead
    assert (parent / ".replan_count").read_text().strip() == "1"
    refreshed_plan = parse_plan(parent / "plan.json", parent_id)
    assert any(s.id.startswith("rev-2-") for s in refreshed_plan.subtasks)


# ---------------------------------------------------------------------------
# 7c. Convergence detector
# ---------------------------------------------------------------------------


def test_jaccard_similarity_basic():
    from mas.tick import _jaccard_similarity

    assert _jaccard_similarity("foo bar baz", "foo bar baz") == 1.0
    assert _jaccard_similarity("foo bar", "qux quux") == 0.0
    assert _jaccard_similarity("", "anything") == 0.0
    assert _jaccard_similarity("Foo Bar", "foo bar") == 1.0  # case-insensitive
    assert 0.3 < _jaccard_similarity("foo bar baz", "foo bar qux") < 0.8


def test_detect_convergence_empty_revision_feedback():
    from mas.tick import _detect_convergence

    plan = Plan(parent_id="p", summary="s", subtasks=[])
    converged, sim = _detect_convergence(plan, "anything")
    assert converged is False
    assert sim == 0.0


def test_detect_convergence_picks_latest_cycle_key():
    from mas.tick import _detect_convergence

    plan = Plan(
        parent_id="p", summary="s", subtasks=[],
        revision_feedback={
            "rev-1": "totally different feedback about widgets",
            "rev-2": "missing return type on foo bar baz handler",
        },
    )
    # Compares against rev-2, not rev-1.
    converged, sim = _detect_convergence(plan, "missing return type on foo bar baz handler")
    assert converged is True
    assert sim == 1.0


def test_handle_child_result_convergence_triggers_replan(tmp_path: Path):
    """Eval feedback ~identical to last cycle's → replan (when budget allows)."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent_id = "20260430-p-cv1-aaaa"
    parent = board.task_dir(mas, "doing", parent_id)
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="g"))
    subtasks = parent / "subtasks"
    subtasks.mkdir()
    (subtasks / "rev-1-evaluator").mkdir()

    plan = Plan(
        parent_id=parent_id, summary="s",
        subtasks=[
            SubtaskSpec(id="test-1", role="tester", goal="t"),
            SubtaskSpec(id="impl-1", role="implementer", goal="i"),
            SubtaskSpec(id="eval-1", role="evaluator", goal="e"),
            SubtaskSpec(id="rev-1-tester", role="tester", goal="r1t"),
            SubtaskSpec(id="rev-1-implementer", role="implementer", goal="r1i"),
            SubtaskSpec(id="rev-1-evaluator", role="evaluator", goal="r1e"),
        ],
        max_revision_cycles=4,  # high enough that replan-threshold won't fire
        revision_feedback={"rev-1": "missing null check on user input field"},
    )
    (parent / "plan.json").write_text(plan.model_dump_json())
    (parent / "result.json").write_text(
        Result(task_id=parent_id, status="success", summary="orch done").model_dump_json()
    )

    spec = next(s for s in plan.subtasks if s.id == "rev-1-evaluator")
    result = Result(
        task_id="rev-1-evaluator", status="needs_revision",
        summary="still broken", verdict="needs_revision",
        feedback="missing null check on user input field",  # identical → 1.0
    )

    cfg = _cfg()  # max_replans=1 default
    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)

    _handle_child_result(env, parent, board.read_task(parent), plan, spec, result)

    # Replan triggered: archived files, replan_count bumped, parent task carries reason
    assert (parent / ".replan_count").read_text().strip() == "1"
    assert (parent / "plan.replan-1.json").exists()
    refreshed = board.read_task(parent)
    assert "convergence_detected" in refreshed.inputs.get("replan_reason", "")


def test_handle_child_result_convergence_fails_when_replan_exhausted(tmp_path: Path):
    """Convergence with replan budget exhausted → parent moves to failed/."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent_id = "20260430-p-cv2-aaaa"
    parent = board.task_dir(mas, "doing", parent_id)
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="g"))
    (parent / "subtasks" / "rev-1-evaluator").mkdir(parents=True)
    (parent / ".replan_count").write_text("1")  # budget used

    plan = Plan(
        parent_id=parent_id, summary="s",
        subtasks=[
            SubtaskSpec(id="test-1", role="tester", goal="t"),
            SubtaskSpec(id="impl-1", role="implementer", goal="i"),
            SubtaskSpec(id="eval-1", role="evaluator", goal="e"),
            SubtaskSpec(id="rev-1-tester", role="tester", goal="r1t"),
            SubtaskSpec(id="rev-1-implementer", role="implementer", goal="r1i"),
            SubtaskSpec(id="rev-1-evaluator", role="evaluator", goal="r1e"),
        ],
        max_revision_cycles=4,
        revision_feedback={"rev-1": "tests still failing on edge case for empty list"},
    )
    (parent / "plan.json").write_text(plan.model_dump_json())

    spec = next(s for s in plan.subtasks if s.id == "rev-1-evaluator")
    result = Result(
        task_id="rev-1-evaluator", status="needs_revision",
        summary="still broken", verdict="needs_revision",
        feedback="tests still failing on edge case for empty list",
    )

    cfg = _cfg()  # max_replans=1; already used
    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)

    _handle_child_result(env, parent, board.read_task(parent), plan, spec, result)

    assert not parent.exists()
    failed_dir = mas / "tasks" / "failed" / parent_id
    assert failed_dir.exists()
    txns = transitions.read_transitions(failed_dir)
    assert any("convergence_detected" in t.reason for t in txns)


def test_handle_child_result_no_convergence_appends_normal_cycle(tmp_path: Path):
    """Dissimilar feedback → falls through to normal revision cycle append."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent_id = "20260430-p-cv3-aaaa"
    parent = board.task_dir(mas, "doing", parent_id)
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="g"))
    (parent / "subtasks" / "rev-1-evaluator").mkdir(parents=True)

    plan = Plan(
        parent_id=parent_id, summary="s",
        subtasks=[
            SubtaskSpec(id="test-1", role="tester", goal="t"),
            SubtaskSpec(id="impl-1", role="implementer", goal="i"),
            SubtaskSpec(id="eval-1", role="evaluator", goal="e"),
            SubtaskSpec(id="rev-1-tester", role="tester", goal="r1t"),
            SubtaskSpec(id="rev-1-implementer", role="implementer", goal="r1i"),
            SubtaskSpec(id="rev-1-evaluator", role="evaluator", goal="r1e"),
        ],
        max_revision_cycles=4,
        revision_feedback={"rev-1": "missing null check on user input field"},
    )
    (parent / "plan.json").write_text(plan.model_dump_json())

    spec = next(s for s in plan.subtasks if s.id == "rev-1-evaluator")
    result = Result(
        task_id="rev-1-evaluator", status="needs_revision",
        summary="now another issue", verdict="needs_revision",
        feedback="completely separate concern about pagination ordering",
    )

    cfg = _cfg()
    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)

    _handle_child_result(env, parent, board.read_task(parent), plan, spec, result)

    # Normal append path: no replan, plan.json still present, rev-2 cycle added
    assert not (parent / ".replan_count").exists()
    assert (parent / "plan.json").exists()
    refreshed_plan = parse_plan(parent / "plan.json", parent_id)
    assert any(s.id.startswith("rev-2-") for s in refreshed_plan.subtasks)


def test_handle_child_result_first_revision_no_convergence(tmp_path: Path):
    """First failing eval has no prior cycle → convergence skipped, normal append."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent_id = "20260430-p-cv4-aaaa"
    parent = board.task_dir(mas, "doing", parent_id)
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="g"))
    (parent / "subtasks").mkdir()

    plan = Plan(
        parent_id=parent_id, summary="s",
        subtasks=[
            SubtaskSpec(id="test-1", role="tester", goal="t"),
            SubtaskSpec(id="impl-1", role="implementer", goal="i"),
            SubtaskSpec(id="eval-1", role="evaluator", goal="e"),
        ],
        max_revision_cycles=2,
    )
    (parent / "plan.json").write_text(plan.model_dump_json())

    spec = next(s for s in plan.subtasks if s.id == "eval-1")
    result = Result(
        task_id="eval-1", status="needs_revision",
        summary="x", verdict="needs_revision", feedback="any feedback at all",
    )

    cfg = _cfg()
    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)

    _handle_child_result(env, parent, board.read_task(parent), plan, spec, result)

    assert not (parent / ".replan_count").exists()
    refreshed_plan = parse_plan(parent / "plan.json", parent_id)
    assert any(s.id.startswith("rev-1-") for s in refreshed_plan.subtasks)


# ---------------------------------------------------------------------------
# 8. _finalize_parent
# ---------------------------------------------------------------------------

def test_finalize_parent_moves_to_done(tmp_path: Path):
    """_finalize_parent moves parent to done/ and prunes."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "20260415-p-14-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260415-p-14-aaaa", role="orchestrator", goal="g"))
    wt = parent / "worktree"
    wt.mkdir()

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with patch.object(wt_module, "commit_changes") as mock_commit, \
         patch.object(wt_module, "prune") as mock_prune:
        _finalize_parent(env, parent, board.read_task(parent))

    mock_commit.assert_called_once()
    mock_prune.assert_called_once()
    assert not parent.exists()
    assert (mas / "tasks" / "done" / "20260415-p-14-aaaa").exists()


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


def test_materialize_proposal_writes_human_readable_doc(tmp_path: Path):
    """Materialized proposal includes a task.md with goal, rationale, acceptance."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    result = Result(
        task_id="prop-doc",
        status="success",
        summary="build a thing",
        handoff={
            "goal": "build a new widget",
            "rationale": "users keep asking for widgets",
            "acceptance": ["widget renders", "widget persists"],
            "suggested_changes": ["add Widget model", "expose /widgets endpoint"],
        },
    )

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(max_proposed=5))
    _materialize_proposal(env, result)

    (proposed,) = list((mas / "tasks" / "proposed").iterdir())
    doc = (proposed / "task.md").read_text()
    assert "# build a new widget" in doc
    assert "Task ID" in doc and proposed.name in doc
    assert "## Rationale" in doc
    assert "users keep asking for widgets" in doc
    assert "## Acceptance criteria" in doc
    assert "- widget renders" in doc
    assert "## Suggested changes" in doc
    assert "- add Widget model" in doc


def test_materialize_proposal_doc_omits_empty_sections(tmp_path: Path):
    """task.md only includes sections for fields that are present."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    result = Result(
        task_id="prop-minimal",
        status="success",
        summary="t",
        handoff={"goal": "just a goal"},
    )

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(max_proposed=5))
    _materialize_proposal(env, result)

    (proposed,) = list((mas / "tasks" / "proposed").iterdir())
    doc = (proposed / "task.md").read_text()
    assert "# just a goal" in doc
    assert "## Rationale" not in doc
    assert "## Acceptance criteria" not in doc
    assert "## Suggested changes" not in doc


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


def test_materialize_proposal_drops_duplicate(tmp_path: Path, caplog):
    """A near-duplicate of an existing goal must not be materialized."""
    import json as _json

    mas = tmp_path / ".mas"
    board.ensure_layout(mas)

    # Seed an existing proposed task.
    existing = mas / "tasks" / "proposed" / "20260101-existing-abcd"
    existing.mkdir(parents=True)
    (existing / "task.json").write_text(_json.dumps({
        "id": "20260101-existing-abcd",
        "role": "orchestrator",
        "goal": "Create an MCP tool that returns budget utilization metrics",
    }))

    result = Result(
        task_id="prop-dup",
        status="success",
        summary="dup",
        handoff={"goal": "Create an MCP tool that returns conversion tracking metrics"},
    )

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(
        max_proposed=10, proposal_similarity_threshold=0.5,
    ))

    with caplog.at_level(logging.INFO):
        _materialize_proposal(env, result)

    # Only the seed remains; no new task was created.
    assert len(list((mas / "tasks" / "proposed").iterdir())) == 1


def test_materialize_proposal_allows_distinct_goal(tmp_path: Path):
    """A genuinely different goal still materializes even with existing proposals."""
    import json as _json

    mas = tmp_path / ".mas"
    board.ensure_layout(mas)

    existing = mas / "tasks" / "proposed" / "20260101-existing-abcd"
    existing.mkdir(parents=True)
    (existing / "task.json").write_text(_json.dumps({
        "id": "20260101-existing-abcd",
        "role": "orchestrator",
        "goal": "Create an MCP tool that returns budget utilization metrics",
    }))

    result = Result(
        task_id="prop-distinct",
        status="success",
        summary="distinct",
        handoff={"goal": "Refactor worktree pruning to handle detached HEAD"},
    )

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(max_proposed=10))
    _materialize_proposal(env, result)

    assert len(list((mas / "tasks" / "proposed").iterdir())) == 2


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
    board.task_dir(mas, "proposed", "20260415-existing-aaaa").mkdir(parents=True)
    board.write_task(board.task_dir(mas, "proposed", "20260415-existing-aaaa"),
                     Task(id="20260415-existing-aaaa", role="orchestrator", goal="g"))

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
    board.task_dir(mas, "doing", "20260415-proposer-running-aaaa").mkdir(parents=True)
    board.write_task(board.task_dir(mas, "doing", "20260415-proposer-running-aaaa"),
                     Task(id="20260415-proposer-running-aaaa", role="proposer", goal="p"))

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
    td = mas / "tasks" / "doing" / "20260415-t1-aaaa"
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
    td = mas / "tasks" / "doing" / "20260415-t2-aaaa"
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
         patch("mas.tick.validate_config", return_value=[]), \
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
         patch("mas.tick.validate_config", return_value=[]), \
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


# ---------------------------------------------------------------------------
# Cost aggregation in _finalize_parent
# ---------------------------------------------------------------------------

def test_finalize_parent_aggregates_child_costs(tmp_path: Path):
    """_finalize_parent must write result.json with summed tokens/cost from children.

    Mix of populated and None values — None treated as 0 in aggregation.
    """
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    parent_id = "20260423-agg-aaaa"
    parent = _seed_parent(mas, parent_id)

    child1_id = "20260423-c1ag-aaaa"
    child2_id = "20260423-c2ag-aaaa"
    child3_id = "20260423-c3ag-aaaa"
    plan = Plan(
        parent_id=parent_id,
        summary="s",
        subtasks=[
            SubtaskSpec(id=child1_id, role="implementer", goal="c1"),
            SubtaskSpec(id=child2_id, role="tester", goal="c2"),
            SubtaskSpec(id=child3_id, role="evaluator", goal="c3"),
        ],
    )
    (parent / "plan.json").write_text(plan.model_dump_json())
    subtasks_dir = parent / "subtasks"
    subtasks_dir.mkdir()

    r1 = Result(task_id=child1_id, status="success", summary="d",
                tokens_in=100, tokens_out=50, cost_usd=0.01)
    r2 = Result(task_id=child2_id, status="success", summary="d",
                tokens_in=200, tokens_out=100, cost_usd=None)
    r3 = Result(task_id=child3_id, status="success", summary="d",
                tokens_in=None, tokens_out=None, cost_usd=0.05)

    for cid, r in [(child1_id, r1), (child2_id, r2), (child3_id, r3)]:
        cd = subtasks_dir / cid
        cd.mkdir()
        (cd / "result.json").write_text(r.model_dump_json())

    parent_task = board.read_task(parent)
    with patch("mas.tick.worktree.commit_changes"), patch("mas.tick.worktree.prune"):
        _finalize_parent(env, parent, parent_task)

    done_dir = mas / "tasks" / "done" / parent_id
    result = board.read_result(done_dir)

    assert result is not None, "parent result.json must be written during finalization"
    assert result.tokens_in == 300, "None tokens treated as 0: 100+200+0"
    assert result.tokens_out == 150, "None tokens treated as 0: 50+100+0"
    assert result.cost_usd == pytest.approx(0.06), "None cost treated as 0: 0.01+0.0+0.05"


def test_finalize_parent_aggregates_recursively(tmp_path: Path):
    """Child result.json with its own aggregated costs is included in parent total.

    This tests the recursive case: a child sub-orchestrator whose result.json
    already contains costs aggregated from its own grandchildren.
    """
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    parent_id = "20260423-rec-aaaa"
    parent = _seed_parent(mas, parent_id)

    child_a_id = "20260423-carg-aaaa"
    child_b_id = "20260423-cbrg-aaaa"
    plan = Plan(
        parent_id=parent_id,
        summary="s",
        subtasks=[
            SubtaskSpec(id=child_a_id, role="orchestrator", goal="ca"),
            SubtaskSpec(id=child_b_id, role="implementer", goal="cb"),
        ],
    )
    (parent / "plan.json").write_text(plan.model_dump_json())
    subtasks_dir = parent / "subtasks"
    subtasks_dir.mkdir()

    r_a = Result(task_id=child_a_id, status="success", summary="d",
                 tokens_in=500, tokens_out=250, cost_usd=0.10)
    r_b = Result(task_id=child_b_id, status="success", summary="d",
                 tokens_in=100, tokens_out=50, cost_usd=0.02)

    for cid, r in [(child_a_id, r_a), (child_b_id, r_b)]:
        cd = subtasks_dir / cid
        cd.mkdir()
        (cd / "result.json").write_text(r.model_dump_json())

    parent_task = board.read_task(parent)
    with patch("mas.tick.worktree.commit_changes"), patch("mas.tick.worktree.prune"):
        _finalize_parent(env, parent, parent_task)

    done_dir = mas / "tasks" / "done" / parent_id
    result = board.read_result(done_dir)

    assert result is not None
    assert result.tokens_in == 600
    assert result.tokens_out == 300
    assert result.cost_usd == pytest.approx(0.12)


def test_finalize_parent_all_none_costs_yields_zero(tmp_path: Path):
    """_finalize_parent result.json has cost_usd=0.0 when all children have None cost."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    parent_id = "20260423-null-aaaa"
    parent = _seed_parent(mas, parent_id)

    child_id = "20260423-nullc-aaaa"
    plan = Plan(
        parent_id=parent_id,
        summary="s",
        subtasks=[SubtaskSpec(id=child_id, role="implementer", goal="c")],
    )
    (parent / "plan.json").write_text(plan.model_dump_json())
    subtasks_dir = parent / "subtasks"
    subtasks_dir.mkdir()

    cd = subtasks_dir / child_id
    cd.mkdir()
    (cd / "result.json").write_text(
        Result(task_id=child_id, status="success", summary="d",
               tokens_in=None, tokens_out=None, cost_usd=None).model_dump_json()
    )

    parent_task = board.read_task(parent)
    with patch("mas.tick.worktree.commit_changes"), patch("mas.tick.worktree.prune"):
        _finalize_parent(env, parent, parent_task)

    done_dir = mas / "tasks" / "done" / parent_id
    result = board.read_result(done_dir)

    assert result is not None
    assert result.tokens_in == 0
    assert result.tokens_out == 0
    assert result.cost_usd == 0.0


# ---------------------------------------------------------------------------
# Cost budget guardrail tests
# ---------------------------------------------------------------------------
# _advance_one must check accumulated child cost_usd against the effective
# budget before dispatching the next subtask.  Tests use Task.model_construct()
# to set cost_budget_usd on the parent without triggering schema validation
# (the field is added by the implementer).  board.read_task is mocked so that
# _advance_one receives the constructed task instead of the on-disk copy.


def _seed_parent_with_done_and_pending(
    mas: Path,
    parent_id: str,
    done_child_id: str,
    pending_child_id: str,
    done_cost_usd: float,
) -> Path:
    """Create a doing/ parent with one completed subtask and one pending subtask."""
    parent = board.task_dir(mas, "doing", parent_id)
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="g"))
    (parent / "worktree").mkdir()

    plan = Plan(
        parent_id=parent_id,
        summary="s",
        subtasks=[
            SubtaskSpec(id=done_child_id, role="implementer", goal="done work"),
            SubtaskSpec(id=pending_child_id, role="tester", goal="pending work"),
        ],
    )
    (parent / "plan.json").write_text(plan.model_dump_json())

    subtasks_root = parent / "subtasks"
    subtasks_root.mkdir()

    done_dir = subtasks_root / done_child_id
    done_dir.mkdir()
    done_result = Result(
        task_id=done_child_id, status="success", summary="done", cost_usd=done_cost_usd
    )
    (done_dir / "result.json").write_text(done_result.model_dump_json())

    (subtasks_root / pending_child_id).mkdir()
    return parent


def test_cost_budget_no_budget_dispatches_normally(tmp_path: Path):
    """When no budget is set (cost_budget_usd=None), next subtask is dispatched normally."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = _seed_parent_with_done_and_pending(
        mas, "20260424-nobdgt-aaaa", "20260424-nbdone-aaaa", "20260424-nbpend-aaaa", 9999.0
    )
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with patch("mas.tick._dispatch_role", return_value=12345) as mock_dispatch:
        _advance_one(env, parent)

    mock_dispatch.assert_called_once()


def test_cost_budget_under_budget_dispatches_normally(tmp_path: Path):
    """When spent < budget, next subtask is dispatched normally."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = _seed_parent_with_done_and_pending(
        mas, "20260424-undbdgt-aaaa", "20260424-ubdone-aaaa", "20260424-ubpend-aaaa", 0.5
    )
    # Budget 100.0 >> spent 0.5 — dispatch should proceed
    parent_task = Task.model_construct(
        id="20260424-undbdgt-aaaa", role="orchestrator", goal="g",
        cycle=0, attempt=1, parent_id=None, inputs={}, constraints={},
        previous_failure=None, prior_results=[],
        cost_budget_usd=100.0,
    )
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with patch("mas.board.read_task", return_value=parent_task), \
         patch("mas.tick._dispatch_role", return_value=12345) as mock_dispatch:
        _advance_one(env, parent)

    mock_dispatch.assert_called_once()


def test_cost_budget_exceeded_fails_parent(tmp_path: Path):
    """When spent >= budget, parent moves to failed/ with a synthesized failure result."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = _seed_parent_with_done_and_pending(
        mas, "20260424-exceeded-aaaa", "20260424-exdone-aaaa", "20260424-expend-aaaa", 2.0
    )
    # Budget 1.0 < spent 2.0 — must NOT dispatch; must fail the parent
    parent_task = Task.model_construct(
        id="20260424-exceeded-aaaa", role="orchestrator", goal="g",
        cycle=0, attempt=1, parent_id=None, inputs={}, constraints={},
        previous_failure=None, prior_results=[],
        cost_budget_usd=1.0,
    )
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with patch("mas.board.read_task", return_value=parent_task), \
         patch("mas.tick._dispatch_role") as mock_dispatch:
        _advance_one(env, parent)

    # No further subtask must be dispatched
    mock_dispatch.assert_not_called()

    # Parent must be in failed/
    failed_dir = mas / "tasks" / "failed" / "20260424-exceeded-aaaa"
    assert failed_dir.exists(), "parent should be moved to failed/ when budget exceeded"

    # result.json must encode the budget failure with required fields
    result_path = failed_dir / "result.json"
    assert result_path.exists(), "result.json must be written on budget exceeded"
    result = Result.model_validate_json(result_path.read_text())
    assert result.status == "failure"
    assert "cost budget exceeded" in result.summary.lower()
    assert result.handoff is not None, "handoff must contain budget diagnostic fields"
    assert "spent_usd" in result.handoff
    assert "budget_usd" in result.handoff
    assert "last_completed_subtask_id" in result.handoff

    # Transition log must record the specific reason
    txns = transitions.read_transitions(failed_dir)
    reasons = [t.reason for t in txns]
    assert "cost_budget_exceeded" in reasons, (
        f"expected transition reason 'cost_budget_exceeded', got: {reasons}"
    )


def test_cost_budget_task_override_beats_config_default(tmp_path: Path):
    """Task-level cost_budget_usd takes priority over MasConfig.default_cost_budget_usd."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = _seed_parent_with_done_and_pending(
        mas, "20260424-override-aaaa", "20260424-ovdone-aaaa", "20260424-ovpend-aaaa", 0.8
    )
    # Config has a generous default (10.0) that would NOT be exceeded.
    # Task has a tight budget (0.5) that IS exceeded by spent=0.8.
    # The task-level budget must take priority.
    cfg = MasConfig.model_construct(
        providers={"mock": ProviderConfig(cli="sh", max_concurrent=2, extra_args=[])},
        roles={
            "proposer": RoleConfig(provider="mock", max_retries=2),
            "orchestrator": RoleConfig(provider="mock", max_retries=2),
            "implementer": RoleConfig(provider="mock", max_retries=2),
            "tester": RoleConfig(provider="mock", max_retries=2),
            "evaluator": RoleConfig(provider="mock", max_retries=2),
        },
        max_proposed=10,
        proposal_similarity_threshold=0.7,
        proposer_signals={},
        default_cost_budget_usd=10.0,
    )
    parent_task = Task.model_construct(
        id="20260424-override-aaaa", role="orchestrator", goal="g",
        cycle=0, attempt=1, parent_id=None, inputs={}, constraints={},
        previous_failure=None, prior_results=[],
        cost_budget_usd=0.5,
    )
    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)

    with patch("mas.board.read_task", return_value=parent_task), \
         patch("mas.tick._dispatch_role") as mock_dispatch:
        _advance_one(env, parent)

    # Task budget (0.5) must be used instead of config default (10.0)
    mock_dispatch.assert_not_called()
    failed_dir = mas / "tasks" / "failed" / "20260424-override-aaaa"
    assert failed_dir.exists(), "task-level cost_budget_usd must override config default"


# ---------------------------------------------------------------------------
# Arbiter dispatch (gap 3 / TODO #13)
# ---------------------------------------------------------------------------

def _cfg_with_arbiter() -> MasConfig:
    """Cfg matching `_cfg()` plus an arbiter role on the same provider."""
    return MasConfig(
        providers={"mock": ProviderConfig(cli="sh", max_concurrent=2, extra_args=[])},
        roles={
            "proposer": RoleConfig(provider="mock"),
            "orchestrator": RoleConfig(provider="mock"),
            "implementer": RoleConfig(provider="mock"),
            "tester": RoleConfig(provider="mock"),
            "evaluator": RoleConfig(provider="mock"),
            "arbiter": RoleConfig(provider="mock"),
        },
        max_proposed=10,
        proposal_similarity_threshold=0.7,
    )


def _seed_revision_cycle_with_disputes(
    tmp_path: Path, parent_id: str, *, disputes: list[dict]
) -> tuple[Path, Plan, Path]:
    """Set up a parent that has run cycle 1 (impl raised disputes) and is now
    handling a needs_revision evaluator from cycle 1. Returns (parent_dir,
    plan, mas_dir)."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", parent_id)
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="g"))
    subtasks = parent / "subtasks"
    subtasks.mkdir()

    plan = Plan(
        parent_id=parent_id, summary="s",
        subtasks=[
            SubtaskSpec(id="test-1", role="tester", goal="t"),
            SubtaskSpec(id="impl-1", role="implementer", goal="i"),
            SubtaskSpec(id="eval-1", role="evaluator", goal="e"),
            SubtaskSpec(id="rev-1-tester", role="tester", goal="r1t"),
            SubtaskSpec(id="rev-1-implementer", role="implementer", goal="r1i"),
            SubtaskSpec(id="rev-1-evaluator", role="evaluator", goal="r1e"),
        ],
        max_revision_cycles=2,
    )
    (parent / "plan.json").write_text(plan.model_dump_json())

    impl_dir = subtasks / "rev-1-implementer"
    impl_dir.mkdir()
    (impl_dir / "result.json").write_text(
        Result(
            task_id="rev-1-implementer",
            status="success",
            summary="addressed feedback",
            handoff={
                "changed_files": ["x.py"],
                "final_exit_code": 0,
                "disputes": disputes,
            },
        ).model_dump_json()
    )
    return parent, plan, mas


def test_arbiter_dispatched_when_implementer_disputes_evaluator(tmp_path: Path):
    """Cycle-1 evaluator says needs_revision but implementer raised disputes:
    arbiter subtask is appended instead of a new revision cycle."""
    parent, plan, mas = _seed_revision_cycle_with_disputes(
        tmp_path, "20260504-p-arb1-aaaa",
        disputes=[{"evaluator_claim": "missing X", "implementer_response": "X is at line 12"}],
    )

    spec = next(s for s in plan.subtasks if s.id == "rev-1-evaluator")
    result = Result(
        task_id="rev-1-evaluator", status="needs_revision",
        summary="still off", verdict="needs_revision", feedback="missing X",
    )

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg_with_arbiter())
    _handle_child_result(env, parent, board.read_task(parent), plan, spec, result)

    refreshed = parse_plan(parent / "plan.json", "20260504-p-arb1-aaaa")
    arbiters = [s for s in refreshed.subtasks if s.role == "arbiter"]
    assert len(arbiters) == 1
    assert arbiters[0].id == "arbiter-1"
    assert arbiters[0].inputs["evaluator_feedback"] == "missing X"
    assert arbiters[0].inputs["disputes"] == [
        {"evaluator_claim": "missing X", "implementer_response": "X is at line 12"}
    ]
    # No new rev-2-* cycle was appended.
    assert not any(s.id.startswith("rev-2-") for s in refreshed.subtasks)


def test_arbiter_skipped_when_role_not_configured(tmp_path: Path):
    """Without an arbiter role configured, the normal revision cycle is appended
    even when implementer raised disputes."""
    parent, plan, mas = _seed_revision_cycle_with_disputes(
        tmp_path, "20260504-p-arb2-aaaa",
        disputes=[{"evaluator_claim": "c", "implementer_response": "r"}],
    )

    spec = next(s for s in plan.subtasks if s.id == "rev-1-evaluator")
    result = Result(
        task_id="rev-1-evaluator", status="needs_revision",
        summary="x", verdict="needs_revision", feedback="fb",
    )

    cfg = _cfg()  # no arbiter role
    cfg.max_replans = 0  # disable replan path so we deterministically hit append
    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)
    _handle_child_result(env, parent, board.read_task(parent), plan, spec, result)

    refreshed = parse_plan(parent / "plan.json", "20260504-p-arb2-aaaa")
    assert not any(s.role == "arbiter" for s in refreshed.subtasks)
    assert any(s.id.startswith("rev-2-") for s in refreshed.subtasks)


def test_arbiter_skipped_on_first_cycle_eval(tmp_path: Path):
    """No revision cycle has run yet (cycle-0 evaluator) → arbiter must not be
    dispatched even if implementer's handoff carried disputes."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent_id = "20260504-p-arb3-aaaa"
    parent = board.task_dir(mas, "doing", parent_id)
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="g"))
    subtasks = parent / "subtasks"
    subtasks.mkdir()

    plan = Plan(
        parent_id=parent_id, summary="s",
        subtasks=[
            SubtaskSpec(id="test-1", role="tester", goal="t"),
            SubtaskSpec(id="impl-1", role="implementer", goal="i"),
            SubtaskSpec(id="eval-1", role="evaluator", goal="e"),
        ],
        max_revision_cycles=2,
    )
    (parent / "plan.json").write_text(plan.model_dump_json())

    impl_dir = subtasks / "impl-1"
    impl_dir.mkdir()
    (impl_dir / "result.json").write_text(
        Result(
            task_id="impl-1", status="success", summary="ok",
            handoff={
                "changed_files": [], "final_exit_code": 0,
                "disputes": [{"evaluator_claim": "c", "implementer_response": "r"}],
            },
        ).model_dump_json()
    )

    spec = next(s for s in plan.subtasks if s.id == "eval-1")
    result = Result(
        task_id="eval-1", status="needs_revision",
        summary="x", verdict="needs_revision", feedback="fb",
    )

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg_with_arbiter())
    _handle_child_result(env, parent, board.read_task(parent), plan, spec, result)

    refreshed = parse_plan(parent / "plan.json", parent_id)
    assert not any(s.role == "arbiter" for s in refreshed.subtasks)
    assert any(s.id.startswith("rev-1-") for s in refreshed.subtasks)


def test_arbiter_skipped_when_no_disputes(tmp_path: Path):
    """Implementer didn't raise disputes → fall through to normal revision flow.
    Uses max_replans=0 to keep this test scoped to append-cycle path; replan
    behavior is exercised separately."""
    parent, plan, mas = _seed_revision_cycle_with_disputes(
        tmp_path, "20260504-p-arb4-aaaa", disputes=[]
    )
    spec = next(s for s in plan.subtasks if s.id == "rev-1-evaluator")
    result = Result(
        task_id="rev-1-evaluator", status="needs_revision",
        summary="x", verdict="needs_revision", feedback="fb",
    )

    cfg = _cfg_with_arbiter()
    cfg.max_replans = 0
    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)
    _handle_child_result(env, parent, board.read_task(parent), plan, spec, result)

    refreshed = parse_plan(parent / "plan.json", "20260504-p-arb4-aaaa")
    assert not any(s.role == "arbiter" for s in refreshed.subtasks)


def test_arbiter_pass_lets_parent_finalize(tmp_path: Path):
    """Arbiter verdict=pass → _handle_child_result returns without moving the
    parent; _all_children_passed treats it as a passing terminal subtask."""
    parent, plan, mas = _seed_revision_cycle_with_disputes(
        tmp_path, "20260504-p-arb5-aaaa",
        disputes=[{"evaluator_claim": "c", "implementer_response": "r"}],
    )
    plan.subtasks.append(SubtaskSpec(id="arbiter-1", role="arbiter", goal="resolve"))
    (parent / "plan.json").write_text(plan.model_dump_json())

    arb_dir = parent / "subtasks" / "arbiter-1"
    arb_dir.mkdir()
    arb_result = Result(
        task_id="arbiter-1", status="success", summary="implementer right",
        verdict="pass",
    )
    (arb_dir / "result.json").write_text(arb_result.model_dump_json())

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg_with_arbiter())
    spec = next(s for s in plan.subtasks if s.id == "arbiter-1")
    _handle_child_result(env, parent, board.read_task(parent), plan, spec, arb_result)

    # Parent stays in doing/ — finalize happens on next tick once
    # _all_children_passed observes the full chain. Seed the rest as passing.
    for sid in ("test-1", "impl-1"):
        d = parent / "subtasks" / sid
        d.mkdir(exist_ok=True)
        (d / "result.json").write_text(
            Result(task_id=sid, status="success", summary="ok").model_dump_json()
        )
    eval_dir = parent / "subtasks" / "eval-1"
    eval_dir.mkdir(exist_ok=True)
    (eval_dir / "result.json").write_text(
        Result(task_id="eval-1", status="success", summary="ok",
               verdict="pass").model_dump_json()
    )
    rev_eval_dir = parent / "subtasks" / "rev-1-evaluator"
    rev_eval_dir.mkdir(exist_ok=True)
    (rev_eval_dir / "result.json").write_text(
        Result(task_id="rev-1-evaluator", status="needs_revision",
               summary="x", verdict="needs_revision", feedback="fb").model_dump_json()
    )
    rev_test_dir = parent / "subtasks" / "rev-1-tester"
    rev_test_dir.mkdir(exist_ok=True)
    (rev_test_dir / "result.json").write_text(
        Result(task_id="rev-1-tester", status="success", summary="ok").model_dump_json()
    )

    assert _all_children_passed(plan, parent / "subtasks") is True
    assert parent.exists(), "parent should not have moved on arbiter pass"


def test_arbiter_fail_moves_parent_to_failed(tmp_path: Path):
    """Arbiter verdict=fail → parent moves straight to failed/ with reason
    'arbiter_verdict_fail', no further revision cycles."""
    parent, plan, mas = _seed_revision_cycle_with_disputes(
        tmp_path, "20260504-p-arb6-aaaa",
        disputes=[{"evaluator_claim": "c", "implementer_response": "r"}],
    )
    plan.subtasks.append(SubtaskSpec(id="arbiter-1", role="arbiter", goal="resolve"))
    (parent / "plan.json").write_text(plan.model_dump_json())

    arb_dir = parent / "subtasks" / "arbiter-1"
    arb_dir.mkdir()
    arb_result = Result(
        task_id="arbiter-1", status="success", summary="evaluator right",
        verdict="fail", feedback="claim was correct",
    )
    (arb_dir / "result.json").write_text(arb_result.model_dump_json())

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg_with_arbiter())
    spec = next(s for s in plan.subtasks if s.id == "arbiter-1")
    _handle_child_result(env, parent, board.read_task(parent), plan, spec, arb_result)

    parent_id = "20260504-p-arb6-aaaa"
    assert not parent.exists()
    failed_dir = mas / "tasks" / "failed" / parent_id
    assert failed_dir.exists()
    txns = transitions.read_transitions(failed_dir)
    assert any(t.reason == "arbiter_verdict_fail" for t in txns)


def test_arbiter_not_dispatched_twice(tmp_path: Path):
    """Once an arbiter subtask exists in plan.subtasks, another evaluator
    needs_revision must not append a second arbiter."""
    parent, plan, mas = _seed_revision_cycle_with_disputes(
        tmp_path, "20260504-p-arb7-aaaa",
        disputes=[{"evaluator_claim": "c", "implementer_response": "r"}],
    )
    plan.subtasks.append(SubtaskSpec(id="arbiter-1", role="arbiter", goal="resolve"))
    (parent / "plan.json").write_text(plan.model_dump_json())

    spec = next(s for s in plan.subtasks if s.id == "rev-1-evaluator")
    result = Result(
        task_id="rev-1-evaluator", status="needs_revision",
        summary="x", verdict="needs_revision", feedback="fb",
    )

    cfg = _cfg_with_arbiter()
    cfg.max_replans = 0  # avoid replan branch noise; we want append cycle path
    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)
    _handle_child_result(env, parent, board.read_task(parent), plan, spec, result)

    refreshed = parse_plan(parent / "plan.json", "20260504-p-arb7-aaaa")
    assert sum(1 for s in refreshed.subtasks if s.role == "arbiter") == 1


# ---------------------------------------------------------------------------
# Evaluator quorum (gap 1 / TODO #14)
# ---------------------------------------------------------------------------

def _cfg_with_quorum(n: int = 2) -> MasConfig:
    """Cfg matching `_cfg()` with `roles.evaluator.quorum = n`."""
    return MasConfig(
        providers={"mock": ProviderConfig(cli="sh", max_concurrent=2, extra_args=[])},
        roles={
            "proposer": RoleConfig(provider="mock"),
            "orchestrator": RoleConfig(provider="mock"),
            "implementer": RoleConfig(provider="mock"),
            "tester": RoleConfig(provider="mock"),
            "evaluator": RoleConfig(provider="mock", quorum=n),
        },
        max_proposed=10,
        proposal_similarity_threshold=0.7,
    )


def test_quorum_field_default_is_one():
    """Quorum defaults to 1 (single evaluator) so existing configs are unchanged."""
    cfg = _cfg()
    assert cfg.roles["evaluator"].quorum == 1


def test_quorum_field_rejects_zero():
    """quorum < 1 is invalid — Pydantic must reject it."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        RoleConfig(provider="mock", quorum=0)


def test_expand_evaluator_quorum_noop_when_quorum_one():
    """quorum=1 leaves the plan unchanged."""
    from mas.tick import _expand_evaluator_quorum

    plan = Plan(
        parent_id="p", summary="s",
        subtasks=[
            SubtaskSpec(id="impl-1", role="implementer", goal="i"),
            SubtaskSpec(id="eval-1", role="evaluator", goal="e"),
        ],
    )
    changed = _expand_evaluator_quorum(plan, _cfg())
    assert changed is False
    assert [s.id for s in plan.subtasks] == ["impl-1", "eval-1"]


def test_expand_evaluator_quorum_expands_to_n_siblings():
    """quorum=3 expands a single eval-1 into eval-1-q1, eval-1-q2, eval-1-q3."""
    from mas.tick import _expand_evaluator_quorum

    plan = Plan(
        parent_id="p", summary="s",
        subtasks=[
            SubtaskSpec(id="impl-1", role="implementer", goal="i"),
            SubtaskSpec(id="eval-1", role="evaluator", goal="e"),
        ],
    )
    changed = _expand_evaluator_quorum(plan, _cfg_with_quorum(3))
    assert changed is True
    assert [s.id for s in plan.subtasks] == [
        "impl-1", "eval-1-q1", "eval-1-q2", "eval-1-q3",
    ]
    # Other fields preserved on each clone.
    for s in plan.subtasks[1:]:
        assert s.role == "evaluator"
        assert s.goal == "e"


def test_expand_evaluator_quorum_idempotent():
    """Re-expanding an already-expanded plan must not double-expand."""
    from mas.tick import _expand_evaluator_quorum

    plan = Plan(
        parent_id="p", summary="s",
        subtasks=[
            SubtaskSpec(id="eval-1-q1", role="evaluator", goal="e"),
            SubtaskSpec(id="eval-1-q2", role="evaluator", goal="e"),
        ],
    )
    changed = _expand_evaluator_quorum(plan, _cfg_with_quorum(2))
    assert changed is False
    assert len(plan.subtasks) == 2


def _seed_quorum_parent(tmp_path: Path, parent_id: str, quorum: int = 2) -> tuple[Path, Plan, Path]:
    """Set up a parent with one tester+implementer already passing and a
    quorum of evaluators ready to be aggregated."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", parent_id)
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="g"))
    subtasks = parent / "subtasks"
    subtasks.mkdir()

    eval_specs = [
        SubtaskSpec(id=f"eval-1-q{i}", role="evaluator", goal="e")
        for i in range(1, quorum + 1)
    ]
    plan = Plan(
        parent_id=parent_id, summary="s",
        subtasks=[
            SubtaskSpec(id="test-1", role="tester", goal="t"),
            SubtaskSpec(id="impl-1", role="implementer", goal="i"),
            *eval_specs,
        ],
        max_revision_cycles=2,
    )
    (parent / "plan.json").write_text(plan.model_dump_json())

    for sid in ("test-1", "impl-1"):
        d = subtasks / sid
        d.mkdir()
        (d / "result.json").write_text(
            Result(task_id=sid, status="success", summary="ok").model_dump_json()
        )
    return parent, plan, mas


def _write_eval_result(parent: Path, sid: str, *, verdict: str, feedback: str = "") -> Result:
    d = parent / "subtasks" / sid
    d.mkdir(exist_ok=True)
    status = "success" if verdict == "pass" else "needs_revision"
    r = Result(
        task_id=sid, status=status, summary=f"{sid} verdict",
        verdict=verdict, feedback=feedback,
    )
    (d / "result.json").write_text(r.model_dump_json())
    return r


def test_quorum_defers_until_all_siblings_complete(tmp_path: Path):
    """When only one quorum sibling has finished, _handle_child_result must
    not append a revision cycle — the merger waits for the second sibling."""
    parent, plan, mas = _seed_quorum_parent(tmp_path, "20260504-quor1-aaaa")
    # Only q1 has a result (pass); q2 is pending.
    r1 = _write_eval_result(parent, "eval-1-q1", verdict="pass")

    spec = next(s for s in plan.subtasks if s.id == "eval-1-q1")
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg_with_quorum(2))
    _handle_child_result(env, parent, board.read_task(parent), plan, spec, r1)

    refreshed = parse_plan(parent / "plan.json", "20260504-quor1-aaaa")
    assert not any(s.id.startswith("rev-") for s in refreshed.subtasks)
    assert parent.exists(), "parent must remain in doing/ until quorum completes"


def test_quorum_unanimous_pass_does_not_revise(tmp_path: Path):
    """All quorum members pass → no revision cycle, parent eligible to finalize."""
    parent, plan, mas = _seed_quorum_parent(tmp_path, "20260504-quor2-aaaa")
    _write_eval_result(parent, "eval-1-q1", verdict="pass")
    r2 = _write_eval_result(parent, "eval-1-q2", verdict="pass")

    spec = next(s for s in plan.subtasks if s.id == "eval-1-q2")
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg_with_quorum(2))
    _handle_child_result(env, parent, board.read_task(parent), plan, spec, r2)

    refreshed = parse_plan(parent / "plan.json", "20260504-quor2-aaaa")
    assert not any(s.id.startswith("rev-") for s in refreshed.subtasks)
    # Both quorum members are individually verdict=pass, so all_children_passed True.
    assert _all_children_passed(refreshed, parent / "subtasks") is True


def test_quorum_dissent_appends_revision_with_merged_feedback(tmp_path: Path):
    """One pass + one needs_revision → consensus is needs_revision, a revision
    cycle is appended, and the recorded feedback merges both sibling messages."""
    parent, plan, mas = _seed_quorum_parent(tmp_path, "20260504-quor3-aaaa")
    _write_eval_result(parent, "eval-1-q1", verdict="pass", feedback="looks good")
    r2 = _write_eval_result(
        parent, "eval-1-q2", verdict="needs_revision", feedback="missing test for X",
    )

    spec = next(s for s in plan.subtasks if s.id == "eval-1-q2")
    cfg = _cfg_with_quorum(2)
    cfg.max_replans = 0  # keep test scoped to append-cycle path
    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)
    _handle_child_result(env, parent, board.read_task(parent), plan, spec, r2)

    refreshed = parse_plan(parent / "plan.json", "20260504-quor3-aaaa")
    rev_specs = [s for s in refreshed.subtasks if s.id.startswith("rev-1-")]
    assert rev_specs, "revision cycle must be appended on quorum dissent"
    merged = refreshed.revision_feedback["rev-1"]
    assert "eval-1-q1" in merged and "looks good" in merged
    assert "eval-1-q2" in merged and "missing test for X" in merged


def test_quorum_revision_cycle_evaluator_also_expanded(tmp_path: Path):
    """When a revision cycle is appended under quorum config, the new
    `rev-1-evaluator` subtask must itself be expanded into N quorum members."""
    parent, plan, mas = _seed_quorum_parent(tmp_path, "20260504-quor4-aaaa")
    _write_eval_result(parent, "eval-1-q1", verdict="pass")
    r2 = _write_eval_result(
        parent, "eval-1-q2", verdict="needs_revision", feedback="X missing",
    )

    spec = next(s for s in plan.subtasks if s.id == "eval-1-q2")
    cfg = _cfg_with_quorum(2)
    cfg.max_replans = 0
    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)
    _handle_child_result(env, parent, board.read_task(parent), plan, spec, r2)

    refreshed = parse_plan(parent / "plan.json", "20260504-quor4-aaaa")
    rev_evals = [s for s in refreshed.subtasks if s.role == "evaluator" and s.id.startswith("rev-1-")]
    assert {s.id for s in rev_evals} == {"rev-1-evaluator-q1", "rev-1-evaluator-q2"}


def test_advance_one_expands_orchestrator_plan(tmp_path: Path):
    """Plan written by an orchestrator with a single evaluator gets expanded
    in-place by _advance_one before any subtask is dispatched."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent_id = "20260504-quor5-aaaa"
    parent = board.task_dir(mas, "doing", parent_id)
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="g"))
    (parent / "worktree").mkdir()
    plan = Plan(
        parent_id=parent_id, summary="s",
        subtasks=[
            SubtaskSpec(id="impl-1", role="implementer", goal="i"),
            SubtaskSpec(id="eval-1", role="evaluator", goal="e"),
        ],
    )
    (parent / "plan.json").write_text(plan.model_dump_json())

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg_with_quorum(2))
    # Patch dispatch so we don't actually launch a subprocess.
    with patch("mas.tick._dispatch_role", return_value=None):
        _advance_one(env, parent)

    refreshed = parse_plan(parent / "plan.json", parent_id)
    assert [s.id for s in refreshed.subtasks] == [
        "impl-1", "eval-1-q1", "eval-1-q2",
    ]


# ---------------------------------------------------------------------------
# 14. Plan validation (_validate_plan / InvalidPlanError)
# ---------------------------------------------------------------------------

def test_validate_plan_valid_passes():
    """Valid Plan passes _validate_plan without raising."""
    from mas.tick import _validate_plan
    plan = Plan(
        parent_id="x", summary="s",
        subtasks=[SubtaskSpec(id="s1", role="implementer", goal="g")],
    )
    config = _cfg()
    _validate_plan(plan, config)


def test_validate_plan_empty_subtasks_raises():
    """Plan with empty subtasks raises InvalidPlanError."""
    from mas.tick import _validate_plan, InvalidPlanError
    plan = Plan(parent_id="x", summary="s", subtasks=[])
    config = _cfg()
    with pytest.raises(InvalidPlanError, match="subtasks"):
        _validate_plan(plan, config)


def test_validate_plan_unknown_role_raises():
    """Plan referencing unconfigured role raises InvalidPlanError."""
    from mas.tick import _validate_plan, InvalidPlanError
    plan = Plan(
        parent_id="x", summary="s",
        subtasks=[SubtaskSpec(id="s1", role="arbiter", goal="g")],
    )
    config = _cfg()  # no "arbiter" role
    with pytest.raises(InvalidPlanError) as exc:
        _validate_plan(plan, config)
    assert "arbiter" in str(exc.value)


def test_validate_plan_invalid_json_moves_to_failed(tmp_path: Path):
    """plan.json with invalid JSON causes parent to move to failed/."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent_id = "20260512-bij-aaaa"
    parent = board.task_dir(mas, "doing", parent_id)
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="g"))
    (parent / "worktree").mkdir()
    (parent / "plan.json").write_text("{invalid}")

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with patch("mas.tick._dispatch_role"):
        _advance_doing(env)

    assert (mas / "tasks" / "failed" / parent_id).exists()


def test_validate_plan_missing_subtasks_moves_to_failed(tmp_path: Path):
    """plan.json missing 'subtasks' → parent moved to failed/."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent_id = "20260512-bms-aaaa"
    parent = board.task_dir(mas, "doing", parent_id)
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="g"))
    (parent / "worktree").mkdir()
    (parent / "plan.json").write_text(
        json.dumps({"parent_id": parent_id, "summary": "s"})
    )

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with patch("mas.tick._dispatch_role"):
        _advance_doing(env)

    assert (mas / "tasks" / "failed" / parent_id).exists()


def test_validate_plan_unknown_role_moves_to_failed(tmp_path: Path):
    """Subtasks referencing unconfigured role → parent moved to failed/."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent_id = "20260512-unk-aaaa"
    parent = board.task_dir(mas, "doing", parent_id)
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="g"))
    (parent / "worktree").mkdir()
    plan = Plan(
        parent_id=parent_id, summary="s",
        subtasks=[SubtaskSpec(id="s1", role="arbiter", goal="g")],
    )
    (parent / "plan.json").write_text(plan.model_dump_json())

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with patch("mas.tick._dispatch_role"):
        _advance_doing(env)

    assert (mas / "tasks" / "failed" / parent_id).exists()
