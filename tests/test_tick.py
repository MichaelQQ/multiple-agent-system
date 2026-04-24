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

    _append_revision_cycle(parent, plan, board.read_task(parent), "more feedback")

    updated = parse_plan(parent / "plan.json", "20260415-p-13-aaaa")
    assert len(updated.subtasks) == 6


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

    with patch("mas.tick._dispatch_role") as mock_dispatch:
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
         patch("mas.tick._dispatch_role") as mock_dispatch:
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
