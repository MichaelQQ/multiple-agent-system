"""Tests for the failure-history feature on the task detail page (/task/<id>)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from mas import board
from mas.patterns import FailurePattern, patterns_path
from mas.roles import goal_similarity
from mas.schemas import Task
from mas.web.app import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A tmp project with a .mas/ layout."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    return tmp_path


@pytest.fixture
def client(project: Path) -> TestClient:
    app = create_app(project)
    return TestClient(app)


def _put_task(mas: Path, column: str, task_id: str, role: str = "implementer", goal: str = "do a thing") -> Path:
    tdir = board.task_dir(mas, column, task_id)
    tdir.mkdir(parents=True)
    board.write_task(tdir, Task(id=task_id, role=role, goal=goal))
    return tdir


def _write_patterns(mas: Path, patterns: list[FailurePattern]) -> Path:
    p = patterns_path(mas)
    with p.open("w") as f:
        for pat in patterns:
            f.write(pat.model_dump_json() + "\n")
    return p


def _make_pattern(
    goal_sample: str,
    terminal_reason: str = "revision_cycles_exhausted",
    count: int = 1,
    task_ids: list[str] | None = None,
    rejected_attempts_sample: list[str] | None = None,
) -> FailurePattern:
    from mas.roles import _goal_tokens

    tokens = sorted(_goal_tokens(goal_sample or ""))
    canonical = " ".join(tokens) if tokens else (goal_sample or "").strip().lower()[:80]
    sig = f"{terminal_reason}|{canonical}"
    return FailurePattern(
        signature=sig,
        terminal_reason=terminal_reason,
        goal_sample=goal_sample,
        count=count,
        last_seen=datetime.now(timezone.utc).isoformat(),
        task_ids=task_ids or [],
        rejected_attempts_sample=rejected_attempts_sample or [],
    )


# ---------------------------------------------------------------------------
# Test 1: matching failure patterns are shown
# ---------------------------------------------------------------------------

def test_task_detail_shows_matching_failure_patterns(project: Path, client: TestClient):
    mas = project / ".mas"
    task_id = "20260506-match-aaaa"
    goal = "Implement surface failure pattern data in 8b10"
    _put_task(mas, "doing", task_id, goal=goal)

    # Verify similarity is above threshold
    pattern_goal = "surface failure pattern data in 8b10"
    sim = goal_similarity(goal, pattern_goal)
    assert sim >= 0.7, f"Test setup: goal_similarity={sim} < 0.7"

    pat = _make_pattern(
        goal_sample=pattern_goal,
        terminal_reason="revision_cycles_exhausted",
        count=2,
        task_ids=[task_id, "20260506-other-bbbb"],
    )
    _write_patterns(mas, [pat])

    r = client.get(f"/task/{task_id}")
    assert r.status_code == 200
    body = r.text

    assert "Failure history" in body
    assert pattern_goal in body
    assert "revision_cycles_exhausted" in body
    assert "2" in body  # count
    assert task_id in body
    assert "/task/20260506-other-bbbb" in body  # clickable link


# ---------------------------------------------------------------------------
# Test 2: no patterns.jsonl file
# ---------------------------------------------------------------------------

def test_task_detail_no_patterns_file(project: Path, client: TestClient):
    mas = project / ".mas"
    task_id = "20260506-nopat-aaaa"
    _put_task(mas, "doing", task_id, goal="some goal")

    # Ensure patterns.jsonl does NOT exist
    assert not patterns_path(mas).exists()

    r = client.get(f"/task/{task_id}")
    assert r.status_code == 200
    assert "No failure patterns yet" in r.text


# ---------------------------------------------------------------------------
# Test 3: empty patterns.jsonl
# ---------------------------------------------------------------------------

def test_task_detail_empty_patterns_file(project: Path, client: TestClient):
    mas = project / ".mas"
    task_id = "20260506-emptypat-bbbb"
    _put_task(mas, "doing", task_id, goal="another goal")

    patterns_path(mas).write_text("")

    r = client.get(f"/task/{task_id}")
    assert r.status_code == 200
    assert "No failure patterns yet" in r.text


# ---------------------------------------------------------------------------
# Test 4: no matching patterns (unrelated goal)
# ---------------------------------------------------------------------------

def test_task_detail_no_matching_patterns(project: Path, client: TestClient):
    mas = project / ".mas"
    task_id = "20260506-nomatch-cccc"
    goal = "Implement caching layer"
    _put_task(mas, "doing", task_id, goal=goal)

    # Pattern with unrelated goal — similarity should be low
    pattern_goal = "Fix mobile auth token rotation"
    sim = goal_similarity(goal, pattern_goal)
    assert sim < 0.7, f"Test setup: goal_similarity={sim} >= 0.7, goals too similar"

    pat = _make_pattern(
        goal_sample=pattern_goal,
        terminal_reason="unknown",
        count=1,
        task_ids=["20260506-unrelated-dddd"],
    )
    _write_patterns(mas, [pat])

    r = client.get(f"/task/{task_id}")
    assert r.status_code == 200
    assert "No failure patterns yet" in r.text


# ---------------------------------------------------------------------------
# Test 5: filter high severity (?failure_filter=blocking)
# ---------------------------------------------------------------------------

def test_task_detail_filter_high_severity(project: Path, client: TestClient):
    mas = project / ".mas"
    task_id = "20260506-filter-dddd"
    _put_task(mas, "doing", task_id, goal="surface failure pattern data in 8b10")

    # Low severity: count=1, unknown reason
    pat_low = _make_pattern(
        goal_sample="surface failure pattern data in 8b10",
        terminal_reason="unknown",
        count=1,
        task_ids=[task_id],
    )
    # High severity: count=3, revision_cycles_exhausted
    pat_high = _make_pattern(
        goal_sample="surface failure pattern data in 8b10",
        terminal_reason="revision_cycles_exhausted",
        count=3,
        task_ids=[task_id, "20260506-high-eeee", "20260506-high-ffff"],
    )
    _write_patterns(mas, [pat_low, pat_high])

    r = client.get(f"/task/{task_id}?failure_filter=blocking")
    assert r.status_code == 200
    body = r.text

    # High severity pattern should appear
    assert "revision_cycles_exhausted" in body
    assert "3" in body
    # Low severity pattern should NOT appear
    # (it has terminal_reason=unknown and count=1, so it's filtered out)
    assert "unknown" not in body or "1" not in body


# ---------------------------------------------------------------------------
# Test 6: rejected_attempts_sample shown
# ---------------------------------------------------------------------------

def test_failure_history_shows_rejected_attempts_sample(project: Path, client: TestClient):
    mas = project / ".mas"
    task_id = "20260506-rejected-eeee"
    goal = "surface failure pattern data in 8b10"
    _put_task(mas, "doing", task_id, goal=goal)

    pat = _make_pattern(
        goal_sample=goal,
        terminal_reason="revision_cycles_exhausted",
        count=2,
        task_ids=[task_id],
        rejected_attempts_sample=[
            "[implementer/failure] syntax error in module",
            "[tester/failure] tests failed after changes",
        ],
    )
    _write_patterns(mas, [pat])

    r = client.get(f"/task/{task_id}")
    assert r.status_code == 200
    body = r.text

    assert "Failure history" in body
    assert "syntax error in module" in body
    assert "tests failed after changes" in body
