"""Tests for dashboard filtering in the mas web UI.

Covers: filtering by task_id, status, cost range, failure reason,
date range, combined filters, and match count display.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from mas import board
from mas.schemas import Plan, Result, SubtaskSpec, Task
from mas.web.app import create_app


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A tmp project with a .mas/ layout; mas.web operates on this root."""
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


def _put_task_with_result(
    mas: Path,
    column: str,
    task_id: str,
    role: str = "implementer",
    goal: str = "do a thing",
    summary: str = "failure occurred",
) -> Path:
    """Create a task with result.json. For failed tasks, summary contains the failure_reason."""
    tdir = _put_task(mas, column, task_id, role=role, goal=goal)
    result = Result(
        task_id=task_id,
        status="failure" if column == "failed" else "success",
        summary=summary,
        verdict="fail" if column == "failed" else "pass",
    )
    (tdir / "result.json").write_text(result.model_dump_json())
    return tdir


def _put_task_with_cost(
    mas: Path,
    column: str,
    task_id: str,
    role: str = "implementer",
    goal: str = "do a thing",
    subtasks: list[tuple[str, float]] | None = None,
) -> Path:
    """Create parent task with subtasks that have result.json containing cost_usd."""
    if subtasks is None:
        subtasks = [("20260507-sub-1111", 0.5)]
    tdir = _put_task(mas, column, task_id, role=role, goal=goal)
    plan = Plan(
        parent_id=task_id,
        summary="cost test plan",
        subtasks=[
            SubtaskSpec(id=sub_id, role="implementer", goal=f"impl {sub_id}")
            for sub_id, _ in subtasks
        ],
    )
    (tdir / "plan.json").write_text(plan.model_dump_json())
    subs_dir = tdir / "subtasks"
    for sub_id, cost in subtasks:
        sub_tdir = subs_dir / sub_id
        sub_tdir.mkdir(parents=True)
        board.write_task(sub_tdir, Task(id=sub_id, role="implementer", goal=f"impl {sub_id}", parent_id=task_id))
        sub_result = Result(
            task_id=sub_id,
            status="success",
            summary=f"impl done {sub_id}",
            verdict="pass",
            cost_usd=cost,
        )
        (sub_tdir / "result.json").write_text(sub_result.model_dump_json())
    return tdir


def _put_task_with_created_at(
    mas: Path,
    column: str,
    task_id: str,
    created_at: datetime,
    role: str = "implementer",
    goal: str = "do a thing",
) -> Path:
    """Create a task with explicit created_at timestamp for date filtering tests."""
    tdir = board.task_dir(mas, column, task_id)
    tdir.mkdir(parents=True)
    board.write_task(tdir, Task(id=task_id, role=role, goal=goal, created_at=created_at))
    return tdir


class TestFilterByTaskId:
    def test_filter_by_task_id_substring(self, project: Path, client: TestClient):
        """GET /?task_id=aaaa should only show tasks with 'aaaa' in their id."""
        mas = project / ".mas"
        _put_task(mas, "proposed", "20260507-alpha-aaaa", goal="alpha task")
        _put_task(mas, "doing", "20260507-beta-bbbb", goal="beta task")
        _put_task(mas, "done", "20260507-gamma-aaaa", goal="gamma task")

        r = client.get("/", params={"task_id": "aaaa"})
        assert r.status_code == 200
        body = r.text
        assert "20260507-alpha-aaaa" in body
        assert "20260507-gamma-aaaa" in body
        assert "20260507-beta-bbbb" not in body

    def test_filter_by_task_id_no_matches(self, project: Path, client: TestClient):
        """GET /?task_id=nonexistent should not show any tasks."""
        mas = project / ".mas"
        _put_task(mas, "proposed", "20260507-alpha-aaaa")

        r = client.get("/", params={"task_id": "nonexistent"})
        assert r.status_code == 200
        assert "20260507-alpha-aaaa" not in r.text


class TestFilterByStatus:
    def test_filter_by_status_single(self, project: Path, client: TestClient):
        """GET /?status=failed should only show failed tasks."""
        mas = project / ".mas"
        _put_task(mas, "proposed", "20260507-proposed-1111")
        _put_task(mas, "doing", "20260507-doing-2222")
        _put_task(mas, "done", "20260507-done-3333")
        _put_task(mas, "failed", "20260507-failed-4444")

        r = client.get("/", params={"status": "failed"})
        assert r.status_code == 200
        body = r.text
        assert "20260507-failed-4444" in body
        assert "20260507-proposed-1111" not in body
        assert "20260507-doing-2222" not in body
        assert "20260507-done-3333" not in body

    def test_filter_by_status_multi_select(self, project: Path, client: TestClient):
        """GET /?status=doing&status=failed should show both columns."""
        mas = project / ".mas"
        _put_task(mas, "proposed", "20260507-proposed-1111")
        _put_task(mas, "doing", "20260507-doing-2222")
        _put_task(mas, "done", "20260507-done-3333")
        _put_task(mas, "failed", "20260507-failed-4444")

        r = client.get("/", params=[("status", "doing"), ("status", "failed")])
        assert r.status_code == 200
        body = r.text
        assert "20260507-doing-2222" in body
        assert "20260507-failed-4444" in body
        assert "20260507-proposed-1111" not in body
        assert "20260507-done-3333" not in body


class TestFilterByCost:
    def test_filter_by_cost_min_max(self, project: Path, client: TestClient):
        """GET /?cost_min=0.5&cost_max=1.5 should only show tasks with aggregated cost in range."""
        mas = project / ".mas"
        _put_task_with_cost(mas, "done", "20260507-cost-low-1111", subtasks=[("impl-1", 0.3)])
        _put_task_with_cost(mas, "done", "20260507-cost-high-2222", subtasks=[("impl-2", 2.0)])
        _put_task_with_cost(mas, "done", "20260507-cost-mid-3333", subtasks=[("impl-3", 1.0)])

        r = client.get("/", params={"cost_min": 0.5, "cost_max": 1.5})
        assert r.status_code == 200
        body = r.text
        assert "20260507-cost-mid-3333" in body
        assert "20260507-cost-low-1111" not in body
        assert "20260507-cost-high-2222" not in body


class TestFilterByFailureReason:
    def test_filter_by_failure_reason_keyword(self, project: Path, client: TestClient):
        """GET /?failure_reason=timeout should show failed tasks with keyword in result summary."""
        mas = project / ".mas"
        _put_task_with_result(mas, "failed", "20260507-fail-timeout-1111", summary="timeout occurred")
        _put_task_with_result(mas, "failed", "20260507-fail-other-2222", summary="other error")
        _put_task_with_result(mas, "done", "20260507-done-3333", summary="success")

        r = client.get("/", params={"failure_reason": "timeout"})
        assert r.status_code == 200
        body = r.text
        assert "20260507-fail-timeout-1111" in body
        assert "20260507-fail-other-2222" not in body
        assert "20260507-done-3333" not in body


class TestFilterByDate:
    def test_filter_by_date_from_to(self, project: Path, client: TestClient):
        """GET /?date_from=2026-05-01&date_to=2026-05-03 shows only tasks in range."""
        mas = project / ".mas"
        _put_task_with_created_at(
            mas, "proposed", "20260507-may-1",
            datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        )
        _put_task_with_created_at(
            mas, "proposed", "20260507-may-2",
            datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
        )
        _put_task_with_created_at(
            mas, "proposed", "20260507-may-4",
            datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc),
        )

        r = client.get("/", params={"date_from": "2026-05-01", "date_to": "2026-05-03"})
        assert r.status_code == 200
        body = r.text
        assert "20260507-may-1" in body
        assert "20260507-may-2" in body
        assert "20260507-may-4" not in body


class TestCombinedFilters:
    def test_combined_task_id_and_status(self, project: Path, client: TestClient):
        """GET /?task_id=aaa&status=doing should show intersection."""
        mas = project / ".mas"
        _put_task(mas, "doing", "20260507-aaa-doing-1111")
        _put_task(mas, "proposed", "20260507-aaa-proposed-2222")
        _put_task(mas, "doing", "20260507-bbb-doing-3333")

        r = client.get("/", params={"task_id": "aaa", "status": "doing"})
        assert r.status_code == 200
        body = r.text
        assert "20260507-aaa-doing-1111" in body
        assert "20260507-aaa-proposed-2222" not in body
        assert "20260507-bbb-doing-3333" not in body


class TestEmptyFilters:
    def test_empty_filters_returns_all(self, project: Path, client: TestClient):
        """GET / with no params returns all tasks (backward compat)."""
        mas = project / ".mas"
        _put_task(mas, "proposed", "20260507-all-1111")
        _put_task(mas, "doing", "20260507-all-2222")
        _put_task(mas, "done", "20260507-all-3333")
        _put_task(mas, "failed", "20260507-all-4444")

        r = client.get("/")
        assert r.status_code == 200
        body = r.text
        assert "20260507-all-1111" in body
        assert "20260507-all-2222" in body
        assert "20260507-all-3333" in body
        assert "20260507-all-4444" in body


class TestNoMatches:
    def test_no_matches_shows_zero_count(self, project: Path, client: TestClient):
        """GET with filter matching nothing should show '0 of N tasks'."""
        mas = project / ".mas"
        _put_task(mas, "proposed", "20260507-exists-1111")

        r = client.get("/", params={"task_id": "nonexistent"})
        assert r.status_code == 200
        body = r.text.lower()
        assert "0 of 1" in body


class TestMatchCountDisplay:
    def test_match_count_display(self, project: Path, client: TestClient):
        """GET /?task_id=aaa should show 'X of Y tasks' in response."""
        mas = project / ".mas"
        _put_task(mas, "proposed", "20260507-aaa-1111")
        _put_task(mas, "proposed", "20260507-aaa-2222")
        _put_task(mas, "proposed", "20260507-bbb-3333")

        r = client.get("/", params={"task_id": "aaa"})
        assert r.status_code == 200
        body = r.text.lower()
        assert "2 of 3" in body
