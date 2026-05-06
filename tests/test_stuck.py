"""Tests for stuck-task detection in the tick loop.

Covers:
(a) StuckDetectionConfig schema with defaults
(b) Task.stuck bool field defaults to False
(c) _is_task_stuck() when .current_subtask marker age exceeds timeout
(d) _is_task_stuck() when no result and task idle too long
(e) _is_task_stuck() when within thresholds
(f) tick _advance_doing integration: stuck flag set on parent
(g) config validation rejects negative timeout values
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mas import board, transitions
from mas.schemas import (
    MasConfig,
    Plan,
    ProviderConfig,
    Result,
    RoleConfig,
    StuckDetectionConfig,
    SubtaskSpec,
    Task,
)
from mas.tick import (
    TickEnv,
    _advance_one,
    _is_task_stuck,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(**overrides) -> MasConfig:
    base = MasConfig(
        providers={"mock": ProviderConfig(cli="sh", max_concurrent=2)},
        roles={
            "proposer": RoleConfig(provider="mock"),
            "orchestrator": RoleConfig(provider="mock"),
            "implementer": RoleConfig(provider="mock"),
            "tester": RoleConfig(provider="mock"),
            "evaluator": RoleConfig(provider="mock"),
        },
        stuck_detection=StuckDetectionConfig(),
    )
    if overrides:
        base = MasConfig.model_validate({**base.model_dump(), **overrides})
    return base


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
    (parent / "subtasks" / child_id).mkdir(parents=True)
    return parent


def _backdate_file(path: Path, hours_ago: float) -> None:
    """Set mtime of a file to `hours_ago` hours in the past."""
    target = time.time() - (hours_ago * 3600)
    os.utime(path, (target, target))


def _backdate_transitions(task_dir: Path, hours_ago: float) -> None:
    """Rewrite .transitions.log so the first entry timestamp is hours_ago in the past."""
    log_path = task_dir / ".transitions.log"
    if not log_path.exists():
        return
    lines = log_path.read_text().splitlines()
    if not lines:
        return
    old_ts = lines[0].split("|", 1)[0]
    new_ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    lines[0] = lines[0].replace(old_ts, new_ts, 1)
    log_path.write_text("\n".join(lines) + "\n")


def _write_current_subtask_marker(parent_dir: Path, hours_ago: float) -> None:
    """Write a .current_subtask marker with a backdated start_time_iso."""
    start = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    marker = {
        "role": "implementer",
        "provider": "mock",
        "pid": 12345,
        "start_time_iso": start.isoformat().replace("+00:00", "Z"),
        "subtask_id": "some-subtask",
    }
    (parent_dir / ".current_subtask").write_text(json.dumps(marker, indent=2))


# ---------------------------------------------------------------------------
# (a) StuckDetectionConfig schema with defaults
# ---------------------------------------------------------------------------

def test_stuck_detection_config_defaults():
    """StuckDetectionConfig has the expected default timeout values."""
    cfg = StuckDetectionConfig()
    assert cfg.current_subtask_timeout_hours == 8
    assert cfg.task_idle_timeout_hours == 24


# ---------------------------------------------------------------------------
# (g) Config validation rejects negative timeout values
# ---------------------------------------------------------------------------

def test_stuck_detection_config_rejects_negative_current_subtask_timeout():
    """Negative current_subtask_timeout_hours raises a validation error."""
    with pytest.raises(Exception):  # pydantic.ValidationError
        StuckDetectionConfig(current_subtask_timeout_hours=-1)


def test_stuck_detection_config_rejects_negative_task_idle_timeout():
    """Negative task_idle_timeout_hours raises a validation error."""
    with pytest.raises(Exception):  # pydantic.ValidationError
        StuckDetectionConfig(task_idle_timeout_hours=-1)


# ---------------------------------------------------------------------------
# MasConfig.stuck_detection field
# ---------------------------------------------------------------------------

def test_mas_config_stuck_detection_default():
    """MasConfig.stuck_detection defaults to a StuckDetectionConfig instance."""
    cfg = _cfg()
    assert isinstance(cfg.stuck_detection, StuckDetectionConfig)
    assert cfg.stuck_detection.current_subtask_timeout_hours == 8
    assert cfg.stuck_detection.task_idle_timeout_hours == 24


# ---------------------------------------------------------------------------
# (b) Task.stuck bool field defaults to False
# ---------------------------------------------------------------------------

def test_task_stuck_defaults_false():
    """Task.stuck is False by default."""
    task = Task(id="20260506-test-aaaa", role="orchestrator", goal="g")
    assert task.stuck is False


def test_task_stuck_can_be_set_true():
    """Task.stuck can be set to True."""
    task = Task(id="20260506-test-aaaa", role="orchestrator", goal="g", stuck=True)
    assert task.stuck is True


# ---------------------------------------------------------------------------
# (c) _is_task_stuck() — current_subtask marker expired
# ---------------------------------------------------------------------------

def test_is_task_stuck_subtask_marker_expired(tmp_path: Path):
    """When .current_subtask marker age exceeds current_subtask_timeout_hours,
    _is_task_stuck returns (True, reason)."""
    parent = tmp_path / "task"
    parent.mkdir()
    _write_current_subtask_marker(parent, hours_ago=10)  # 10h > 8h default

    config = StuckDetectionConfig()
    stuck, reason = _is_task_stuck(parent, config)

    assert stuck is True
    assert "current_subtask" in reason.lower() or "subtask" in reason.lower()


def test_is_task_stuck_subtask_marker_not_expired(tmp_path: Path):
    """When .current_subtask marker age is within the threshold, not stuck."""
    parent = tmp_path / "task"
    parent.mkdir()
    _write_current_subtask_marker(parent, hours_ago=2)  # 2h < 8h default

    config = StuckDetectionConfig()
    stuck, reason = _is_task_stuck(parent, config)

    assert stuck is False
    assert reason == ""


# ---------------------------------------------------------------------------
# (d) _is_task_stuck() — task idle too long, no result
# ---------------------------------------------------------------------------

def test_is_task_stuck_task_idle_no_result(tmp_path: Path):
    """When no .current_subtask marker and no subtask result has been written,
    and the task has been in doing/ longer than task_idle_timeout_hours,
    _is_task_stuck returns (True, reason)."""
    parent = tmp_path / "task"
    parent.mkdir()
    # Create transitions.log with a backdated first entry
    transitions.log_transition(parent, "none", "doing", "created")
    _backdate_transitions(parent, hours_ago=30)  # 30h > 24h default

    config = StuckDetectionConfig()
    stuck, reason = _is_task_stuck(parent, config)

    assert stuck is True
    assert "idle" in reason.lower() or "doing" in reason.lower()


def test_is_task_stuck_idle_but_has_result(tmp_path: Path):
    """When the task is idle but a subtask result exists, not stuck."""
    parent = tmp_path / "task"
    parent.mkdir()
    transitions.log_transition(parent, "none", "doing", "created")
    _backdate_transitions(parent, hours_ago=30)

    # Simulate that a subtask result has been written
    subtasks = parent / "subtasks"
    subtasks.mkdir()
    child = subtasks / "child-1"
    child.mkdir()
    r = Result(task_id="child-1", status="success", summary="done")
    (child / "result.json").write_text(r.model_dump_json())

    config = StuckDetectionConfig()
    stuck, reason = _is_task_stuck(parent, config)

    assert stuck is False
    assert reason == ""


def test_is_task_stuck_idle_but_has_current_subtask(tmp_path: Path):
    """When the task is idle but a current_subtask marker exists (recent),
    the subtask-marker check takes precedence and it is not stuck."""
    parent = tmp_path / "task"
    parent.mkdir()
    transitions.log_transition(parent, "none", "doing", "created")
    _backdate_transitions(parent, hours_ago=30)

    # Recent current_subtask marker (2h < 8h)
    _write_current_subtask_marker(parent, hours_ago=2)

    config = StuckDetectionConfig()
    stuck, reason = _is_task_stuck(parent, config)

    assert stuck is False
    assert reason == ""


# ---------------------------------------------------------------------------
# (e) _is_task_stuck() — within thresholds
# ---------------------------------------------------------------------------

def test_is_task_stuck_within_thresholds(tmp_path: Path):
    """When both marker age and idle time are within thresholds, not stuck."""
    parent = tmp_path / "task"
    parent.mkdir()
    transitions.log_transition(parent, "none", "doing", "created")
    # Fresh transitions
    _write_current_subtask_marker(parent, hours_ago=1)  # 1h < 8h

    config = StuckDetectionConfig()
    stuck, reason = _is_task_stuck(parent, config)

    assert stuck is False
    assert reason == ""


def test_is_task_stuck_no_marker_fresh_transitions(tmp_path: Path):
    """No .current_subtask marker and fresh transitions → not stuck."""
    parent = tmp_path / "task"
    parent.mkdir()
    transitions.log_transition(parent, "none", "doing", "created")

    config = StuckDetectionConfig()
    stuck, reason = _is_task_stuck(parent, config)

    assert stuck is False
    assert reason == ""


# ---------------------------------------------------------------------------
# (f) tick _advance_doing integration: stuck flag set on parent
# ---------------------------------------------------------------------------

def test_advance_one_sets_stuck_flag_when_subtask_expired(tmp_path: Path):
    """When _is_task_stuck detects a stuck task (expired subtask marker),
    _advance_one logs a WARNING and sets task.stuck=True in task.json."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)

    parent_id = "20260506-stuck-aaaa"
    parent = _seed_parent_with_plan(mas, parent_id, "20260506-impl-aaaa")

    # Write an expired .current_subtask marker (10h > 8h)
    _write_current_subtask_marker(parent, hours_ago=10)

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with patch("mas.tick.get_adapter") as mock_get, \
         patch("mas.board.count_active_pids", return_value=0), \
         patch("mas.board.write_pid"), \
         patch("mas.tick._role_running", return_value=False), \
         patch("mas.tick._pid_alive", return_value=False), \
         patch("mas.tick._worker_orphaned", return_value=False):
        mock_adapter = MagicMock()
        mock_adapter.dispatch.return_value = MagicMock(pid=12345)
        mock_adapter.agentic = False
        mock_get.return_value.return_value = mock_adapter

        _advance_one(env, parent)

    # The task.json should have stuck=True
    task = board.read_task(parent)
    assert task.stuck is True


def test_advance_one_does_not_set_stuck_when_not_stuck(tmp_path: Path, caplog):
    """When the task is not stuck, _advance_one does not set stuck=True."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)

    parent_id = "20260506-ok-aaaa"
    parent = _seed_parent_with_plan(mas, parent_id, "20260506-impl-bbbb")

    # Fresh .current_subtask marker (1h < 8h)
    _write_current_subtask_marker(parent, hours_ago=1)

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    with patch("mas.tick.get_adapter") as mock_get, \
         patch("mas.board.count_active_pids", return_value=0), \
         patch("mas.board.write_pid"), \
         patch("mas.tick._role_running", return_value=False), \
         patch("mas.tick._pid_alive", return_value=False), \
         patch("mas.tick._worker_orphaned", return_value=False):
        mock_adapter = MagicMock()
        mock_adapter.dispatch.return_value = MagicMock(pid=12345)
        mock_adapter.agentic = False
        mock_get.return_value.return_value = mock_adapter

        _advance_one(env, parent)

    task = board.read_task(parent)
    assert task.stuck is False
