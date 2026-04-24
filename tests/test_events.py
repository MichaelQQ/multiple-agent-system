"""Tests for the `mas events` CLI command (board-wide event stream).

These tests FAIL against the current unimplemented code and should PASS once
the real `events` command is implemented.

Covers:
  (a) empty board → empty output / no rows; --json prints nothing
  (b) multi-task aggregation sorted by timestamp ascending, task_id column present
  (c) each filter individually and combined (AND semantics):
      --task, --role, --status, --event, --since, --until
  (d) --json emits newline-delimited JSON preserving audit.jsonl field names
  (e) --follow picks up new events and exits cleanly on KeyboardInterrupt
  (f) malformed audit lines surface UserWarning (proves read_events reuse)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from mas.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _wide_console():
    """Prevent Rich from truncating table output in the test runner."""
    from mas import cli

    original = cli.console
    cli.console = Console(width=200)
    yield
    cli.console = original


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_task_dir(mas_dir: Path, column: str, task_id: str) -> Path:
    d = mas_dir / "tasks" / column / task_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_audit(task_dir: Path, events: list[dict]) -> None:
    (task_dir / "audit.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n"
    )


def _evt(
    task_id: str,
    ts: str,
    event: str = "dispatch",
    role: str = "implementer",
    status: str | None = None,
    summary: str = "",
) -> dict:
    return {
        "timestamp": ts,
        "event": event,
        "role": role,
        "provider": "claude_code",
        "task_id": task_id,
        "subtask_id": None,
        "status": status,
        "duration_s": None,
        "summary": summary,
        "details": {},
    }


# Shared task ID constants
_T1 = "20260423-task-aaaa"
_T2 = "20260423-task-bbbb"
_TF = "20260423-filt-cccc"

_FILTER_EVENTS = [
    _evt(_TF, "2026-04-23T00:00:00+00:00", event="dispatch",         role="implementer", status=None,      summary="impl dispatch"),
    _evt(_TF, "2026-04-23T00:01:00+00:00", event="completion",       role="implementer", status="success", summary="impl complete"),
    _evt(_TF, "2026-04-23T00:02:00+00:00", event="dispatch",         role="tester",      status=None,      summary="tester dispatch"),
    _evt(_TF, "2026-04-23T00:03:00+00:00", event="state_transition", role=None,          status="success", summary="doing→done"),
]

_EVENTS_T1 = [
    _evt(_T1, "2026-04-23T00:00:00+00:00", summary="task1 event1"),
    _evt(_T1, "2026-04-23T00:02:00+00:00", event="completion", status="success", summary="task1 event2"),
]
_EVENTS_T2 = [
    _evt(_T2, "2026-04-23T00:01:00+00:00", role="tester", summary="task2 event1"),
    _evt(_T2, "2026-04-23T00:03:00+00:00", event="state_transition", role="tester", status="success", summary="task2 event2"),
]


# ── (a) Empty board ───────────────────────────────────────────────────────────


class TestEventsEmpty:
    def test_empty_board_exits_zero(self, tmp_board, monkeypatch):
        """mas events on an empty board exits 0 and shows a table header."""
        monkeypatch.chdir(tmp_board.parent)
        result = runner.invoke(app, ["events"])
        assert result.exit_code != 2, (
            f"exit_code=2 means the command is not registered; output:\n{result.output}"
        )
        assert result.exit_code == 0, (
            f"Expected exit 0 on empty board, got {result.exit_code}.\n{result.output}"
        )
        out_lower = result.output.lower()
        assert "timestamp" in out_lower or "task_id" in out_lower or "event" in out_lower

    def test_empty_board_json_no_output(self, tmp_board, monkeypatch):
        """mas events --json on an empty board emits nothing (no lines)."""
        monkeypatch.chdir(tmp_board.parent)
        result = runner.invoke(app, ["events", "--json"])
        assert result.exit_code == 0, (
            f"Expected exit 0 on empty board, got {result.exit_code}.\n{result.output}"
        )
        assert result.output.strip() == "", (
            f"Expected empty output for --json on empty board; got:\n{result.output}"
        )


# ── (b) Multi-task aggregation ────────────────────────────────────────────────


class TestEventsAggregation:
    def _seed(self, tmp_board):
        da = _make_task_dir(tmp_board, "done",  _T1)
        db = _make_task_dir(tmp_board, "doing", _T2)
        _write_audit(da, _EVENTS_T1)
        _write_audit(db, _EVENTS_T2)

    def test_shows_events_from_multiple_tasks(self, tmp_board, monkeypatch):
        """Events from multiple tasks all appear in the combined output."""
        monkeypatch.chdir(tmp_board.parent)
        self._seed(tmp_board)
        result = runner.invoke(app, ["events"])
        assert result.exit_code == 0, f"Expected exit 0: {result.output}"
        assert _T1 in result.output
        assert _T2 in result.output

    def test_sorted_by_timestamp_ascending(self, tmp_board, monkeypatch):
        """Aggregated events are sorted by timestamp ascending across tasks."""
        monkeypatch.chdir(tmp_board.parent)
        self._seed(tmp_board)
        result = runner.invoke(app, ["events", "--json"])
        assert result.exit_code == 0, f"Expected exit 0: {result.output}"
        lines = [l for l in result.output.strip().splitlines() if l.strip()]
        assert len(lines) == 4, f"Expected 4 events, got {len(lines)}: {lines}"
        timestamps = [json.loads(l)["timestamp"] for l in lines]
        assert timestamps == sorted(timestamps), (
            f"Events not sorted ascending: {timestamps}"
        )

    def test_table_includes_task_id_column(self, tmp_board, monkeypatch):
        """Table output includes a task_id column header."""
        monkeypatch.chdir(tmp_board.parent)
        self._seed(tmp_board)
        result = runner.invoke(app, ["events"])
        assert result.exit_code == 0, f"Expected exit 0: {result.output}"
        assert "task_id" in result.output.lower()

    def test_task_without_audit_jsonl_silently_ignored(self, tmp_board, monkeypatch):
        """Task directories without audit.jsonl are silently skipped."""
        monkeypatch.chdir(tmp_board.parent)
        _make_task_dir(tmp_board, "done", _T1)  # no audit.jsonl
        db = _make_task_dir(tmp_board, "done", _T2)
        _write_audit(db, _EVENTS_T2)
        result = runner.invoke(app, ["events"])
        assert result.exit_code == 0, (
            f"Expected exit 0 when some tasks lack audit.jsonl: {result.output}"
        )
        assert _T2 in result.output


# ── (c) Filters ───────────────────────────────────────────────────────────────


class TestEventsFilters:
    def _seed(self, tmp_board):
        d = _make_task_dir(tmp_board, "done", _TF)
        _write_audit(d, _FILTER_EVENTS)

    def test_filter_by_task(self, tmp_board, monkeypatch):
        """--task <id> shows only events for that task, hiding others."""
        monkeypatch.chdir(tmp_board.parent)
        other = "20260423-other-zzzz"
        od = _make_task_dir(tmp_board, "done", other)
        _write_audit(od, [_evt(other, "2026-04-23T00:10:00+00:00", summary="other task event")])
        self._seed(tmp_board)

        result = runner.invoke(app, ["events", "--task", _TF])
        assert result.exit_code == 0, f"Expected exit 0: {result.output}"
        assert _TF in result.output
        assert other not in result.output

    def test_filter_by_role(self, tmp_board, monkeypatch):
        """--role implementer shows only implementer events, hiding tester."""
        monkeypatch.chdir(tmp_board.parent)
        self._seed(tmp_board)

        result = runner.invoke(app, ["events", "--role", "implementer"])
        assert result.exit_code == 0, f"Expected exit 0: {result.output}"
        assert "implementer" in result.output
        assert "tester dispatch" not in result.output

    def test_filter_by_status(self, tmp_board, monkeypatch):
        """--status success shows only events with status=success."""
        monkeypatch.chdir(tmp_board.parent)
        self._seed(tmp_board)

        result = runner.invoke(app, ["events", "--status", "success"])
        assert result.exit_code == 0, f"Expected exit 0: {result.output}"
        # dispatch events have status=None — must not appear
        assert "impl dispatch" not in result.output
        assert "tester dispatch" not in result.output

    def test_filter_by_event_type(self, tmp_board, monkeypatch):
        """--event dispatch shows only dispatch events."""
        monkeypatch.chdir(tmp_board.parent)
        self._seed(tmp_board)

        result = runner.invoke(app, ["events", "--event", "dispatch"])
        assert result.exit_code == 0, f"Expected exit 0: {result.output}"
        assert "dispatch" in result.output
        assert "impl complete" not in result.output   # completion summary
        assert "doing→done" not in result.output      # state_transition summary

    def test_filter_by_since(self, tmp_board, monkeypatch):
        """--since hides events before the given timestamp."""
        monkeypatch.chdir(tmp_board.parent)
        self._seed(tmp_board)

        result = runner.invoke(app, ["events", "--since", "2026-04-23T00:02:00Z"])
        assert result.exit_code == 0, f"Expected exit 0: {result.output}"
        assert "impl dispatch" not in result.output
        assert "impl complete" not in result.output
        # events at 00:02 and 00:03 must appear
        assert "tester dispatch" in result.output or "doing→done" in result.output

    def test_filter_by_until(self, tmp_board, monkeypatch):
        """--until hides events after the given timestamp."""
        monkeypatch.chdir(tmp_board.parent)
        self._seed(tmp_board)

        result = runner.invoke(app, ["events", "--until", "2026-04-23T00:01:00Z"])
        assert result.exit_code == 0, f"Expected exit 0: {result.output}"
        assert "impl dispatch" in result.output
        assert "impl complete" in result.output
        assert "tester dispatch" not in result.output

    def test_combined_filters_and_semantics(self, tmp_board, monkeypatch):
        """Multiple filters combine with AND semantics."""
        monkeypatch.chdir(tmp_board.parent)
        self._seed(tmp_board)

        # implementer + success → only "impl complete"
        result = runner.invoke(
            app, ["events", "--role", "implementer", "--status", "success"]
        )
        assert result.exit_code == 0, f"Expected exit 0: {result.output}"
        assert "impl complete" in result.output
        assert "impl dispatch" not in result.output    # implementer but status=None
        assert "tester dispatch" not in result.output


# ── (d) --json mode ───────────────────────────────────────────────────────────


class TestEventsJson:
    def _seed(self, tmp_board):
        d = _make_task_dir(tmp_board, "done", _TF)
        _write_audit(d, _FILTER_EVENTS[:2])

    def test_each_line_is_valid_json(self, tmp_board, monkeypatch):
        """--json emits newline-delimited JSON; each line round-trips via json.loads."""
        monkeypatch.chdir(tmp_board.parent)
        self._seed(tmp_board)

        result = runner.invoke(app, ["events", "--json"])
        assert result.exit_code == 0, f"Expected exit 0: {result.output}"
        lines = [l for l in result.output.strip().splitlines() if l.strip()]
        assert len(lines) == 2, f"Expected 2 JSON lines, got {len(lines)}: {lines}"
        for line in lines:
            obj = json.loads(line)
            assert isinstance(obj, dict), f"Expected dict, got {type(obj)}"

    def test_json_preserves_field_names(self, tmp_board, monkeypatch):
        """--json output preserves standard audit.jsonl field names."""
        monkeypatch.chdir(tmp_board.parent)
        self._seed(tmp_board)

        result = runner.invoke(app, ["events", "--json"])
        assert result.exit_code == 0, f"Expected exit 0: {result.output}"
        lines = [l for l in result.output.strip().splitlines() if l.strip()]
        assert lines, "Expected at least one JSON line"
        obj = json.loads(lines[0])
        for field in ("timestamp", "event", "role", "task_id", "status", "summary"):
            assert field in obj, f"Field {field!r} missing from --json output"

    def test_json_task_id_matches_task(self, tmp_board, monkeypatch):
        """--json output carries the correct task_id field."""
        monkeypatch.chdir(tmp_board.parent)
        self._seed(tmp_board)

        result = runner.invoke(app, ["events", "--json"])
        assert result.exit_code == 0, f"Expected exit 0: {result.output}"
        lines = [l for l in result.output.strip().splitlines() if l.strip()]
        obj = json.loads(lines[0])
        assert obj["task_id"] == _TF


# ── (e) --follow mode ─────────────────────────────────────────────────────────


class TestEventsFollow:
    def test_follow_picks_up_new_events_and_exits_on_sigint(
        self, tmp_board, monkeypatch
    ):
        """--follow streams newly-appended events and exits 0 on KeyboardInterrupt."""
        monkeypatch.chdir(tmp_board.parent)
        d = _make_task_dir(tmp_board, "doing", _TF)
        _write_audit(d, _FILTER_EVENTS[:1])

        new_event = _evt(_TF, "2026-04-23T01:00:00+00:00", summary="newly appended event")
        calls: list[float] = []

        def fake_sleep(seconds: float) -> None:
            calls.append(seconds)
            if len(calls) == 1:
                with (d / "audit.jsonl").open("a") as f:
                    f.write(json.dumps(new_event) + "\n")
            elif len(calls) >= 2:
                raise KeyboardInterrupt

        monkeypatch.setattr("time.sleep", fake_sleep)

        result = runner.invoke(app, ["events", "--follow", "--interval", "0.01"])
        assert result.exit_code == 0, (
            f"--follow must exit 0 on KeyboardInterrupt, got {result.exit_code}.\n"
            f"Output:\n{result.output}"
        )
        assert "newly appended event" in result.output, (
            f"Expected new event in --follow output:\n{result.output}"
        )
        assert "Traceback" not in result.output
        assert "KeyboardInterrupt" not in result.output

    def test_follow_respects_interval_option(self, tmp_board, monkeypatch):
        """--follow sleeps for --interval seconds between polls."""
        monkeypatch.chdir(tmp_board.parent)
        d = _make_task_dir(tmp_board, "doing", _TF)
        _write_audit(d, _FILTER_EVENTS[:1])

        sleep_calls: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            raise KeyboardInterrupt

        monkeypatch.setattr("time.sleep", fake_sleep)

        result = runner.invoke(app, ["events", "--follow", "--interval", "0.5"])
        assert result.exit_code == 0, (
            f"Expected exit 0 on KeyboardInterrupt, got {result.exit_code}:\n{result.output}"
        )
        assert any(abs(s - 0.5) < 0.1 for s in sleep_calls), (
            f"Expected sleep called with ~0.5s, got: {sleep_calls}"
        )


# ── (f) Malformed lines → UserWarning ────────────────────────────────────────


class TestEventsMalformed:
    def test_malformed_line_surfaces_user_warning(self, tmp_board, monkeypatch):
        """Malformed audit lines surface a UserWarning (proves mas.audit.read_events is reused)."""
        monkeypatch.chdir(tmp_board.parent)
        d = _make_task_dir(tmp_board, "done", _TF)
        (d / "audit.jsonl").write_text(
            '{"timestamp":"2026-04-23T00:00:00+00:00","event":"dispatch","role":"implementer",'
            '"provider":"claude_code","task_id":"' + _TF + '","subtask_id":null,'
            '"status":null,"duration_s":null,"summary":"valid event","details":{}}\n'
            "NOT_VALID_JSON\n"
            '{"timestamp":"2026-04-23T00:01:00+00:00","event":"completion","role":"implementer",'
            '"provider":"claude_code","task_id":"' + _TF + '","subtask_id":null,'
            '"status":"success","duration_s":1.0,"summary":"also valid","details":{}}\n'
        )

        with pytest.warns(UserWarning):
            result = runner.invoke(app, ["events"])

        assert result.exit_code == 0, (
            f"Expected exit 0 even with malformed audit lines: {result.output}"
        )
        assert "valid event" in result.output or "also valid" in result.output


# ── (g) Evaluator feedback: -f short flag and --interval default ──────────────


class TestEvaluatorFeedback:
    def test_follow_short_flag_accepted(self, tmp_board, monkeypatch):
        """-f must be accepted as a short form of --follow and exit 0 on KeyboardInterrupt."""
        monkeypatch.chdir(tmp_board.parent)
        d = _make_task_dir(tmp_board, "doing", _TF)
        _write_audit(d, _FILTER_EVENTS[:1])

        def fake_sleep(seconds: float) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr("time.sleep", fake_sleep)

        result = runner.invoke(app, ["events", "-f", "--interval", "0.01"])
        assert result.exit_code != 2, (
            "exit_code=2 means -f is not registered as a short form of --follow"
        )
        assert result.exit_code == 0, (
            f"-f must exit 0 on KeyboardInterrupt, got {result.exit_code}.\n{result.output}"
        )

    def test_interval_default_is_2_seconds(self, tmp_board, monkeypatch):
        """--interval default must be 2.0 seconds (matches documentation)."""
        monkeypatch.chdir(tmp_board.parent)
        d = _make_task_dir(tmp_board, "doing", _TF)
        _write_audit(d, _FILTER_EVENTS[:1])

        sleep_calls: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            raise KeyboardInterrupt

        monkeypatch.setattr("time.sleep", fake_sleep)

        result = runner.invoke(app, ["events", "--follow"])
        assert result.exit_code == 0, (
            f"Expected exit 0 on KeyboardInterrupt, got {result.exit_code}:\n{result.output}"
        )
        assert sleep_calls, "Expected time.sleep to be called at least once in --follow mode"
        assert any(abs(s - 2.0) < 0.1 for s in sleep_calls), (
            f"Expected --interval default of 2.0s but sleep was called with: {sleep_calls}"
        )
