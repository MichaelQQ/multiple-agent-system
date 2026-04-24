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
from mas.schemas import Plan, Result, SubtaskSpec, Task

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


# ---------------------------------------------------------------------------
# Tests for `mas show --json` (board view)
# ---------------------------------------------------------------------------


class TestShowJsonBoard:
    def test_show_json_empty_board(self, tmp_board, monkeypatch):
        """(a) Empty board produces []."""
        monkeypatch.chdir(tmp_board.parent)
        result = runner.invoke(app, ["show", "--json"])
        assert result.exit_code == 0, (
            f"Expected exit 0, got {result.exit_code}. Output:\n{result.output}"
        )
        data = json.loads(result.output)
        assert data == [], f"Expected [], got {data!r}"

    def test_show_json_multi_column_board_keys(self, tmp_board, monkeypatch):
        """(b) Each element in the JSON array has required keys with correct types."""
        monkeypatch.chdir(tmp_board.parent)
        _write_task(tmp_board, "proposed", "20260423-prop1-aaaa", role="orchestrator", goal="proposed goal")
        _write_task(tmp_board, "doing", "20260423-do11-bbbb", role="implementer", goal="doing goal")
        _write_task(tmp_board, "done", "20260423-done-cccc", role="tester", goal="done goal")
        _write_task(tmp_board, "failed", "20260423-fail-dddd", role="evaluator", goal="failed goal")

        result = runner.invoke(app, ["show", "--json"])
        assert result.exit_code == 0, f"Expected exit 0:\n{result.output}"
        data = json.loads(result.output)
        assert isinstance(data, list), f"Expected list, got {type(data)}"
        assert len(data) == 4, f"Expected 4 tasks, got {len(data)}"

        for item in data:
            for key in ("column", "id", "role", "goal", "progress", "transitions"):
                assert key in item, f"Missing key {key!r} in {item}"
            assert isinstance(item["transitions"], list), (
                f"Expected transitions to be list, got {type(item['transitions'])}"
            )
            for txn in item["transitions"]:
                for tkey in ("timestamp", "from_state", "to_state", "reason"):
                    assert tkey in txn, f"Missing key {tkey!r} in transition {txn}"

    def test_show_json_column_ordering(self, tmp_board, monkeypatch):
        """(b) Array is ordered proposed→doing→done→failed regardless of insertion order."""
        monkeypatch.chdir(tmp_board.parent)
        _write_task(tmp_board, "failed", "20260423-fail-aaaa", role="evaluator", goal="failed")
        _write_task(tmp_board, "done", "20260423-done-bbbb", role="tester", goal="done")
        _write_task(tmp_board, "doing", "20260423-do22-cccc", role="implementer", goal="doing")
        _write_task(tmp_board, "proposed", "20260423-prop-dddd", role="orchestrator", goal="proposed")

        result = runner.invoke(app, ["show", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        columns = [item["column"] for item in data]
        assert columns == ["proposed", "doing", "done", "failed"], (
            f"Expected column order [proposed, doing, done, failed], got {columns}"
        )

    def test_show_json_board_values(self, tmp_board, monkeypatch):
        """(b) JSON elements carry correct column, id, role, goal values."""
        monkeypatch.chdir(tmp_board.parent)
        _write_task(tmp_board, "proposed", "20260423-mypr-aaaa", role="orchestrator", goal="my proposed goal")

        result = runner.invoke(app, ["show", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 1
        item = data[0]
        assert item["column"] == "proposed"
        assert item["id"] == "20260423-mypr-aaaa"
        assert item["role"] == "orchestrator"
        assert item["goal"] == "my proposed goal"


# ---------------------------------------------------------------------------
# Tests for `mas show <task-id> --json` (task view)
# ---------------------------------------------------------------------------


class TestShowJsonTask:
    def test_show_task_json_with_plan(self, tmp_board, monkeypatch):
        """(c) Doing task with plan returns object with task_id/column/role/goal/result/plan."""
        monkeypatch.chdir(tmp_board.parent)
        task_id = "20260423-main1-abcd"
        tdir = _write_task(tmp_board, "doing", task_id, role="orchestrator", goal="main task goal")

        impl_id = "20260424-impl1-cafe"
        test_id = "20260424-test1-babe"
        plan = Plan(
            parent_id=task_id,
            summary="test plan summary",
            subtasks=[
                SubtaskSpec(id=impl_id, role="implementer", goal="write the code"),
                SubtaskSpec(id=test_id, role="tester", goal="test the code"),
            ],
        )
        (tdir / "plan.json").write_text(plan.model_dump_json())

        impl_dir = tdir / "subtasks" / impl_id
        impl_dir.mkdir(parents=True, exist_ok=True)
        impl_result = Result(
            task_id=impl_id,
            status="success",
            summary="implementation complete",
            verdict="pass",
        )
        (impl_dir / "result.json").write_text(impl_result.model_dump_json())

        (tdir / "subtasks" / test_id).mkdir(parents=True, exist_ok=True)

        result = runner.invoke(app, ["show", task_id, "--json"])
        assert result.exit_code == 0, f"Expected exit 0:\n{result.output}"
        data = json.loads(result.output)

        for key in ("task_id", "column", "role", "goal", "result", "plan"):
            assert key in data, f"Missing top-level key {key!r}; got keys: {list(data.keys())}"

        assert data["task_id"] == task_id
        assert data["column"] == "doing"
        assert data["role"] == "orchestrator"
        assert data["goal"] == "main task goal"

        assert data["plan"] is not None, "Expected plan to be non-null"
        assert "subtasks" in data["plan"], "Expected 'subtasks' key in plan"

        subtasks = data["plan"]["subtasks"]
        assert len(subtasks) == 2, f"Expected 2 subtasks, got {len(subtasks)}"

        for st in subtasks:
            for st_key in ("id", "role", "goal", "status", "summary"):
                assert st_key in st, f"Missing key {st_key!r} in subtask {st}"

        by_id = {st["id"]: st for st in subtasks}
        assert by_id[impl_id]["status"] == "pass", (
            f"Expected 'pass' for completed subtask, got {by_id[impl_id]['status']!r}"
        )
        assert by_id[test_id]["status"] == "pending", (
            f"Expected 'pending' for subtask with no result, got {by_id[test_id]['status']!r}"
        )

    def test_show_task_json_without_plan(self, tmp_board, monkeypatch):
        """(d) Doing task without plan.json returns same shape with plan: null."""
        monkeypatch.chdir(tmp_board.parent)
        task_id = "20260423-main2-beef"
        _write_task(tmp_board, "doing", task_id, role="implementer", goal="task without plan")

        result = runner.invoke(app, ["show", task_id, "--json"])
        assert result.exit_code == 0, f"Expected exit 0:\n{result.output}"
        data = json.loads(result.output)

        for key in ("task_id", "column", "role", "goal", "result", "plan"):
            assert key in data, f"Missing top-level key {key!r}; got keys: {list(data.keys())}"

        assert data["task_id"] == task_id
        assert data["plan"] is None, f"Expected plan to be null, got {data['plan']!r}"

    def test_show_task_json_unknown_id(self, tmp_board, monkeypatch):
        """(e) Unknown task ID exits non-zero and prints {"error": "not found: <id>"} on stdout."""
        monkeypatch.chdir(tmp_board.parent)
        unknown_id = "20260423-noexist-zzzz"
        result = runner.invoke(app, ["show", unknown_id, "--json"])
        assert result.exit_code != 0, (
            f"Expected non-zero exit for unknown task, got {result.exit_code}"
        )
        output = result.output.strip()
        assert output, f"Expected non-empty output for unknown task, got empty string"
        data = json.loads(output)
        assert data == {"error": f"not found: {unknown_id}"}, (
            f"Expected error JSON, got {data!r}"
        )
