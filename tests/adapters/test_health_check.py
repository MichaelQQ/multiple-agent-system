from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from mas import board
from mas.adapters import AdapterUnavailableError
from mas.adapters.base import Adapter
from mas.schemas import MasConfig, ProviderConfig, RoleConfig, Task, SubtaskSpec, Result
from mas.tick import TickEnv, _dispatch_role, _handle_child_result


def _provider_cfg(cli: str) -> ProviderConfig:
    return ProviderConfig(cli=cli, max_concurrent=1, extra_args=[])


def _role_cfg(provider: str, max_retries: int = 2) -> RoleConfig:
    return RoleConfig(provider=provider, max_retries=max_retries)


def _make_env_and_cfg(tmp_path: Path, provider_name: str = "mock"):
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    cfg = MasConfig(
        providers={provider_name: _provider_cfg(provider_name)},
        roles={
            "proposer": _role_cfg(provider_name),
            "orchestrator": _role_cfg(provider_name),
            "implementer": _role_cfg(provider_name),
            "tester": _role_cfg(provider_name),
            "evaluator": _role_cfg(provider_name),
        },
    )
    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)
    return mas, env, cfg


class FailingAdapter(Adapter):
    name = "mock"
    agentic = True

    def build_command(self, prompt: str, task_dir: Path, cwd: Path) -> list[str]:
        return ["true"]

    def health_check(self) -> bool:
        return True

    def dispatch(self, prompt, task_dir, cwd, log_path, role, stdin_text=None, **_):
        raise AdapterUnavailableError("mock provider unavailable")


def test_dispatch_role_handles_adapter_unavailable(tmp_path: Path):
    """Updated: task stays in doing/ with result.json, NOT moved to failed/."""
    mas, env, cfg = _make_env_and_cfg(tmp_path)

    task = Task(
        id="20260422-health-check-abcd",
        role="proposer",
        goal="verify unavailable adapter handling",
    )
    task_dir = board.task_dir(mas, "doing", task.id)

    with patch("mas.tick.get_adapter", return_value=FailingAdapter):
        _dispatch_role(env, task, task_dir, tmp_path, role="proposer")

    # Task must stay in doing/, not moved to failed/
    assert task_dir.exists(), "Task dir must still exist in doing/"
    assert (task_dir / "result.json").exists(), "result.json must be written in task dir"

    failed_dir = board.task_dir(mas, "failed", task.id)
    assert not failed_dir.exists(), "Task must NOT be moved to failed/"

    result = json.loads((task_dir / "result.json").read_text())
    assert result["status"] == "failure"
    assert "mock" in result["summary"].lower()


def test_subtask_stays_under_parent_on_adapter_unavailable(tmp_path: Path):
    """Subtask dir stays under parent_dir/subtasks/, result.json written in child_dir."""
    mas, env, cfg = _make_env_and_cfg(tmp_path)

    parent_id = "20260507-parent-1234"
    parent_dir = board.task_dir(mas, "doing", parent_id)
    parent_dir.mkdir(parents=True, exist_ok=True)

    subtasks_dir = parent_dir / "subtasks"
    subtasks_dir.mkdir(exist_ok=True)
    child_id = "spec-1"
    child_dir = subtasks_dir / child_id
    child_dir.mkdir()

    spec = SubtaskSpec(id=child_id, role="implementer", goal="test subtask")
    task = Task(
        id=child_id,
        parent_id=parent_id,
        role="implementer",
        goal="test subtask",
        inputs={"spec": spec.model_dump()},
    )
    (child_dir / "task.json").write_text(task.model_dump_json())

    with patch("mas.tick.get_adapter", return_value=FailingAdapter):
        _dispatch_role(env, task, child_dir, tmp_path, role="implementer")

    assert child_dir.exists(), "Subtask dir must still exist"
    assert child_dir.parent == subtasks_dir, "Subtask dir must stay under parent/subtasks/"
    assert (child_dir / "result.json").exists(), "result.json must be written in child_dir"

    assert parent_dir.exists(), "Parent dir must still be in doing/"
    failed_dir = board.task_dir(mas, "failed", parent_id)
    assert not failed_dir.exists(), "Parent must NOT be moved to failed/"

    result = Result.parse_raw((child_dir / "result.json").read_text())
    assert result.status == "failure"
    assert "mock" in result.summary.lower()


def test_retry_path_bumps_attempt_and_writes_previous_failure(tmp_path: Path):
    """After _dispatch_role catches AdapterUnavailableError (with fix: no board.move),
    the next tick's _handle_child_result triggers retry path.
    This test verifies the full flow: _dispatch_role -> result.json in doing/ -> retry."""
    mas, env, cfg = _make_env_and_cfg(tmp_path, provider_name="mock")

    parent_id = "20260507-parent-1234"
    parent_dir = board.task_dir(mas, "doing", parent_id)
    parent_dir.mkdir(parents=True, exist_ok=True)
    parent_task = Task(id=parent_id, role="orchestrator", goal="parent task")
    (parent_dir / "task.json").write_text(parent_task.model_dump_json())

    subtasks_dir = parent_dir / "subtasks"
    subtasks_dir.mkdir(exist_ok=True)
    child_id = "spec-1"
    child_dir = subtasks_dir / child_id
    child_dir.mkdir()

    spec = SubtaskSpec(id=child_id, role="implementer", goal="test subtask")
    task = Task(
        id=child_id,
        parent_id=parent_id,
        role="implementer",
        goal="test subtask",
        attempt=1,
    )
    (child_dir / "task.json").write_text(task.model_dump_json())
    (child_dir / ".attempt").write_text("1")

    # Call _dispatch_role with FailingAdapter
    with patch("mas.tick.get_adapter", return_value=FailingAdapter):
        _dispatch_role(env, task, child_dir, tmp_path, role="implementer")

    # After fix: task should still be in doing/ with result.json
    assert child_dir.exists(), "Subtask dir must still exist after fix"
    assert (child_dir / "result.json").exists(), "result.json must be written in child_dir"

    # Now simulate next tick: _handle_child_result should trigger retry
    from mas.schemas import Plan
    plan = Plan(parent_id=parent_id, summary="test", subtasks=[spec])
    failure_result = Result.parse_raw((child_dir / "result.json").read_text())
    _handle_child_result(env, parent_dir, parent_task, plan, spec, failure_result)

    assert (child_dir / ".attempt").read_text() == "2", "Attempt must be bumped"
    assert (child_dir / ".previous_failure").exists(), ".previous_failure must be written"


def test_max_retries_exhausted_parent_moves_to_failed(tmp_path: Path):
    """After max retries exhausted, parent moves to failed/.
    This test verifies the full flow with the fix in place."""
    mas, env, cfg = _make_env_and_cfg(tmp_path, provider_name="mock")

    parent_id = "20260507-parent-1234"
    parent_dir = board.task_dir(mas, "doing", parent_id)
    parent_dir.mkdir(parents=True, exist_ok=True)
    parent_task = Task(id=parent_id, role="orchestrator", goal="parent task")
    (parent_dir / "task.json").write_text(parent_task.model_dump_json())

    subtasks_dir = parent_dir / "subtasks"
    subtasks_dir.mkdir(exist_ok=True)
    child_id = "spec-1"
    child_dir = subtasks_dir / child_id
    child_dir.mkdir()

    spec = SubtaskSpec(id=child_id, role="implementer", goal="test subtask")
    task = Task(
        id=child_id,
        parent_id=parent_id,
        role="implementer",
        goal="test subtask",
        attempt=3,  # Already at max_retries + 1
    )
    (child_dir / "task.json").write_text(task.model_dump_json())
    (child_dir / ".attempt").write_text("3")

    # Call _dispatch_role with FailingAdapter (simulates AdapterUnavailableError)
    with patch("mas.tick.get_adapter", return_value=FailingAdapter):
        _dispatch_role(env, task, child_dir, tmp_path, role="implementer")

    # After fix: task should still be in doing/ with result.json
    # Then _handle_child_result should move parent to failed/
    assert child_dir.exists(), "Subtask dir must still exist after fix"
    assert (child_dir / "result.json").exists(), "result.json must be written"

    from mas.schemas import Plan
    plan = Plan(parent_id=parent_id, summary="test", subtasks=[spec])
    failure_result = Result.parse_raw((child_dir / "result.json").read_text())
    _handle_child_result(env, parent_dir, parent_task, plan, spec, failure_result)

    # Parent should be moved to failed/
    assert not parent_dir.exists(), "Parent dir must be removed from doing"
    failed_dir = board.task_dir(mas, "failed", parent_id)
    assert failed_dir.exists(), "Parent must be moved to failed/"
