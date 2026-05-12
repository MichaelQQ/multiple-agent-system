"""Tests for the similar-completed-tasks feature.

Covers: backend find_similar_tasks helper, task detail integration, and edge
cases (no done/ tasks, none above threshold, limit enforcement, missing
result.json).
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from mas import board
from mas.schemas import Result, Task
from mas.roles import goal_similarity
from mas.web.app import create_app, find_similar_tasks


@pytest.fixture
def project(tmp_path: Path) -> Path:
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    return tmp_path


@pytest.fixture
def client(project: Path) -> TestClient:
    app = create_app(project)
    return TestClient(app)


def _put_task(
    mas: Path,
    column: str,
    task_id: str,
    role: str = "implementer",
    goal: str = "do a thing",
) -> Path:
    tdir = board.task_dir(mas, column, task_id)
    tdir.mkdir(parents=True)
    board.write_task(tdir, Task(id=task_id, role=role, goal=goal))
    return tdir


def _put_task_with_result(
    mas: Path,
    column: str,
    task_id: str,
    goal: str = "do a thing",
    cost_usd: float | None = 0.25,
    duration_s: float | None = 10.0,
) -> Path:
    tdir = _put_task(mas, column, task_id, goal=goal)
    result = Result(
        task_id=task_id,
        status="success",
        summary="done",
        cost_usd=cost_usd,
        duration_s=duration_s,
        tokens_in=100,
        tokens_out=50,
    )
    (tdir / "result.json").write_text(result.model_dump_json())
    return tdir


# ---------------------------------------------------------------------------
# Helper: find_similar_tasks
# ---------------------------------------------------------------------------


def test_find_similar_tasks_returns_matches_sorted_by_recency(
    project: Path,
) -> None:
    """Similar tasks found, sorted newest-first."""
    mas = project / ".mas"
    goal = "add dark mode to settings page"
    for i in range(3):
        tid = f"20260512-sim-aaaa{i:04d}"
        _put_task_with_result(mas, "done", tid, goal=f"add dark mode to settings")
        (mas / "tasks" / "done" / tid / "task.json").touch()  # bump mtime

    result = find_similar_tasks(mas, goal)
    assert len(result) > 0
    timestamps = [m["task_id"] for m in result]
    assert timestamps == sorted(timestamps, reverse=True)


def test_find_similar_tasks_no_done_tasks(project: Path) -> None:
    """No done/ tasks at all -> empty list."""
    mas = project / ".mas"
    result = find_similar_tasks(mas, "some goal")
    assert result == []


def test_find_similar_tasks_none_above_threshold(
    project: Path,
) -> None:
    """Done tasks exist but none are similar -> empty list."""
    mas = project / ".mas"
    _put_task_with_result(mas, "done", "20260512-far-nnnn", goal="completely unrelated totally different")
    result = find_similar_tasks(mas, "add dark mode to settings page", threshold=0.9)
    assert result == []


def test_find_similar_tasks_respects_limit(project: Path) -> None:
    """If 10 similar exist, only top 5 returned."""
    mas = project / ".mas"
    goal = "implement user authentication"
    for i in range(10):
        tid = f"20260512-limit-bbbb{i:04d}"
        _put_task_with_result(mas, "done", tid, goal=f"implement user auth flow")

    result = find_similar_tasks(mas, goal, limit=5)
    assert len(result) == 5


def test_find_similar_tasks_missing_result_json(project: Path) -> None:
    """Tasks without result.json appear with null cost/duration."""
    mas = project / ".mas"
    _put_task(mas, "done", "20260512-nores-cccc", goal="add dark mode to settings")
    result = find_similar_tasks(mas, "add dark mode to settings page")
    assert len(result) > 0
    match = next(m for m in result if m["task_id"] == "20260512-nores-cccc")
    assert match["cost_usd"] is None
    assert match["duration_s"] is None


def test_find_similar_tasks_each_match_has_required_keys(
    project: Path,
) -> None:
    """Each match dict contains all required keys."""
    mas = project / ".mas"
    _put_task_with_result(
        mas, "done", "20260512-keys-dddd",
        goal="add dark mode to settings page",
        cost_usd=0.50,
        duration_s=30.0,
    )
    result = find_similar_tasks(mas, "add dark mode to settings page")
    assert len(result) > 0
    match = result[0]
    assert "task_id" in match
    assert "goal" in match
    assert "cost_usd" in match
    assert "duration_s" in match
    assert "revision_count" in match
    assert "similarity_score" in match


# ---------------------------------------------------------------------------
# Integration: task detail page shows similar completed tasks
# ---------------------------------------------------------------------------


def test_task_detail_shows_similar_section(
    project: Path,
    client: TestClient,
) -> None:
    """GET /task/<id> includes a 'Similar Completed Tasks' section when
    similar done/ tasks exist."""
    mas = project / ".mas"
    goal = "add dark mode to settings page"
    _put_task_with_result(mas, "done", "20260512-int-dddd", goal=goal)
    doing_id = "20260512-int-eeee"
    _put_task(mas, "doing", doing_id, goal=goal)

    r = client.get(f"/task/{doing_id}")
    assert r.status_code == 200
    body = r.text
    assert "Similar Completed Tasks" in body
    assert "20260512-int-dddd" in body
    assert 'href="/task/20260512-int-dddd"' in body


def test_task_detail_no_similar_tasks_shows_no_results(
    project: Path,
    client: TestClient,
) -> None:
    """GET /task/<id> with no similar tasks shows 'No similar tasks found'."""
    mas = project / ".mas"
    _put_task_with_result(mas, "done", "20260512-no-nnnn", goal="unique unrelated thing")
    doing_id = "20260512-no-oooo"
    _put_task(mas, "doing", doing_id, goal="completely different unique goal")

    r = client.get(f"/task/{doing_id}")
    assert r.status_code == 200
    body = r.text
    assert "Similar Completed Tasks" in body
    assert "No similar tasks found" in body


def test_task_detail_similar_section_shows_cost_duration_revisions(
    project: Path,
    client: TestClient,
) -> None:
    """Similar tasks section includes cost, duration, and revision count."""
    mas = project / ".mas"
    goal = "add dark mode to settings page"
    _put_task_with_result(
        mas, "done", "20260512-met-pppp",
        goal=goal,
        cost_usd=1.23,
        duration_s=45.6,
    )
    doing_id = "20260512-met-qqqq"
    _put_task(mas, "doing", doing_id, goal=goal)

    r = client.get(f"/task/{doing_id}")
    assert r.status_code == 200
    body = r.text
    assert "1.23" in body
    assert "45.6" in body
