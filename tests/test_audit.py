"""Tests for audit logging — src/mas/audit.py and tick-loop integration.

All tests are designed to FAIL against the current codebase (stubs raise
NotImplementedError) and PASS once the real implementation is in place.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest

from mas import audit, board
from mas.adapters.base import Adapter, DispatchHandle
from mas.schemas import (
    MasConfig,
    Plan,
    ProviderConfig,
    Result,
    RoleConfig,
    SubtaskSpec,
    Task,
)
from mas.tick import TickEnv, _advance_one, _handle_child_result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(max_retries: int = 2) -> MasConfig:
    return MasConfig(
        providers={"mock": ProviderConfig(cli="sh", max_concurrent=2)},
        roles={
            "proposer": RoleConfig(provider="mock", max_retries=max_retries),
            "orchestrator": RoleConfig(provider="mock", max_retries=max_retries),
            "implementer": RoleConfig(provider="mock", max_retries=max_retries),
            "tester": RoleConfig(provider="mock", max_retries=max_retries),
            "evaluator": RoleConfig(provider="mock", max_retries=max_retries),
        },
    )


def _fake_dispatch(self, prompt, task_dir, cwd, log_path, role, stdin_text=None):
    """Drop-in for Adapter.dispatch that avoids spawning a real subprocess."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch()
    return DispatchHandle(
        pid=99999, provider="mock", role=role, task_dir=task_dir, log_path=log_path
    )


# ---------------------------------------------------------------------------
# 1.  append_event — basic contract
# ---------------------------------------------------------------------------


class TestAppendEvent:
    def test_creates_audit_jsonl(self, tmp_path):
        """append_event creates audit.jsonl in the given task_dir."""
        audit.append_event(
            tmp_path,
            event="dispatch",
            task_id="20260423-test-aaaa",
            role="implementer",
            provider="claude_code",
            summary="dispatched implementer",
        )
        assert (tmp_path / "audit.jsonl").exists()

    def test_event_has_all_required_fields(self, tmp_path):
        """Each event record contains all ten required fields."""
        audit.append_event(
            tmp_path,
            event="dispatch",
            task_id="20260423-test-aaaa",
            role="implementer",
            provider="claude_code",
            subtask_id="sub-1",
            status="running",
            duration_s=None,
            summary="dispatched",
            details={"attempt": 1},
        )
        line = (tmp_path / "audit.jsonl").read_text().strip()
        entry = json.loads(line)
        required = {
            "timestamp",
            "event",
            "role",
            "provider",
            "task_id",
            "subtask_id",
            "status",
            "duration_s",
            "summary",
            "details",
        }
        missing = required - set(entry.keys())
        assert not missing, f"Missing fields in audit entry: {missing}"

    def test_timestamp_is_utc_iso(self, tmp_path):
        """Timestamp field is a valid UTC ISO 8601 string."""
        from datetime import datetime

        audit.append_event(
            tmp_path,
            event="dispatch",
            task_id="20260423-test-aaaa",
            role="implementer",
            provider="claude_code",
            summary="dispatched",
        )
        line = (tmp_path / "audit.jsonl").read_text().strip()
        entry = json.loads(line)
        ts = entry["timestamp"]
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        assert dt.tzinfo is not None, "timestamp must carry timezone info"

    def test_multiple_events_appended_as_separate_lines(self, tmp_path):
        """Repeated calls append one JSONL line each."""
        for event_type in ("dispatch", "completion", "state_transition"):
            audit.append_event(
                tmp_path,
                event=event_type,
                task_id="20260423-multi-aaaa",
                role="implementer",
                provider="claude_code",
                summary=f"{event_type} event",
            )
        lines = [l for l in (tmp_path / "audit.jsonl").read_text().splitlines() if l.strip()]
        assert len(lines) == 3

    def test_valid_event_types_accepted(self, tmp_path):
        """Each recognized event type round-trips through append/read."""
        for event_type in ("dispatch", "completion", "state_transition", "error"):
            d = tmp_path / f"task_{event_type}"
            d.mkdir()
            audit.append_event(
                d,
                event=event_type,
                task_id="20260423-types-aaaa",
                role="implementer",
                provider="mock",
                summary=event_type,
            )
            line = (d / "audit.jsonl").read_text().strip()
            entry = json.loads(line)
            assert entry["event"] == event_type


# ---------------------------------------------------------------------------
# 2.  read_events — parsing and filters
# ---------------------------------------------------------------------------


_SAMPLE_JSONL = [
    {
        "timestamp": "2026-04-23T00:00:00+00:00",
        "event": "dispatch",
        "role": "implementer",
        "provider": "claude_code",
        "task_id": "20260423-test-aaaa",
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
        "task_id": "20260423-test-aaaa",
        "subtask_id": "sub-1",
        "status": "success",
        "duration_s": 60.0,
        "summary": "implementer completed",
        "details": {},
    },
    {
        "timestamp": "2026-04-23T00:02:00+00:00",
        "event": "dispatch",
        "role": "tester",
        "provider": "claude_code",
        "task_id": "20260423-test-aaaa",
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
        "task_id": "20260423-test-aaaa",
        "subtask_id": None,
        "status": "success",
        "duration_s": None,
        "summary": "doing → done",
        "details": {"reason": "role_success"},
    },
]


def _write_sample(tmp_path: Path) -> None:
    (tmp_path / "audit.jsonl").write_text(
        "\n".join(json.dumps(e) for e in _SAMPLE_JSONL) + "\n"
    )


class TestReadEvents:
    def test_read_returns_all_events(self, tmp_path):
        _write_sample(tmp_path)
        events = audit.read_events(tmp_path)
        assert len(events) == 4

    def test_filter_by_role(self, tmp_path):
        """role filter returns only events for that role."""
        _write_sample(tmp_path)
        events = audit.read_events(tmp_path, role="implementer")
        assert len(events) == 2
        assert all(e["role"] == "implementer" for e in events)

    def test_filter_by_status(self, tmp_path):
        """status filter returns only matching events."""
        _write_sample(tmp_path)
        events = audit.read_events(tmp_path, status="success")
        assert all(e["status"] == "success" for e in events)
        assert len(events) >= 1

    def test_filter_by_since(self, tmp_path):
        """since filter returns events at or after the given timestamp."""
        _write_sample(tmp_path)
        events = audit.read_events(tmp_path, since="2026-04-23T00:01:00Z")
        # Events at 00:01, 00:02, 00:03 should appear; 00:00 should not
        assert len(events) == 3
        for e in events:
            assert e["timestamp"] >= "2026-04-23T00:01:00"

    def test_filter_by_until(self, tmp_path):
        """until filter returns events at or before the given timestamp."""
        _write_sample(tmp_path)
        events = audit.read_events(tmp_path, until="2026-04-23T00:01:00Z")
        # Events at 00:00 and 00:01 should appear
        assert len(events) == 2

    def test_corrupt_line_skipped_with_warning(self, tmp_path):
        """A malformed JSONL line is skipped and a UserWarning is emitted."""
        (tmp_path / "audit.jsonl").write_text(
            '{"timestamp":"2026-04-23T00:00:00+00:00","event":"dispatch","role":"implementer",'
            '"provider":"claude_code","task_id":"x","subtask_id":null,"status":null,'
            '"duration_s":null,"summary":"ok","details":{}}\n'
            "NOT_VALID_JSON\n"
            '{"timestamp":"2026-04-23T00:01:00+00:00","event":"completion","role":"implementer",'
            '"provider":"claude_code","task_id":"x","subtask_id":null,"status":"success",'
            '"duration_s":1.0,"summary":"done","details":{}}\n'
        )
        with pytest.warns(UserWarning):
            events = audit.read_events(tmp_path)
        assert len(events) == 2, "corrupt line skipped, two valid lines returned"

    def test_corrupt_line_does_not_raise(self, tmp_path):
        """read_events never raises even when a corrupt line is present."""
        (tmp_path / "audit.jsonl").write_text(
            "CORRUPT\n"
            '{"timestamp":"2026-04-23T00:01:00+00:00","event":"completion","role":"implementer",'
            '"provider":"claude_code","task_id":"x","subtask_id":null,"status":"success",'
            '"duration_s":1.0,"summary":"done","details":{}}\n'
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            events = audit.read_events(tmp_path)
        assert len(events) == 1


# ---------------------------------------------------------------------------
# 3.  Tick integration: dispatch → parent audit.jsonl
# ---------------------------------------------------------------------------


class TestTickDispatchAudit:
    def _make_parent_with_plan(self, mas, parent_id, child_id, child_role="implementer"):
        parent = board.task_dir(mas, "doing", parent_id)
        parent.mkdir(parents=True)
        board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="g"))
        (parent / "worktree").mkdir()
        plan = Plan(
            parent_id=parent_id,
            summary="s",
            subtasks=[SubtaskSpec(id=child_id, role=child_role, goal="do it")],
        )
        (parent / "plan.json").write_text(plan.model_dump_json())
        (parent / "subtasks" / child_id).mkdir(parents=True)
        return parent

    def test_advance_one_dispatch_creates_parent_audit(self, tmp_path):
        """After _advance_one dispatches a subtask, parent's audit.jsonl exists."""
        mas = tmp_path / ".mas"
        board.ensure_layout(mas)

        parent_id = "20260423-parent-aaaa"
        parent = self._make_parent_with_plan(mas, parent_id, "child-impl-1")
        env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

        with patch.object(Adapter, "dispatch", _fake_dispatch), \
             patch("mas.board.count_active_pids", return_value=0), \
             patch("mas.tick._pid_alive", return_value=False):
            _advance_one(env, parent)

        audit_file = parent / "audit.jsonl"
        assert audit_file.exists(), "parent audit.jsonl not created after subtask dispatch"
        entries = [json.loads(l) for l in audit_file.read_text().strip().splitlines() if l]
        dispatch_entries = [e for e in entries if e.get("event") == "dispatch"]
        assert len(dispatch_entries) >= 1, "no 'dispatch' entry in parent audit.jsonl"

    def test_subtask_audit_entries_include_subtask_id(self, tmp_path):
        """Audit entries for subtask dispatch carry the subtask_id field."""
        mas = tmp_path / ".mas"
        board.ensure_layout(mas)

        parent_id = "20260423-parent-bbbb"
        child_id = "child-tester-1"
        parent = self._make_parent_with_plan(mas, parent_id, child_id, child_role="tester")
        env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

        with patch.object(Adapter, "dispatch", _fake_dispatch), \
             patch("mas.board.count_active_pids", return_value=0), \
             patch("mas.tick._pid_alive", return_value=False):
            _advance_one(env, parent)

        parent_audit = parent / "audit.jsonl"
        assert parent_audit.exists(), "subtask dispatch not logged to parent's audit.jsonl"
        entries = [json.loads(l) for l in parent_audit.read_text().strip().splitlines() if l]
        subtask_entries = [
            e for e in entries
            if e.get("subtask_id") is not None and e.get("event") == "dispatch"
        ]
        assert len(subtask_entries) >= 1, \
            "no subtask-level dispatch entries in parent's audit.jsonl"

    def test_subtask_audit_not_in_child_dir(self, tmp_path):
        """Subtask audit is appended to PARENT, not written to child's dir."""
        mas = tmp_path / ".mas"
        board.ensure_layout(mas)

        parent_id = "20260423-parent-cccc"
        child_id = "child-impl-2"
        parent = self._make_parent_with_plan(mas, parent_id, child_id)
        env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

        with patch.object(Adapter, "dispatch", _fake_dispatch), \
             patch("mas.board.count_active_pids", return_value=0), \
             patch("mas.tick._pid_alive", return_value=False):
            _advance_one(env, parent)

        parent_audit = parent / "audit.jsonl"
        child_audit = parent / "subtasks" / child_id / "audit.jsonl"
        # Parent MUST have the audit entry
        assert parent_audit.exists(), \
            "dispatch event must be logged in parent's audit.jsonl"
        # Child dir must NOT have a separate audit file
        assert not child_audit.exists(), \
            "subtask audit should NOT be in child dir — belongs in parent's audit.jsonl"


# ---------------------------------------------------------------------------
# 4.  Tick integration: completion result → parent audit.jsonl
# ---------------------------------------------------------------------------


class TestTickCompletionAudit:
    def test_handle_child_result_success_creates_completion_entry(self, tmp_path):
        """Processing a successful subtask result appends a completion entry to parent audit."""
        mas = tmp_path / ".mas"
        board.ensure_layout(mas)

        parent_id = "20260423-parent-dddd"
        parent = board.task_dir(mas, "doing", parent_id)
        parent.mkdir(parents=True)
        parent_task = Task(id=parent_id, role="orchestrator", goal="g")
        board.write_task(parent, parent_task)
        (parent / "worktree").mkdir()

        child_id = "child-impl-1"
        plan = Plan(
            parent_id=parent_id,
            summary="s",
            subtasks=[SubtaskSpec(id=child_id, role="implementer", goal="impl")],
        )
        (parent / "plan.json").write_text(plan.model_dump_json())
        child_dir = parent / "subtasks" / child_id
        child_dir.mkdir(parents=True)

        result = Result(
            task_id=child_id,
            status="success",
            summary="implementation done",
            duration_s=42.0,
        )
        (child_dir / "result.json").write_text(result.model_dump_json())

        env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())
        spec = plan.subtasks[0]
        _handle_child_result(env, parent, parent_task, plan, spec, result)

        audit_file = parent / "audit.jsonl"
        assert audit_file.exists(), "parent audit.jsonl not created on subtask completion"
        entries = [json.loads(l) for l in audit_file.read_text().strip().splitlines() if l]
        completion_entries = [e for e in entries if e.get("event") == "completion"]
        assert len(completion_entries) >= 1, "no 'completion' entry in parent audit.jsonl"

    def test_completion_entry_includes_duration_and_status(self, tmp_path):
        """Completion audit entry carries duration_s and status from the result."""
        mas = tmp_path / ".mas"
        board.ensure_layout(mas)

        parent_id = "20260423-parent-eeee"
        parent = board.task_dir(mas, "doing", parent_id)
        parent.mkdir(parents=True)
        parent_task = Task(id=parent_id, role="orchestrator", goal="g")
        board.write_task(parent, parent_task)
        (parent / "worktree").mkdir()

        child_id = "child-impl-3"
        plan = Plan(
            parent_id=parent_id,
            summary="s",
            subtasks=[SubtaskSpec(id=child_id, role="implementer", goal="impl")],
        )
        (parent / "plan.json").write_text(plan.model_dump_json())
        child_dir = parent / "subtasks" / child_id
        child_dir.mkdir(parents=True)

        result = Result(
            task_id=child_id,
            status="success",
            summary="impl complete",
            duration_s=99.9,
        )
        (child_dir / "result.json").write_text(result.model_dump_json())

        env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())
        spec = plan.subtasks[0]
        _handle_child_result(env, parent, parent_task, plan, spec, result)

        audit_file = parent / "audit.jsonl"
        assert audit_file.exists()
        entries = [json.loads(l) for l in audit_file.read_text().strip().splitlines() if l]
        completion_entries = [e for e in entries if e.get("event") == "completion"]
        assert completion_entries, "no completion entry found"
        entry = completion_entries[0]
        assert entry.get("status") == "success", "completion entry missing status"
        assert entry.get("duration_s") is not None, "completion entry missing duration_s"


# ---------------------------------------------------------------------------
# 5.  Tick integration: state transition → audit.jsonl
# ---------------------------------------------------------------------------


class TestStateTransitionAudit:
    def test_board_move_writes_state_transition_to_audit(self, tmp_path):
        """board.move appends a state_transition entry to the task's audit.jsonl."""
        mas = tmp_path / ".mas"
        board.ensure_layout(mas)

        task_id = "20260423-trans-aaaa"
        src = mas / "tasks" / "doing" / task_id
        dst = mas / "tasks" / "done" / task_id
        src.mkdir(parents=True)
        board.write_task(src, Task(id=task_id, role="implementer", goal="g"))

        board.move(src, dst, reason="role_success")

        audit_file = dst / "audit.jsonl"
        assert audit_file.exists(), "audit.jsonl not created on board.move"
        entries = [json.loads(l) for l in audit_file.read_text().strip().splitlines() if l]
        trans_entries = [e for e in entries if e.get("event") == "state_transition"]
        assert len(trans_entries) >= 1, "no state_transition entry after board.move"

    def test_state_transition_entry_contains_reason(self, tmp_path):
        """state_transition audit entry includes the reason from board.move."""
        mas = tmp_path / ".mas"
        board.ensure_layout(mas)

        task_id = "20260423-trans-bbbb"
        src = mas / "tasks" / "doing" / task_id
        dst = mas / "tasks" / "failed" / task_id
        src.mkdir(parents=True)
        board.write_task(src, Task(id=task_id, role="implementer", goal="g"))

        board.move(src, dst, reason="max_retries_exceeded")

        audit_file = dst / "audit.jsonl"
        assert audit_file.exists()
        content = audit_file.read_text()
        entries = [json.loads(l) for l in content.strip().splitlines() if l]
        trans_entries = [e for e in entries if e.get("event") == "state_transition"]
        assert trans_entries, "no state_transition entry"
        entry = trans_entries[0]
        details = entry.get("details") or {}
        summary = entry.get("summary") or ""
        assert "max_retries_exceeded" in summary or details.get("reason") == "max_retries_exceeded", \
            "state_transition entry should record the reason"


# ---------------------------------------------------------------------------
# 6.  Completeness across happy path, revision cycle, failure
# ---------------------------------------------------------------------------


class TestAuditCompleteness:
    def test_happy_path_all_event_types_present(self, tmp_path):
        """Happy path audit trail contains dispatch, completion, and state_transition."""
        d = tmp_path / "task"
        d.mkdir()
        audit.append_event(d, event="dispatch", task_id="20260423-hp-aaaa",
                           role="implementer", provider="mock", summary="dispatch")
        audit.append_event(d, event="completion", task_id="20260423-hp-aaaa",
                           role="implementer", provider="mock", status="success",
                           duration_s=10.0, summary="done")
        audit.append_event(d, event="state_transition", task_id="20260423-hp-aaaa",
                           summary="doing→done", details={"reason": "role_success"})

        events = audit.read_events(d)
        event_types = {e["event"] for e in events}
        assert "dispatch" in event_types
        assert "completion" in event_types
        assert "state_transition" in event_types

    def test_revision_cycle_events_are_logged(self, tmp_path):
        """Audit log captures events for each revision cycle."""
        d = tmp_path / "task"
        d.mkdir()

        # Cycle 0
        audit.append_event(d, event="dispatch", task_id="20260423-rev-aaaa",
                           role="implementer", provider="mock", summary="cycle 0 impl",
                           details={"cycle": 0})
        audit.append_event(d, event="completion", task_id="20260423-rev-aaaa",
                           role="evaluator", provider="mock", status="needs_revision",
                           summary="needs revision", details={"verdict": "needs_revision", "cycle": 0})

        # Cycle 1 (revision)
        audit.append_event(d, event="dispatch", task_id="20260423-rev-aaaa",
                           role="implementer", provider="mock", summary="cycle 1 impl",
                           details={"cycle": 1})
        audit.append_event(d, event="completion", task_id="20260423-rev-aaaa",
                           role="evaluator", provider="mock", status="success",
                           summary="pass", details={"verdict": "pass", "cycle": 1})

        events = audit.read_events(d)
        assert len(events) == 4

        revision_events = [e for e in events if e.get("details", {}).get("cycle") == 1]
        assert len(revision_events) >= 1, "No events tagged with cycle=1"

        needs_revision = [
            e for e in events
            if e.get("details", {}).get("verdict") == "needs_revision"
        ]
        assert len(needs_revision) >= 1, "No needs_revision evaluator event logged"

    def test_failure_path_events_logged(self, tmp_path):
        """Failure path: retries produce failure completion events; final state_transition reason is max_retries_exceeded."""
        d = tmp_path / "task"
        d.mkdir()

        for attempt in range(1, 4):
            audit.append_event(d, event="dispatch", task_id="20260423-fail-aaaa",
                               role="implementer", provider="mock",
                               summary=f"attempt {attempt}", details={"attempt": attempt})
            audit.append_event(d, event="completion", task_id="20260423-fail-aaaa",
                               role="implementer", provider="mock", status="failure",
                               summary=f"failed attempt {attempt}", details={"attempt": attempt})

        audit.append_event(d, event="state_transition", task_id="20260423-fail-aaaa",
                           summary="doing→failed",
                           details={"reason": "max_retries_exceeded"})

        events = audit.read_events(d)
        failure_completions = [
            e for e in events
            if e.get("event") == "completion" and e.get("status") == "failure"
        ]
        assert len(failure_completions) == 3, "Expected 3 failure completion entries"

        state_events = [e for e in events if e.get("event") == "state_transition"]
        assert any(
            e.get("details", {}).get("reason") == "max_retries_exceeded"
            for e in state_events
        ), "Expected max_retries_exceeded state_transition entry"

    def test_completion_entry_includes_result_summary(self, tmp_path):
        """Completion audit entries carry the result summary text."""
        d = tmp_path / "task"
        d.mkdir()

        audit.append_event(d, event="completion", task_id="20260423-sum-aaaa",
                           role="implementer", provider="mock", status="success",
                           summary="implementation complete: added 3 files",
                           details={"artifacts": ["src/foo.py"]})

        events = audit.read_events(d)
        assert len(events) == 1
        assert "implementation complete" in events[0]["summary"]

    def test_completion_entry_includes_duration(self, tmp_path):
        """Completion audit entries carry duration_s."""
        d = tmp_path / "task"
        d.mkdir()

        audit.append_event(d, event="completion", task_id="20260423-dur-aaaa",
                           role="implementer", provider="mock", status="success",
                           duration_s=42.5, summary="done")

        events = audit.read_events(d)
        assert len(events) == 1
        assert events[0]["duration_s"] == 42.5
