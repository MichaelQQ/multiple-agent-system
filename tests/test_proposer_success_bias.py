"""Tests for proposer success bias — success_patterns in ProposerSignals."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mas.schemas import ProposerSignals
from mas import board, patterns
from mas import transitions as _transitions
from mas.schemas import Task, Result


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
    board.write_task(done, Task(id=task_id, role=role, goal=goal))
    _transitions.log_transition(done, "proposed", "doing", "manual_promote")
    _transitions.log_transition(done, "doing", "done", "role_success")
    result = Result(
        task_id=task_id,
        status="success",
        summary=goal,
        duration_s=duration_s,
        cost_usd=cost_usd,
        verdict=verdict,
    )
    (done / "result.json").write_text(result.model_dump_json(indent=2))


@pytest.fixture
def mas(tmp_path):
    d = tmp_path / ".mas"
    for col in ("proposed", "doing", "done", "failed"):
        (d / "tasks" / col).mkdir(parents=True)
    return d


def test_gather_success_signals_returns_top_10(mas, tmp_path):
    """When >10 success patterns exist, only top 10 by count returned."""
    from mas.roles import gather_proposer_signals

    # Write 15 success patterns directly to the JSONL file
    import json
    from mas.patterns import success_patterns_path
    p = success_patterns_path(mas)
    p.parent.mkdir(parents=True, exist_ok=True)
    records = []
    for i in range(15):
        records.append(
            json.dumps({
                "signature": f"sig-{i}",
                "goal_sample": f"goal {i}",
                "count": i + 1,  # ascending: 1, 2, ..., 15 (unsorted for descending)
                "task_ids": [f"20260506-unique-goal-{i:04d}"],
            })
        )
    p.write_text("\n".join(records) + "\n")

    # Do NOT patch read_success_patterns — let it raise NotImplementedError
    # so this test fails for the right reason until implementation exists.
    signals = gather_proposer_signals(tmp_path, mas_root=mas)
    assert isinstance(signals.success_patterns, list)
    assert len(signals.success_patterns) <= 10
    # Also verify they are sorted by count descending (top 10 by count)
    counts = [p["count"] for p in signals.success_patterns]
    assert counts == sorted(counts, reverse=True)


def test_success_patterns_field_in_proposer_signals(mas, tmp_path, monkeypatch):
    """ProposerSignals schema accepts success_patterns field."""
    from mas.roles import gather_proposer_signals

    # Stub read_success_patterns to return empty list
    monkeypatch.setattr(
        "mas.patterns.read_success_patterns",
        lambda *a, **kw: []
    )

    signals = gather_proposer_signals(tmp_path, mas_root=mas)
    assert hasattr(signals, "success_patterns")
    assert isinstance(signals.success_patterns, list)
    # Validate it conforms to ProposerSignals schema
    assert isinstance(signals, ProposerSignals)
