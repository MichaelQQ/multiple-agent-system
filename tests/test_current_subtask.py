"""Tests for .current_subtask marker file lifecycle in the tick loop.

Tests encode:
1. Marker written on subtask dispatch (not proposer/orchestrator)
2. Marker deleted when result.json is collected
3. Marker not written for proposer or orchestrator
4. Marker cleaned up on orphan synthesis
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
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
from mas.tick import TickEnv, _advance_one


def _cfg(
    max_retries: int = 2,
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
        max_proposed=10,
        proposal_similarity_threshold=0.7,
    )


def _seed_parent_with_plan(mas: Path, parent_id: str, child_id: str, role: str = "implementer") -> Path:
    parent = board.task_dir(mas, "doing", parent_id)
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="g"))
    (parent / "worktree").mkdir()
    plan = Plan(
        parent_id=parent_id,
        summary="s",
        subtasks=[SubtaskSpec(id=child_id, role=role, goal="do")],
    )
    (parent / "plan.json").write_text(plan.model_dump_json())
    (parent / "subtasks" / child_id).mkdir(parents=True)
    return parent


# ---------------------------------------------------------------------------
# 1. Marker file written on subtask dispatch
# ---------------------------------------------------------------------------

def test_current_subtask_written_on_subtask_dispatch(tmp_path: Path):
    """When _advance_one dispatches a subtask, .current_subtask is written to the parent dir."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = _seed_parent_with_plan(mas, "20260424-p1-aaaa", "20260424-impl-1-aaaa")
    marker_path = parent / ".current_subtask"

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with patch("mas.tick.get_adapter") as mock_get, \
         patch("mas.board.count_active_pids", return_value=0), \
         patch("mas.board.write_pid"):
        mock_adapter = MagicMock()
        mock_adapter.dispatch.return_value = MagicMock(pid=99999)
        mock_adapter.agentic = False
        mock_get.return_value.return_value = mock_adapter
        _advance_one(env, parent)

    assert marker_path.exists(), ".current_subtask not written to parent dir"


def test_current_subtask_contains_role_provider_pid_start_time_subtask_id(tmp_path: Path):
    """Marker JSON contains: role, provider, pid (int), start_time_iso (str), subtask_id."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = _seed_parent_with_plan(mas, "20260424-p2-aaaa", "20260424-impl-2-aaaa")
    marker_path = parent / ".current_subtask"

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with patch("mas.tick.get_adapter") as mock_get, \
         patch("mas.board.count_active_pids", return_value=0), \
         patch("mas.board.write_pid"):
        mock_adapter = MagicMock()
        mock_adapter.dispatch.return_value = MagicMock(pid=77777)
        mock_adapter.agentic = False
        mock_get.return_value.return_value = mock_adapter
        _advance_one(env, parent)

    data = json.loads(marker_path.read_text())
    assert data["role"] == "implementer"
    assert data["provider"] == "mock"
    assert data["pid"] == 77777
    assert "start_time_iso" in data
    assert data["subtask_id"] == "20260424-impl-2-aaaa"


# ---------------------------------------------------------------------------
# 2. Marker deleted when result.json is collected
# ---------------------------------------------------------------------------

def test_current_subtask_deleted_when_result_collected(tmp_path: Path):
    """When _advance_one processes a child result.json, .current_subtask is deleted."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = _seed_parent_with_plan(mas, "20260424-p3-aaaa", "20260424-impl-3-aaaa")
    child = parent / "subtasks" / "20260424-impl-3-aaaa"
    marker_path = parent / ".current_subtask"

    result = Result(task_id="20260424-impl-3-aaaa", status="success", summary="ok")
    (child / "result.json").write_text(result.model_dump_json())

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    _advance_one(env, parent)

    assert not marker_path.exists(), ".current_subtask not deleted after result collected"


# ---------------------------------------------------------------------------
# 3. Marker NOT written for proposer or orchestrator
# ---------------------------------------------------------------------------

def test_current_subtask_not_written_for_proposer(tmp_path: Path):
    """Proposer dispatch does NOT write .current_subtask to parent."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "20260424-prop-1-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260424-prop-1-aaaa", role="proposer", goal="propose"))
    marker_path = parent / ".current_subtask"

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with patch("mas.tick.get_adapter") as mock_get, \
         patch("mas.board.count_active_pids", return_value=0), \
         patch("mas.board.write_pid"):
        mock_adapter = MagicMock()
        mock_adapter.dispatch.return_value = MagicMock(pid=11111)
        mock_adapter.agentic = False
        mock_get.return_value.return_value = mock_adapter
        _advance_one(env, parent)

    assert not marker_path.exists(), ".current_subtask written for proposer (should not be)"


def test_current_subtask_not_written_for_orchestrator(tmp_path: Path):
    """Orchestrator dispatch does NOT write .current_subtask to parent."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = board.task_dir(mas, "doing", "20260424-orch-1-aaaa")
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id="20260424-orch-1-aaaa", role="orchestrator", goal="g"))
    (parent / "worktree").mkdir()
    marker_path = parent / ".current_subtask"

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with patch("mas.tick.get_adapter") as mock_get, \
         patch("mas.board.count_active_pids", return_value=0), \
         patch("mas.board.write_pid"):
        mock_adapter = MagicMock()
        mock_adapter.dispatch.return_value = MagicMock(pid=22222)
        mock_adapter.agentic = False
        mock_get.return_value.return_value = mock_adapter
        _advance_one(env, parent)

    assert not marker_path.exists(), ".current_subtask written for orchestrator (should not be)"


# ---------------------------------------------------------------------------
# 4. Marker cleaned up on orphan synthesis
# ---------------------------------------------------------------------------

def test_current_subtask_deleted_on_orphan_synthesis(tmp_path: Path):
    """When a worker is orphaned (dead PID) and result is synthesized, .current_subtask
    is still cleaned up (deleted).

    The orphan detection → result.json synthesis → _handle_child_result path deletes
    the marker. We write a pre-existing marker to prove _handle_child_result
    deletes it (rather than relying on the stub to create the marker).
    """
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    parent = _seed_parent_with_plan(mas, "20260424-p4-aaaa", "20260424-impl-4-aaaa")
    child = parent / "subtasks" / "20260424-impl-4-aaaa"
    marker_path = parent / ".current_subtask"

    (child / "logs").mkdir()
    (child / "logs" / "implementer-1.log").write_text("crashed")
    (child / ".attempt").write_text("1")

    (marker_path).write_text(json.dumps({
        "role": "implementer",
        "provider": "mock",
        "pid": 99999,
        "start_time_iso": datetime.now(timezone.utc).isoformat(),
        "subtask_id": "20260424-impl-4-aaaa",
    }))

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with patch("mas.tick._role_running", return_value=False):
        _advance_one(env, parent)

    assert not marker_path.exists(), ".current_subtask not deleted after orphan result collected"