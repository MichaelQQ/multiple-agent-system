from __future__ import annotations

import threading
from pathlib import Path

import pytest

from mas import transitions
from mas.transitions import ensure_initial_log, log_transition, read_transitions


# --- log_transition -----------------------------------------------------------


def test_log_transition_creates_file(tmp_path):
    log_transition(tmp_path, "none", "proposed", "created")
    assert (tmp_path / ".transitions.log").exists()


def test_log_transition_appends(tmp_path):
    log_transition(tmp_path, "none", "proposed", "created")
    log_transition(tmp_path, "proposed", "doing", "promoted")
    lines = (tmp_path / ".transitions.log").read_text().splitlines()
    assert len(lines) == 2
    assert "proposed→doing" not in lines[0]  # order check via content
    assert "promoted" in lines[1]


def test_log_format(tmp_path):
    log_transition(tmp_path, "proposed", "doing", "promoted")
    line = (tmp_path / ".transitions.log").read_text().strip()
    parts = line.split("|")
    assert len(parts) == 4
    ts, from_s, to_s, reason = parts
    assert "T" in ts  # ISO8601
    assert from_s == "proposed"
    assert to_s == "doing"
    assert reason == "promoted"


# --- read_transitions ---------------------------------------------------------


def test_read_transitions(tmp_path):
    log_transition(tmp_path, "none", "proposed", "created")
    log_transition(tmp_path, "proposed", "doing", "promoted")
    result = read_transitions(tmp_path)
    assert len(result) == 2
    assert result[0].from_state == "none"
    assert result[1].to_state == "doing"
    assert result[1].reason == "promoted"


def test_read_transitions_limit(tmp_path):
    for i in range(5):
        log_transition(tmp_path, str(i), str(i + 1), f"step{i}")
    result = read_transitions(tmp_path, limit=3)
    assert len(result) == 3
    assert result[0].reason == "step2"


def test_read_transitions_empty(tmp_path):
    assert read_transitions(tmp_path) == []


# --- ensure_initial_log -------------------------------------------------------


def test_ensure_initial_log(tmp_path):
    ensure_initial_log(tmp_path, "proposed")
    result = read_transitions(tmp_path)
    assert len(result) == 1
    assert result[0].from_state == "none"
    assert result[0].to_state == "proposed"
    assert result[0].reason == "created"


def test_ensure_initial_log_idempotent(tmp_path):
    ensure_initial_log(tmp_path, "proposed")
    ensure_initial_log(tmp_path, "proposed")
    assert len(read_transitions(tmp_path)) == 1


# --- board integration --------------------------------------------------------


def test_board_move_logs_transition(tmp_path):
    from mas import board
    from mas.schemas import Task

    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    src = mas / "tasks" / "proposed" / "20260415-task-abc-aaaa"
    task = Task(id="20260415-task-abc-aaaa", role="orchestrator", goal="test goal")
    board.write_task(src, task)

    dst = mas / "tasks" / "doing" / "20260415-task-abc-aaaa"
    board.move(src, dst)

    result = read_transitions(dst)
    assert any(r.from_state == "proposed" and r.to_state == "doing" for r in result)


def test_board_move_with_reason(tmp_path):
    from mas import board
    from mas.schemas import Task

    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    src = mas / "tasks" / "proposed" / "20260415-task-xyz-aaaa"
    task = Task(id="20260415-task-xyz-aaaa", role="orchestrator", goal="test")
    board.write_task(src, task)

    dst = mas / "tasks" / "doing" / "20260415-task-xyz-aaaa"
    board.move(src, dst, reason="manual_promote")

    result = read_transitions(dst)
    assert result[-1].reason == "manual_promote"


# --- concurrent writes --------------------------------------------------------


def test_concurrent_writes(tmp_path):
    errors = []

    def writer():
        try:
            for _ in range(10):
                log_transition(tmp_path, "a", "b", "concurrent")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    lines = (tmp_path / ".transitions.log").read_text().splitlines()
    assert len(lines) == 100
    for line in lines:
        parts = line.split("|")
        assert len(parts) == 4, f"malformed line: {line!r}"


def test_existing_task_without_log(tmp_path):
    """Calling log_transition on a task dir that has no .transitions.log works."""
    assert not (tmp_path / ".transitions.log").exists()
    log_transition(tmp_path, "doing", "failed", "max_retries_exceeded")
    result = read_transitions(tmp_path)
    assert len(result) == 1
    assert result[0].reason == "max_retries_exceeded"


# --- transition summary in result feedback -----------------------------------


def test_transition_summary_appended_to_feedback(tmp_path):
    """Transition history is appended to result.feedback when present."""
    log_transition(tmp_path, "none", "doing", "created")
    log_transition(tmp_path, "doing", "doing", "retry")
    log_transition(tmp_path, "doing", "done", "role_success")

    txns = read_transitions(tmp_path, limit=3)
    assert txns

    existing_feedback = "Test output from role"
    txn_str = " | ".join(f"{x.from_state}→{x.to_state}({x.reason})" for x in txns)
    expected = existing_feedback + f"\n[transition history: {txn_str}]"

    combined = (existing_feedback or "") + (
        f"\n[transition history: {txn_str}]" if existing_feedback else f"[transition history: {txn_str}]"
    )
    assert combined == expected


def test_transition_summary_without_existing_feedback(tmp_path):
    """Transition history is set when result has no existing feedback."""
    log_transition(tmp_path, "none", "proposed", "created")
    log_transition(tmp_path, "proposed", "doing", "manual_promote")

    txns = read_transitions(tmp_path, limit=3)
    txn_str = " | ".join(f"{x.from_state}→{x.to_state}({x.reason})" for x in txns)

    feedback = ""
    combined = (feedback or "") + f"[transition history: {txn_str}]"
    assert "[transition history:" in combined
    assert "none→proposed(created)" in combined
    assert "proposed→doing(manual_promote)" in combined


def test_transition_summary_empty_when_no_transitions(tmp_path):
    """No transition history added when task has no transitions.log."""
    txns = read_transitions(tmp_path)
    assert txns == []

    txn_str = " | ".join(f"{x.from_state}→{x.to_state}({x.reason})" for x in txns)
    assert txn_str == ""


def test_retry_logs_manual_retry_reason(tmp_path):
    """Retry command logs 'manual_retry' as the transition reason."""
    from mas import board
    from mas.schemas import Task

    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    src = mas / "tasks" / "failed" / "20260415-task-retry-aaaa"
    task = Task(id="20260415-task-retry-aaaa", role="orchestrator", goal="test")
    board.write_task(src, task)

    dst = mas / "tasks" / "doing" / "20260415-task-retry-aaaa"
    board.move(src, dst, reason="manual_retry")

    result = read_transitions(dst)
    assert result[-1].reason == "manual_retry"
