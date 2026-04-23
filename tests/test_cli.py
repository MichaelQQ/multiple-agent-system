"""Tests for the `mas audit` CLI command.

All tests fail against the current stub implementation and should pass once
the real audit command is implemented.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from mas import board
from mas.cli import app
from mas.schemas import Task

runner = CliRunner()


@pytest.fixture(autouse=True)
def _wide_console():
    """Prevent Rich from truncating table output in the test runner."""
    from mas import cli

    original = cli.console
    cli.console = Console(width=200)
    yield
    cli.console = original


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_task(
    mas_dir: Path,
    column: str,
    task_id: str,
    role: str = "orchestrator",
    goal: str = "test task",
) -> Path:
    task = Task(id=task_id, role=role, goal=goal)
    d = mas_dir / "tasks" / column / task_id
    board.write_task(d, task)
    return d


def _write_audit(task_dir: Path, events: list[dict]) -> None:
    (task_dir / "audit.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n"
    )


_EVENTS = [
    {
        "timestamp": "2026-04-23T00:00:00+00:00",
        "event": "dispatch",
        "role": "implementer",
        "provider": "claude_code",
        "task_id": "20260423-audit-aaaa",
        "subtask_id": "sub-1",
        "status": None,
        "duration_s": None,
        "summary": "dispatched implementer",
        "details": {},
    },
    {
        "timestamp": "2026-04-23T00:01:00+00:00",
        "event": "completion",
        "role": "implementer",
        "provider": "claude_code",
        "task_id": "20260423-audit-aaaa",
        "subtask_id": "sub-1",
        "status": "success",
        "duration_s": 60.0,
        "summary": "implementer completed successfully",
        "details": {},
    },
    {
        "timestamp": "2026-04-23T00:02:00+00:00",
        "event": "dispatch",
        "role": "tester",
        "provider": "claude_code",
        "task_id": "20260423-audit-aaaa",
        "subtask_id": "sub-2",
        "status": None,
        "duration_s": None,
        "summary": "dispatched tester",
        "details": {},
    },
    {
        "timestamp": "2026-04-23T00:03:00+00:00",
        "event": "state_transition",
        "role": None,
        "provider": None,
        "task_id": "20260423-audit-aaaa",
        "subtask_id": None,
        "status": "success",
        "duration_s": None,
        "summary": "doing → done",
        "details": {"reason": "role_success"},
    },
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAuditCommand:
    def test_command_exists_and_succeeds_for_known_task(self, tmp_board, monkeypatch):
        """audit command is registered and exits 0 for a known task with audit events."""
        monkeypatch.chdir(tmp_board.parent)
        task_id = "20260423-audit-aaaa"
        d = _write_task(tmp_board, "done", task_id)
        _write_audit(d, _EVENTS[:1])

        result = runner.invoke(app, ["audit", task_id])
        assert result.exit_code != 2, (
            f"exit_code=2 means the command is not registered; output: {result.output}"
        )
        assert result.exit_code == 0, (
            f"Expected exit 0 for a known task, got {result.exit_code}. Output:\n{result.output}"
        )

    def test_shows_formatted_timeline(self, tmp_board, monkeypatch):
        """audit command prints a timeline including event types and roles."""
        monkeypatch.chdir(tmp_board.parent)
        task_id = "20260423-audit-aaaa"
        d = _write_task(tmp_board, "done", task_id)
        _write_audit(d, _EVENTS)

        result = runner.invoke(app, ["audit", task_id])
        assert result.exit_code == 0, (
            f"Expected exit 0, got {result.exit_code}. Output:\n{result.output}"
        )
        assert "dispatch" in result.output
        assert "completion" in result.output
        assert "implementer" in result.output

    def test_unknown_task_exits_nonzero(self, tmp_board, monkeypatch):
        """audit command exits non-zero and reports task not found for unknown id."""
        monkeypatch.chdir(tmp_board.parent)
        result = runner.invoke(app, ["audit", "20260423-noexist-zzzz"])
        assert result.exit_code != 0, "Expected non-zero exit for unknown task"
        output = result.output.lower()
        assert "not found" in output or "unknown" in output, (
            f"Expected 'not found' or 'unknown' in output; got:\n{result.output}"
        )

    def test_filter_by_role_hides_other_roles(self, tmp_board, monkeypatch):
        """--role implementer shows implementer events and hides tester events."""
        monkeypatch.chdir(tmp_board.parent)
        task_id = "20260423-audit-bbbb"
        d = _write_task(tmp_board, "done", task_id)
        _write_audit(d, _EVENTS)

        result = runner.invoke(app, ["audit", task_id, "--role", "implementer"])
        assert result.exit_code == 0, f"Expected exit 0: {result.output}"
        assert "implementer" in result.output
        assert "dispatched tester" not in result.output

    def test_filter_by_status_hides_other_statuses(self, tmp_board, monkeypatch):
        """--status success shows only success events."""
        monkeypatch.chdir(tmp_board.parent)
        task_id = "20260423-audit-cccc"
        d = _write_task(tmp_board, "done", task_id)
        _write_audit(d, _EVENTS)

        result = runner.invoke(app, ["audit", task_id, "--status", "success"])
        assert result.exit_code == 0, f"Expected exit 0: {result.output}"
        # The dispatch events have status=None and should not appear
        assert "dispatched implementer" not in result.output
        assert "dispatched tester" not in result.output

    def test_filter_by_since_hides_earlier_events(self, tmp_board, monkeypatch):
        """--since 2026-04-23T00:02:00Z shows only events at or after that time."""
        monkeypatch.chdir(tmp_board.parent)
        task_id = "20260423-audit-dddd"
        d = _write_task(tmp_board, "done", task_id)
        _write_audit(d, _EVENTS)

        result = runner.invoke(
            app, ["audit", task_id, "--since", "2026-04-23T00:02:00Z"]
        )
        assert result.exit_code == 0, f"Expected exit 0: {result.output}"
        # Events at 00:00 and 00:01 should not appear
        assert "dispatched implementer" not in result.output
        assert "implementer completed" not in result.output
        # Events at 00:02 and 00:03 should appear
        assert "tester" in result.output or "state_transition" in result.output

    def test_filter_by_until_hides_later_events(self, tmp_board, monkeypatch):
        """--until 2026-04-23T00:01:00Z shows only events at or before that time."""
        monkeypatch.chdir(tmp_board.parent)
        task_id = "20260423-audit-eeee"
        d = _write_task(tmp_board, "done", task_id)
        _write_audit(d, _EVENTS)

        result = runner.invoke(
            app, ["audit", task_id, "--until", "2026-04-23T00:01:00Z"]
        )
        assert result.exit_code == 0, f"Expected exit 0: {result.output}"
        assert "implementer" in result.output
        # tester event at 00:02 should not appear
        assert "dispatched tester" not in result.output

    def test_shows_events_for_all_subtasks(self, tmp_board, monkeypatch):
        """audit command shows events for all subtasks in the full timeline."""
        monkeypatch.chdir(tmp_board.parent)
        task_id = "20260423-audit-ffff"
        d = _write_task(tmp_board, "done", task_id)
        _write_audit(d, _EVENTS)

        result = runner.invoke(app, ["audit", task_id])
        assert result.exit_code == 0, f"Expected exit 0: {result.output}"
        # Both implementer (sub-1) and tester (sub-2) events should appear
        assert "implementer" in result.output
        assert "tester" in result.output
