"""Tests for mas.patterns — failure-pattern index aggregation and consumption."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mas import board, patterns
from mas import state as _state
from mas import transitions as _transitions
from mas.schemas import Task


def _make_failed_task(
    mas: Path,
    *,
    task_id: str,
    goal: str,
    terminal_reason: str,
    rejected_summaries: list[str] | None = None,
    role: str = "orchestrator",
) -> Path:
    """Create a fixture failed/ task with transitions and (optional) state."""
    failed = mas / "tasks" / "failed" / task_id
    failed.parent.mkdir(parents=True, exist_ok=True)
    board.write_task(failed, Task(id=task_id, role=role, goal=goal))
    _transitions.log_transition(failed, "proposed", "doing", "manual_promote")
    _transitions.log_transition(failed, "doing", "failed", terminal_reason)

    if rejected_summaries:
        ps = _state.ParentState(
            rejected_attempts=[
                _state.RejectedAttempt(
                    subtask_id=f"{task_id}-impl-{i}",
                    role="implementer",
                    status="failure",
                    summary=summary,
                    attempt=i + 1,
                )
                for i, summary in enumerate(rejected_summaries)
            ]
        )
        _state.write_state(failed, ps)
    return failed


@pytest.fixture
def mas(tmp_path):
    d = tmp_path / ".mas"
    for col in ("proposed", "doing", "done", "failed"):
        (d / "tasks" / col).mkdir(parents=True)
    return d


def test_compute_patterns_empty_when_no_failures(mas):
    assert patterns.compute_patterns(mas) == []


def test_compute_patterns_groups_by_normalized_goal_and_terminal_reason(mas):
    _make_failed_task(
        mas,
        task_id="20260505-add-mcp-tool-aaaa",
        goal="Add an MCP tool that returns latency metrics",
        terminal_reason="revision_cycles_exhausted",
    )
    # Same tokens up to stopwords/case — should collapse with the first
    _make_failed_task(
        mas,
        task_id="20260505-add-mcp-tool-bbbb",
        goal="add MCP tool that returns LATENCY metrics",
        terminal_reason="revision_cycles_exhausted",
    )
    # Same goal, different terminal reason — distinct pattern
    _make_failed_task(
        mas,
        task_id="20260505-add-mcp-tool-cccc",
        goal="Add an MCP tool that returns latency metrics",
        terminal_reason="max_retries_exceeded",
    )
    # Distinct goal entirely
    _make_failed_task(
        mas,
        task_id="20260505-rate-limit-dddd",
        goal="Add rate-limit middleware to the proxy",
        terminal_reason="revision_cycles_exhausted",
    )

    out = patterns.compute_patterns(mas)
    assert len(out) == 3

    # The two same-goal/same-reason failures must collapse with count=2
    revision_mcp = [
        p for p in out
        if p.terminal_reason == "revision_cycles_exhausted" and "mcp" in p.signature
    ]
    assert len(revision_mcp) == 1
    assert revision_mcp[0].count == 2
    assert set(revision_mcp[0].task_ids) == {
        "20260505-add-mcp-tool-aaaa",
        "20260505-add-mcp-tool-bbbb",
    }

    # The same goal under a different reason is its own pattern
    retry_mcp = [
        p for p in out
        if p.terminal_reason == "max_retries_exceeded" and "mcp" in p.signature
    ]
    assert len(retry_mcp) == 1
    assert retry_mcp[0].count == 1


def test_compute_patterns_skips_proposer_role(mas):
    """Proposer's own bootstrapping tasks must not pollute the index."""
    _make_failed_task(
        mas,
        task_id="20260505-proposer-eeee",
        goal="Propose a new task for the board",
        terminal_reason="max_retries_exceeded",
        role="proposer",
    )
    assert patterns.compute_patterns(mas) == []


def test_compute_patterns_includes_rejected_attempts_sample(mas):
    _make_failed_task(
        mas,
        task_id="20260505-reject-sample-ffff",
        goal="Add caching layer to API",
        terminal_reason="revision_cycles_exhausted",
        rejected_summaries=[
            "implementer wrote cache.py but tests never ran",
            "implementer forgot to wire cache into request handler",
            "implementer skipped error path",
        ],
    )
    [p] = patterns.compute_patterns(mas)
    assert len(p.rejected_attempts_sample) == 3
    assert all(s.startswith("[implementer/failure]") for s in p.rejected_attempts_sample)
    assert "wrote cache.py" in p.rejected_attempts_sample[0]


def test_refresh_writes_jsonl_and_is_idempotent(mas):
    _make_failed_task(
        mas,
        task_id="20260505-idempotent-1111",
        goal="Implement parser for new log format",
        terminal_reason="revision_cycles_exhausted",
    )
    patterns.refresh(mas)
    p = patterns.patterns_path(mas)
    assert p.exists()
    first = p.read_text()

    # Second refresh with no new failures must produce identical output
    patterns.refresh(mas)
    assert p.read_text() == first

    # Each line is a valid JSON object that parses back to FailurePattern shape
    lines = [l for l in p.read_text().splitlines() if l.strip()]
    assert lines, "patterns.jsonl should have at least one line"
    for line in lines:
        record = json.loads(line)
        assert set(record.keys()) >= {
            "signature", "terminal_reason", "goal_sample", "count",
            "last_seen", "task_ids", "rejected_attempts_sample",
        }


def test_read_patterns_skips_malformed_lines(mas, tmp_path):
    # Write a mix of valid and malformed lines directly
    p = patterns.patterns_path(mas)
    p.parent.mkdir(parents=True, exist_ok=True)
    valid = patterns.FailurePattern(
        signature="max_retries_exceeded|foo bar",
        terminal_reason="max_retries_exceeded",
        goal_sample="foo bar",
        count=1,
        last_seen="2026-05-05T10:00:00+00:00",
        task_ids=["t1"],
    )
    p.write_text(
        valid.model_dump_json() + "\n"
        + "{not valid json}\n"
        + json.dumps({"signature": "x"}) + "\n"  # missing required fields
        + valid.model_dump_json() + "\n"
    )

    out = patterns.read_patterns(mas)
    assert len(out) == 2
    assert all(r["signature"] == valid.signature for r in out)


def test_read_patterns_returns_empty_when_file_missing(mas):
    assert patterns.read_patterns(mas) == []


def test_refresh_called_from_run_tick_writes_index(mas, tmp_path, monkeypatch):
    """End-to-end: a tick run produces patterns.jsonl from the failed/ board."""
    from mas import tick as tick_mod
    from mas.config import load_config

    _make_failed_task(
        mas,
        task_id="20260505-tick-suffix-2222",
        goal="Add tracing to scheduler",
        terminal_reason="revision_cycles_exhausted",
    )

    # Minimal config so run_tick doesn't error on validation
    cfg_yaml = """
providers:
  claude-code:
    cli: claude
    max_concurrent: 1
roles:
  proposer:
    provider: claude-code
    model: claude-haiku-4-5-20251001
    timeout_s: 60
  orchestrator:
    provider: claude-code
    model: claude-opus-4-6
    timeout_s: 60
  implementer:
    provider: claude-code
    model: claude-sonnet-4-6
    timeout_s: 60
  tester:
    provider: claude-code
    model: claude-haiku-4-5-20251001
    timeout_s: 60
  evaluator:
    provider: claude-code
    model: claude-haiku-4-5-20251001
    timeout_s: 60
max_proposed: 0
"""
    (mas / "config.yaml").write_text(cfg_yaml)

    # Avoid dispatching anything: max_proposed=0 + no doing/ work + bypass
    # validate_environment by stubbing it. Stub `_advance_doing` and
    # `_maybe_dispatch_proposer` so the tick is purely a refresh test.
    monkeypatch.setattr(tick_mod, "validate_config", lambda *a, **kw: [])
    monkeypatch.setattr(tick_mod, "_advance_doing", lambda env: None)
    monkeypatch.setattr(tick_mod, "_maybe_dispatch_proposer", lambda env: None)
    monkeypatch.setattr(tick_mod, "_reap_workers", lambda env: None)

    cfg = load_config(mas)
    tick_mod.run_tick(start=mas.parent, cfg=cfg)

    p = patterns.patterns_path(mas)
    assert p.exists()
    records = patterns.read_patterns(mas)
    assert len(records) == 1
    assert records[0]["terminal_reason"] == "revision_cycles_exhausted"
    assert "20260505-tick-suffix-2222" in records[0]["task_ids"]


def test_proposer_signals_include_failure_patterns(mas, tmp_path, monkeypatch):
    """gather_proposer_signals must surface read_patterns() output."""
    from mas.roles import gather_proposer_signals
    from mas.patterns import read_success_patterns

    # Stub out read_success_patterns to avoid NotImplementedError
    monkeypatch.setattr("mas.patterns.read_success_patterns", lambda *a, **kw: [])

    _make_failed_task(
        mas,
        task_id="20260505-gather-3333",
        goal="Add health check endpoint",
        terminal_reason="convergence_detected jaccard=0.92",
    )
    patterns.refresh(mas)

    signals = gather_proposer_signals(tmp_path, mas_root=mas)
    assert isinstance(signals.failure_patterns, list)
    assert len(signals.failure_patterns) == 1
    rec = signals.failure_patterns[0]
    assert rec["terminal_reason"].startswith("convergence_detected")
    assert "20260505-gather-3333" in rec["task_ids"]


# --- Success pattern tests -------------------------------------------------


def _make_done_task(
    mas,
    *,
    task_id: str,
    goal: str,
    duration_s: float = 0.0,
    cost_usd: float | None = None,
    verdict: str = "pass",
    role: str = "implementer",
) -> None:
    """Create a fixture done/ task with task.json, result.json, and transitions."""
    # Ensure task_id matches pattern: {yyyymmdd}-{slug}-{hash4}
    import re
    if not re.match(r"^\d{8}-[a-zA-Z0-9_-]+-[a-f0-9]{4}$", task_id):
        # Generate a valid task_id if the provided one doesn't match
        from mas.ids import task_id as new_task_id
        task_id = new_task_id(goal, salt=task_id)

    done = mas / "tasks" / "done" / task_id
    done.mkdir(parents=True, exist_ok=True)
    from mas.schemas import Task
    from mas import transitions as _transitions
    board.write_task(done, Task(id=task_id, role=role, goal=goal))
    _transitions.log_transition(done, "proposed", "doing", "manual_promote")
    _transitions.log_transition(done, "doing", "done", "role_success")
    from mas.schemas import Result
    result = Result(
        task_id=task_id,
        status="success",
        summary=goal,
        duration_s=duration_s,
        cost_usd=cost_usd,
        verdict=verdict,
    )
    (done / "result.json").write_text(result.model_dump_json(indent=2))


def test_compute_success_patterns_empty_when_no_done_tasks(mas):
    from mas.patterns import compute_success_patterns
    assert compute_success_patterns(mas) == []


def test_compute_success_patterns_groups_by_normalized_goal(mas):
    from mas.patterns import compute_success_patterns
    _make_done_task(mas, task_id="20260506-add-mcp-tool-aaaa", goal="Add an MCP tool", duration_s=10.0, cost_usd=0.50)
    _make_done_task(mas, task_id="20260506-add-mcp-tool-bbbb", goal="add MCP tool", duration_s=20.0, cost_usd=1.50)
    patterns = compute_success_patterns(mas)
    assert len(patterns) == 1
    assert patterns[0].count == 2
    assert set(patterns[0].task_ids) == {"20260506-add-mcp-tool-aaaa", "20260506-add-mcp-tool-bbbb"}


def test_compute_success_patterns_captures_duration_and_cost(mas):
    from mas.patterns import compute_success_patterns
    _make_done_task(mas, task_id="20260506-add-caching-cccc", goal="Add caching", duration_s=10.0, cost_usd=0.50)
    _make_done_task(mas, task_id="20260506-add-caching-dddd", goal="Add caching", duration_s=30.0, cost_usd=1.50)
    patterns = compute_success_patterns(mas)
    assert len(patterns) == 1
    assert patterns[0].avg_duration_s == 20.0
    assert patterns[0].avg_cost_usd == 1.0


def test_compute_success_patterns_skips_proposer_role(mas):
    from mas.patterns import compute_success_patterns
    _make_done_task(mas, task_id="20260506-proposer-eeee", goal="Propose a new task", role="proposer")
    assert compute_success_patterns(mas) == []


def test_success_signature_collision(mas):
    from mas.patterns import compute_success_patterns
    from mas.patterns import SuccessPattern
    _make_done_task(mas, task_id="20260506-add-feature-ffff", goal="Add feature X")
    _make_done_task(mas, task_id="20260506-add-feature-gggg", goal="add feature x")
    patterns = compute_success_patterns(mas)
    assert len(patterns) == 1
    sig = patterns[0].signature
    assert isinstance(sig, str)
    # Different goal should produce different signature
    _make_done_task(mas, task_id="20260506-different-hhhh", goal="Completely different goal")
    patterns2 = compute_success_patterns(mas)
    sigs = [p.signature for p in patterns2]
    assert len(set(sigs)) == 2


def test_refresh_success_writes_jsonl(mas):
    from mas.patterns import refresh_success, success_patterns_path, read_success_patterns
    _make_done_task(mas, task_id="20260506-add-feature-iiii", goal="Add feature", duration_s=5.0)
    refresh_success(mas)
    p = success_patterns_path(mas)
    assert p.exists()
    first = p.read_text()
    # Second refresh should produce same output (idempotent)
    refresh_success(mas)
    assert p.read_text() == first
    # Each line is valid JSON that parses to SuccessPattern shape
    lines = [l for l in first.splitlines() if l.strip()]
    assert lines
    for line in lines:
        import json
        record = json.loads(line)
        assert set(record.keys()) >= {
            "signature", "goal_sample", "count", "task_ids",
        }


def test_read_success_patterns_empty_when_file_missing(mas):
    from mas.patterns import read_success_patterns
    assert read_success_patterns(mas) == []


def test_read_success_patterns_skips_malformed_lines(mas):
    from mas.patterns import read_success_patterns, success_patterns_path
    import json
    p = success_patterns_path(mas)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Write mix of valid and malformed lines
    p.write_text(
        '{"signature": "sig1", "goal_sample": "goal", "count": 1, "task_ids": ["t1"]}\n'
        + "{not valid json}\n"
        + '{"signature": "sig2"}\n'  # missing required fields
        + '{"signature": "sig3", "goal_sample": "goal2", "count": 1, "task_ids": ["t2"]}\n'
    )
    out = read_success_patterns(mas)
    assert len(out) == 2


def test_refresh_success_called_from_run_tick(mas, tmp_path, monkeypatch):
    """End-to-end: a tick run produces success_patterns.jsonl from the done/ board."""
    from mas import tick as tick_mod
    from mas.config import load_config

    _make_done_task(mas, task_id="20260506-add-tracing-cd34", goal="Add tracing", duration_s=10.0, cost_usd=0.75)

    cfg_yaml = """
providers:
  claude-code:
    cli: claude
    max_concurrent: 1
roles:
  proposer:
    provider: claude-code
    model: claude-haiku-4-5-20251001
    timeout_s: 60
  orchestrator:
    provider: claude-code
    model: claude-opus-4-6
    timeout_s: 60
  implementer:
    provider: claude-code
    model: claude-sonnet-4-6
    timeout_s: 60
  tester:
    provider: claude-code
    model: claude-haiku-4-5-20251001
    timeout_s: 60
  evaluator:
    provider: claude-code
    model: claude-haiku-4-5-20251001
    timeout_s: 60
max_proposed: 0
"""
    (mas / "config.yaml").write_text(cfg_yaml)

    monkeypatch.setattr(tick_mod, "validate_config", lambda *a, **kw: [])
    monkeypatch.setattr(tick_mod, "_advance_doing", lambda env: None)
    monkeypatch.setattr(tick_mod, "_maybe_dispatch_proposer", lambda env: None)
    monkeypatch.setattr(tick_mod, "_reap_workers", lambda env: None)

    # Do NOT stub refresh_success — let it raise NotImplementedError
    # so this test fails for the right reason until implementation exists.
    cfg = load_config(mas)
    tick_mod.run_tick(start=mas.parent, cfg=cfg)

    from mas.patterns import success_patterns_path, read_success_patterns
    p = success_patterns_path(mas)
    assert p.exists()
    records = read_success_patterns(mas)
    assert len(records) == 1
    assert "20260506-add-tracing-cd34" in records[0]["task_ids"]


def test_proposer_signals_include_success_patterns(mas, tmp_path):
    """gather_proposer_signals must surface read_success_patterns() output."""
    from mas.roles import gather_proposer_signals
    from mas.patterns import refresh_success

    # Setup: create done task and generate success patterns
    # Task ID must have valid hex chars in last 4 positions
    _make_done_task(
        mas,
        task_id="20260506-sig-test-ab12",
        goal="Test success pattern signal inclusion",
        duration_s=5.0,
        cost_usd=0.25,
    )
    refresh_success(mas)

    # Do NOT patch read_success_patterns — let it raise NotImplementedError
    # so this test fails for the right reason until implementation exists.
    signals = gather_proposer_signals(tmp_path, mas_root=mas)
    assert isinstance(signals.success_patterns, list)
    assert len(signals.success_patterns) == 1
    rec = signals.success_patterns[0]
    assert "20260506-sig-test-ab12" in rec["task_ids"]
