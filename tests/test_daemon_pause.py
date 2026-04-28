"""Failing tests for mas daemon pause/resume feature.

All tests must FAIL before implementation and PASS after.

Coverage:
  (a) daemon.pause() creates .mas/PAUSED, is idempotent
  (b) daemon.resume() removes .mas/PAUSED, is idempotent
  (c) both work without a running daemon (exit 0 / no exception)
  (d) run_tick() with .mas/PAUSED skips proposer/impl/tester/eval dispatch
  (e) _reap_workers() still runs while paused
  (f) existing result.json is processed (in-flight drains) while paused
  (g) once .mas/PAUSED removed, next tick dispatches normally
  (h) each paused tick emits exactly one INFO "paused (.mas/PAUSED present), skipping dispatch"
  (i) mas daemon status reports "paused: yes" / "paused: no"
  (j) pause does not interfere with mas show, mas stats, mas events
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from mas import board, daemon
from mas.cli import app
from mas.schemas import MasConfig, Plan, ProviderConfig, Result, RoleConfig, SubtaskSpec, Task
from mas.tick import run_tick

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg() -> MasConfig:
    return MasConfig(
        providers={"mock": ProviderConfig(cli="sh", max_concurrent=2, extra_args=[])},
        roles={
            "proposer": RoleConfig(provider="mock"),
            "orchestrator": RoleConfig(provider="mock"),
            "implementer": RoleConfig(provider="mock"),
            "tester": RoleConfig(provider="mock"),
            "evaluator": RoleConfig(provider="mock"),
        },
    )


def _setup_mas(tmp_path: Path) -> Path:
    """Create minimal .mas layout; return the .mas dir."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    (mas / "prompts").mkdir(exist_ok=True)
    for role in ("proposer", "orchestrator", "implementer", "tester", "evaluator"):
        (mas / "prompts" / f"{role}.md").write_text("goal=$goal")
    (mas / "config.yaml").write_text(
        "providers:\n  mock:\n    cli: sh\n    max_concurrent: 2\n    extra_args: []\n"
    )
    (mas / "roles.yaml").write_text(
        "roles:\n"
        "  proposer: {provider: mock}\n"
        "  orchestrator: {provider: mock}\n"
        "  implementer: {provider: mock}\n"
        "  tester: {provider: mock}\n"
        "  evaluator: {provider: mock}\n"
    )
    return mas


def _seed_task_with_completed_subtask(mas: Path) -> tuple[str, Path]:
    """Create a doing/ parent task with one completed implementer subtask.

    Returns (parent_id, parent_dir).  The parent is ready to be finalized
    by run_tick() without any new dispatch — _finalize_parent() just needs
    worktree.commit_changes / worktree.prune to be mocked.
    """
    parent_id = "20260428-drain-aaaa"
    parent = board.task_dir(mas, "doing", parent_id)
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="drain test"))
    (parent / "worktree").mkdir()

    child_id = "20260428-drain-impl-c1aa"
    plan = Plan(
        parent_id=parent_id,
        summary="drain",
        subtasks=[SubtaskSpec(id=child_id, role="implementer", goal="do the work")],
    )
    (parent / "plan.json").write_text(plan.model_dump_json())

    child_dir = parent / "subtasks" / child_id
    child_dir.mkdir(parents=True)
    board.write_task(child_dir, Task(id=child_id, role="implementer", goal="do the work"))
    child_result = Result(
        task_id=child_id, status="success", summary="done", duration_s=1.0
    )
    (child_dir / "result.json").write_text(child_result.model_dump_json())
    return parent_id, parent


# ---------------------------------------------------------------------------
# (a) + (c) daemon.pause()
# ---------------------------------------------------------------------------

class TestDaemonPause:
    def test_pause_creates_paused_file(self, tmp_path):
        """daemon.pause() must create .mas/PAUSED."""
        mas = _setup_mas(tmp_path)
        with patch("mas.config.project_dir", return_value=mas):
            daemon.pause(tmp_path)  # raises NotImplementedError until implemented
        assert (mas / "PAUSED").exists()

    def test_pause_is_idempotent(self, tmp_path):
        """Calling pause() twice must not raise."""
        mas = _setup_mas(tmp_path)
        (mas / "PAUSED").touch()  # already paused
        with patch("mas.config.project_dir", return_value=mas):
            daemon.pause(tmp_path)  # second call — must not raise
        assert (mas / "PAUSED").exists()

    def test_pause_does_not_require_running_daemon(self, tmp_path):
        """pause() works even when no daemon.pid file exists."""
        mas = _setup_mas(tmp_path)
        assert not (mas / daemon.PID_FILENAME).exists()
        with patch("mas.config.project_dir", return_value=mas):
            daemon.pause(tmp_path)  # raises NotImplementedError until implemented
        assert (mas / "PAUSED").exists()


# ---------------------------------------------------------------------------
# (b) + (c) daemon.resume()
# ---------------------------------------------------------------------------

class TestDaemonResume:
    def test_resume_removes_paused_file(self, tmp_path):
        """daemon.resume() must remove .mas/PAUSED."""
        mas = _setup_mas(tmp_path)
        (mas / "PAUSED").touch()
        with patch("mas.config.project_dir", return_value=mas):
            daemon.resume(tmp_path)  # raises NotImplementedError until implemented
        assert not (mas / "PAUSED").exists()

    def test_resume_is_idempotent(self, tmp_path):
        """Calling resume() when not paused must not raise."""
        mas = _setup_mas(tmp_path)
        assert not (mas / "PAUSED").exists()
        with patch("mas.config.project_dir", return_value=mas):
            daemon.resume(tmp_path)  # raises NotImplementedError until implemented

    def test_resume_does_not_require_running_daemon(self, tmp_path):
        """resume() works even when no daemon.pid file exists."""
        mas = _setup_mas(tmp_path)
        (mas / "PAUSED").touch()
        assert not (mas / daemon.PID_FILENAME).exists()
        with patch("mas.config.project_dir", return_value=mas):
            daemon.resume(tmp_path)  # raises NotImplementedError until implemented
        assert not (mas / "PAUSED").exists()


# ---------------------------------------------------------------------------
# daemon.is_paused()  — tested via direct API (used by status + tick)
# ---------------------------------------------------------------------------

class TestIsPaused:
    def test_is_paused_true_when_marker_present(self, tmp_path):
        """is_paused() returns True when .mas/PAUSED exists."""
        mas = _setup_mas(tmp_path)
        (mas / "PAUSED").touch()
        with patch("mas.config.project_dir", return_value=mas):
            result = daemon.is_paused(tmp_path)  # NotImplementedError until implemented
        assert result is True

    def test_is_paused_false_when_marker_absent(self, tmp_path):
        """is_paused() returns False when .mas/PAUSED is absent."""
        mas = _setup_mas(tmp_path)
        assert not (mas / "PAUSED").exists()
        with patch("mas.config.project_dir", return_value=mas):
            result = daemon.is_paused(tmp_path)  # NotImplementedError until implemented
        assert result is False


# ---------------------------------------------------------------------------
# (i) CLI: mas daemon status shows paused state
# ---------------------------------------------------------------------------

class TestCliDaemonStatus:
    def test_status_shows_paused_yes_no_daemon(self, tmp_path, monkeypatch):
        """mas daemon status shows 'paused: yes' when PAUSED present and no daemon."""
        mas = _setup_mas(tmp_path)
        (mas / "PAUSED").touch()
        monkeypatch.chdir(tmp_path)
        with patch("mas.daemon.status", return_value=(None, False)):
            result = runner.invoke(app, ["daemon", "status"])
        assert result.exit_code == 0, result.output
        assert "paused: yes" in result.output  # fails NOW: no paused output

    def test_status_shows_paused_no_no_daemon(self, tmp_path, monkeypatch):
        """mas daemon status shows 'paused: no' when PAUSED absent and no daemon."""
        mas = _setup_mas(tmp_path)
        monkeypatch.chdir(tmp_path)
        with patch("mas.daemon.status", return_value=(None, False)):
            result = runner.invoke(app, ["daemon", "status"])
        assert result.exit_code == 0, result.output
        assert "paused: no" in result.output  # fails NOW

    def test_status_shows_paused_yes_daemon_running(self, tmp_path, monkeypatch):
        """mas daemon status shows 'paused: yes' even when daemon is running."""
        mas = _setup_mas(tmp_path)
        (mas / "PAUSED").touch()
        monkeypatch.chdir(tmp_path)
        with patch("mas.daemon.status", return_value=(12345, True)):
            result = runner.invoke(app, ["daemon", "status"])
        assert result.exit_code == 0, result.output
        assert "paused: yes" in result.output  # fails NOW

    def test_status_shows_paused_no_daemon_running(self, tmp_path, monkeypatch):
        """mas daemon status shows 'paused: no' when daemon running but not paused."""
        mas = _setup_mas(tmp_path)
        monkeypatch.chdir(tmp_path)
        with patch("mas.daemon.status", return_value=(12345, True)):
            result = runner.invoke(app, ["daemon", "status"])
        assert result.exit_code == 0, result.output
        assert "paused: no" in result.output  # fails NOW


# ---------------------------------------------------------------------------
# (d) + (e) run_tick() behaviour when paused — dispatch / reaper
# ---------------------------------------------------------------------------

class TestPausedTickDispatch:

    def test_paused_tick_skips_proposer_dispatch(self, tmp_path, caplog):
        """run_tick() with .mas/PAUSED must NOT call _maybe_dispatch_proposer."""
        mas = _setup_mas(tmp_path)
        (mas / "PAUSED").touch()
        cfg = _cfg()
        lock_mock = MagicMock()

        with patch("mas.tick._acquire_lock", return_value=lock_mock), \
             patch("mas.tick.load_config", return_value=cfg), \
             patch("mas.tick.validate_config", return_value=[]), \
             patch("mas.tick.project_root", return_value=tmp_path), \
             patch("mas.tick._reap_workers"), \
             patch("mas.tick._advance_doing"), \
             patch("mas.tick._maybe_dispatch_proposer") as mock_proposer:
            with caplog.at_level(logging.INFO, logger="mas.tick"):
                run_tick(start=tmp_path, cfg=cfg)

        # Fails NOW: current code calls proposer regardless of PAUSED
        mock_proposer.assert_not_called()

    def test_paused_tick_reaper_still_runs(self, tmp_path):
        """run_tick() with .mas/PAUSED must still call _reap_workers."""
        mas = _setup_mas(tmp_path)
        (mas / "PAUSED").touch()
        cfg = _cfg()
        lock_mock = MagicMock()

        with patch("mas.tick._acquire_lock", return_value=lock_mock), \
             patch("mas.tick.load_config", return_value=cfg), \
             patch("mas.tick.validate_config", return_value=[]), \
             patch("mas.tick.project_root", return_value=tmp_path), \
             patch("mas.tick._reap_workers") as mock_reap, \
             patch("mas.tick._advance_doing"), \
             patch("mas.tick._maybe_dispatch_proposer") as mock_proposer:
            run_tick(start=tmp_path, cfg=cfg)

        mock_reap.assert_called_once()       # reaper must run — will verify after impl
        mock_proposer.assert_not_called()    # fails NOW

    def test_paused_tick_skips_dispatch_role_for_new_work(self, tmp_path):
        """run_tick() with .mas/PAUSED must NOT call _dispatch_role for pending subtasks."""
        mas = _setup_mas(tmp_path)
        (mas / "PAUSED").touch()
        cfg = _cfg()

        # A parent task whose next subtask has no result yet — needs dispatch.
        parent_id = "20260428-pending-aaaa"
        parent = board.task_dir(mas, "doing", parent_id)
        parent.mkdir(parents=True)
        board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="pending"))
        (parent / "worktree").mkdir()

        child_id = "20260428-pending-impl-c1aa"
        plan = Plan(
            parent_id=parent_id,
            summary="pending",
            subtasks=[SubtaskSpec(id=child_id, role="implementer", goal="work")],
        )
        (parent / "plan.json").write_text(plan.model_dump_json())
        child_dir = parent / "subtasks" / child_id
        child_dir.mkdir(parents=True)
        board.write_task(child_dir, Task(id=child_id, role="implementer", goal="work"))
        # No result.json — subtask is pending dispatch.

        lock_mock = MagicMock()
        with patch("mas.tick._acquire_lock", return_value=lock_mock), \
             patch("mas.tick.load_config", return_value=cfg), \
             patch("mas.tick.validate_config", return_value=[]), \
             patch("mas.tick.project_root", return_value=tmp_path), \
             patch("mas.tick._dispatch_role", return_value=99999) as mock_dispatch, \
             patch("mas.tick._maybe_dispatch_proposer") as mock_proposer:
            run_tick(start=tmp_path, cfg=cfg)

        # Fails NOW: current code dispatches the pending subtask regardless of PAUSED
        mock_dispatch.assert_not_called()
        mock_proposer.assert_not_called()

    def test_paused_tick_skips_revision_cycle_dispatch(self, tmp_path):
        """run_tick() with .mas/PAUSED must NOT dispatch revision-cycle subtasks."""
        mas = _setup_mas(tmp_path)
        (mas / "PAUSED").touch()
        cfg = _cfg()

        # Parent whose plan already has a revision cycle appended (cycle 1).
        # The evaluator from cycle 0 already returned needs_revision and
        # _append_revision_cycle already ran (plan.json updated). Now the first
        # revision subtask (rev-1-tester) is pending — a paused tick must NOT dispatch it.
        parent_id = "20260428-revision-aaaa"
        parent = board.task_dir(mas, "doing", parent_id)
        parent.mkdir(parents=True)
        board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="revision"))
        (parent / "worktree").mkdir()

        impl_id = "orig-impl-1"
        eval_id = "orig-eval-1"
        rev_tester_id = "rev-1-tester"

        plan = Plan(
            parent_id=parent_id,
            summary="revision",
            subtasks=[
                SubtaskSpec(id=impl_id, role="implementer", goal="implement"),
                SubtaskSpec(id=eval_id, role="evaluator", goal="evaluate"),
                SubtaskSpec(id=rev_tester_id, role="tester", goal="augment tests (cycle 1)"),
            ],
            revision_feedback={"rev-1": "needs more tests"},
        )
        (parent / "plan.json").write_text(plan.model_dump_json())

        # Original impl done
        impl_dir = parent / "subtasks" / impl_id
        impl_dir.mkdir(parents=True)
        (impl_dir / "result.json").write_text(
            Result(task_id=impl_id, status="success", summary="done", duration_s=1.0).model_dump_json()
        )
        # Original evaluator: needs_revision (superseded by rev cycle already appended)
        eval_dir = parent / "subtasks" / eval_id
        eval_dir.mkdir(parents=True)
        (eval_dir / "result.json").write_text(
            Result(
                task_id=eval_id, status="needs_revision", summary="needs work",
                verdict="needs_revision", duration_s=1.0,
            ).model_dump_json()
        )
        # rev-1-tester: no result — pending dispatch
        rev_dir = parent / "subtasks" / rev_tester_id
        rev_dir.mkdir(parents=True)

        lock_mock = MagicMock()
        with patch("mas.tick._acquire_lock", return_value=lock_mock), \
             patch("mas.tick.load_config", return_value=cfg), \
             patch("mas.tick.validate_config", return_value=[]), \
             patch("mas.tick.project_root", return_value=tmp_path), \
             patch("mas.tick._dispatch_role", return_value=99999) as mock_dispatch, \
             patch("mas.tick._maybe_dispatch_proposer") as mock_proposer:
            run_tick(start=tmp_path, cfg=cfg)

        # Fails NOW: _dispatch_role IS called for rev-1-tester (no PAUSED check)
        mock_dispatch.assert_not_called()
        mock_proposer.assert_not_called()


# ---------------------------------------------------------------------------
# (h) paused tick emits exactly one INFO log line
# ---------------------------------------------------------------------------

class TestPausedTickLog:
    def test_emits_exactly_one_paused_log_line(self, tmp_path, caplog):
        """Each paused tick emits exactly one 'paused (.mas/PAUSED present), skipping dispatch'."""
        mas = _setup_mas(tmp_path)
        (mas / "PAUSED").touch()
        cfg = _cfg()
        lock_mock = MagicMock()

        with patch("mas.tick._acquire_lock", return_value=lock_mock), \
             patch("mas.tick.load_config", return_value=cfg), \
             patch("mas.tick.validate_config", return_value=[]), \
             patch("mas.tick.project_root", return_value=tmp_path), \
             patch("mas.tick._reap_workers"), \
             patch("mas.tick._advance_doing"), \
             patch("mas.tick._maybe_dispatch_proposer"):
            with caplog.at_level(logging.INFO, logger="mas.tick"):
                run_tick(start=tmp_path, cfg=cfg)

        paused_lines = [
            r for r in caplog.records
            if "paused (.mas/PAUSED present), skipping dispatch" in r.getMessage()
        ]
        # Fails NOW: no such log line exists
        assert len(paused_lines) == 1

    def test_unpaused_tick_emits_no_paused_log_line(self, tmp_path, caplog):
        """A normal (unpaused) tick must NOT emit the paused log line."""
        mas = _setup_mas(tmp_path)
        assert not (mas / "PAUSED").exists()
        cfg = _cfg()
        lock_mock = MagicMock()

        with patch("mas.tick._acquire_lock", return_value=lock_mock), \
             patch("mas.tick.load_config", return_value=cfg), \
             patch("mas.tick.validate_config", return_value=[]), \
             patch("mas.tick.project_root", return_value=tmp_path), \
             patch("mas.tick._reap_workers"), \
             patch("mas.tick._advance_doing"), \
             patch("mas.tick._maybe_dispatch_proposer"):
            with caplog.at_level(logging.INFO, logger="mas.tick"):
                run_tick(start=tmp_path, cfg=cfg)

        paused_lines = [
            r for r in caplog.records
            if "paused (.mas/PAUSED present), skipping dispatch" in r.getMessage()
        ]
        assert len(paused_lines) == 0  # passes now, guards against regression


# ---------------------------------------------------------------------------
# (f) in-flight results still processed when paused
# ---------------------------------------------------------------------------

class TestPausedTickDrainsInFlight:
    def test_completed_subtask_result_processed_while_paused(self, tmp_path, caplog):
        """When paused, a task whose subtasks are all done is still finalized."""
        mas = _setup_mas(tmp_path)
        (mas / "PAUSED").touch()
        cfg = _cfg()

        parent_id, _ = _seed_task_with_completed_subtask(mas)

        lock_mock = MagicMock()
        with patch("mas.tick._acquire_lock", return_value=lock_mock), \
             patch("mas.tick.load_config", return_value=cfg), \
             patch("mas.tick.validate_config", return_value=[]), \
             patch("mas.tick.project_root", return_value=tmp_path), \
             patch("mas.tick.worktree.commit_changes"), \
             patch("mas.tick.worktree.prune"), \
             patch("mas.tick._dispatch_role") as mock_dispatch, \
             patch("mas.tick._maybe_dispatch_proposer") as mock_proposer:
            with caplog.at_level(logging.INFO, logger="mas.tick"):
                run_tick(start=tmp_path, cfg=cfg)

        # No new dispatch (finalize path never calls _dispatch_role)
        mock_dispatch.assert_not_called()    # passes currently (finalize doesn't dispatch)
        mock_proposer.assert_not_called()    # fails NOW (proposer IS called)

        # Parent was finalized (moved to done/) — drain happened
        assert (mas / "tasks" / "done" / parent_id).exists()

        # Paused log was emitted
        paused_lines = [
            r for r in caplog.records
            if "paused (.mas/PAUSED present), skipping dispatch" in r.getMessage()
        ]
        assert len(paused_lines) == 1  # fails NOW


# ---------------------------------------------------------------------------
# (g) normal dispatch resumes after PAUSED removed
# ---------------------------------------------------------------------------

class TestResumedTickDispatchesNormally:
    def test_dispatch_resumes_after_paused_file_removed(self, tmp_path):
        """Once .mas/PAUSED is deleted, the next tick calls _maybe_dispatch_proposer."""
        mas = _setup_mas(tmp_path)
        cfg = _cfg()
        lock_mock = MagicMock()

        # Tick 1: paused — proposer must NOT run
        (mas / "PAUSED").touch()
        with patch("mas.tick._acquire_lock", return_value=lock_mock), \
             patch("mas.tick.load_config", return_value=cfg), \
             patch("mas.tick.validate_config", return_value=[]), \
             patch("mas.tick.project_root", return_value=tmp_path), \
             patch("mas.tick._reap_workers"), \
             patch("mas.tick._advance_doing"), \
             patch("mas.tick._maybe_dispatch_proposer") as mock_paused_proposer:
            run_tick(start=tmp_path, cfg=cfg)
        # Fails NOW: proposer IS called (no PAUSED check)
        mock_paused_proposer.assert_not_called()

        # Tick 2: resumed — proposer MUST run
        (mas / "PAUSED").unlink()
        with patch("mas.tick._acquire_lock", return_value=lock_mock), \
             patch("mas.tick.load_config", return_value=cfg), \
             patch("mas.tick.validate_config", return_value=[]), \
             patch("mas.tick.project_root", return_value=tmp_path), \
             patch("mas.tick._reap_workers"), \
             patch("mas.tick._advance_doing"), \
             patch("mas.tick._maybe_dispatch_proposer") as mock_resumed_proposer:
            run_tick(start=tmp_path, cfg=cfg)
        mock_resumed_proposer.assert_called_once()  # passes now — regression guard


# ---------------------------------------------------------------------------
# (j) pause does not interfere with other CLI commands
# ---------------------------------------------------------------------------

class TestPauseDoesNotInterfereWithOtherCommands:
    def test_mas_show_works_when_paused(self, tmp_path, monkeypatch):
        """mas show exits normally even when .mas/PAUSED is present."""
        mas = _setup_mas(tmp_path)
        (mas / "PAUSED").touch()
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["show"])
        assert result.exit_code != 2  # command exists and runs; exit_code may be 0 or 1

    def test_mas_stats_works_when_paused(self, tmp_path, monkeypatch):
        """mas stats exits normally even when .mas/PAUSED is present."""
        mas = _setup_mas(tmp_path)
        (mas / "PAUSED").touch()
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["stats"])
        assert result.exit_code != 2

    def test_mas_events_works_when_paused(self, tmp_path, monkeypatch):
        """mas events exits normally even when .mas/PAUSED is present."""
        mas = _setup_mas(tmp_path)
        (mas / "PAUSED").touch()
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["events"])
        assert result.exit_code != 2
