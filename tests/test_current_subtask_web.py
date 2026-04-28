"""Tests for .current_subtask visibility in the web layer.

Tests encode:
5. _task_detail reads .current_subtask and returns current_subtask dict with elapsed_s
6. _task_detail returns current_subtask: None when no marker file
7. task.html renders current subtask line when present
8. task.html renders placeholder when no subtask running and task is in doing
9. elapsed_s calculation is accurate (60s ago ~ 60 seconds)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from mas import board
from mas.schemas import Plan, Result, SubtaskSpec, Task
from mas.web.app import _task_detail, create_app


@pytest.fixture
def project(tmp_path: Path) -> Path:
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    return tmp_path


@pytest.fixture
def client(project: Path) -> TestClient:
    return TestClient(create_app(project))


def _put_doing_task(mas: Path, task_id: str) -> Path:
    tdir = board.task_dir(mas, "doing", task_id)
    tdir.mkdir(parents=True)
    board.write_task(tdir, Task(id=task_id, role="orchestrator", goal="g"))
    return tdir


# ---------------------------------------------------------------------------
# 5. _task_detail reads .current_subtask and computes elapsed_s
# ---------------------------------------------------------------------------

def test_task_detail_reads_current_subtask_marker(project: Path):
    """_task_detail returns a current_subtask dict when .current_subtask exists."""
    mas = project / ".mas"
    task_id = "20260424-cs1-aaaa"
    tdir = _put_doing_task(mas, task_id)

    now_iso = datetime.now(timezone.utc).isoformat()
    (tdir / ".current_subtask").write_text(json.dumps({
        "role": "implementer",
        "provider": "mock",
        "pid": 54321,
        "start_time_iso": now_iso,
        "subtask_id": "20260424-impl-1-aaaa",
    }))

    detail = _task_detail(mas, task_id)

    assert detail["current_subtask"] is not None
    assert detail["current_subtask"]["role"] == "implementer"
    assert detail["current_subtask"]["provider"] == "mock"
    assert detail["current_subtask"]["pid"] == 54321
    assert detail["current_subtask"]["subtask_id"] == "20260424-impl-1-aaaa"


def test_task_detail_includes_elapsed_s(project: Path):
    """_task_detail includes elapsed_s (float) in current_subtask, computed from start_time_iso."""
    mas = project / ".mas"
    task_id = "20260424-cs2-aaaa"
    tdir = _put_doing_task(mas, task_id)

    start = datetime.now(timezone.utc) - timedelta(seconds=60)
    start_iso = start.isoformat()
    (tdir / ".current_subtask").write_text(json.dumps({
        "role": "tester",
        "provider": "mock",
        "pid": 12345,
        "start_time_iso": start_iso,
        "subtask_id": "20260424-test-1-aaaa",
    }))

    detail = _task_detail(mas, task_id)

    assert "elapsed_s" in detail["current_subtask"]
    elapsed = detail["current_subtask"]["elapsed_s"]
    assert isinstance(elapsed, float)
    assert 55.0 < elapsed < 65.0


# ---------------------------------------------------------------------------
# 6. _task_detail returns current_subtask: None when no marker
# ---------------------------------------------------------------------------

def test_task_detail_returns_none_when_no_marker(project: Path):
    """_task_detail returns current_subtask: None when .current_subtask does not exist."""
    mas = project / ".mas"
    task_id = "20260424-cs3-aaaa"
    _put_doing_task(mas, task_id)

    detail = _task_detail(mas, task_id)

    assert detail["current_subtask"] is None


# ---------------------------------------------------------------------------
# 7. task.html renders current subtask info when present
# ---------------------------------------------------------------------------

def test_task_html_renders_current_subtask_line(project: Path, client: TestClient):
    """When current_subtask is present, task.html shows role/provider/PID line."""
    mas = project / ".mas"
    task_id = "20260424-cs4-aaaa"
    tdir = _put_doing_task(mas, task_id)

    now_iso = datetime.now(timezone.utc).isoformat()
    (tdir / ".current_subtask").write_text(json.dumps({
        "role": "tester",
        "provider": "claude",
        "pid": 98765,
        "start_time_iso": now_iso,
        "subtask_id": "20260424-tst-1-aaaa",
    }))

    r = client.get(f"/task/{task_id}")
    assert r.status_code == 200
    body = r.text
    assert "tester" in body
    assert "claude" in body
    assert "98765" in body


# ---------------------------------------------------------------------------
# 8. task.html renders placeholder when no subtask running and in doing
# ---------------------------------------------------------------------------

def test_task_html_renders_no_subtask_running_placeholder(project: Path, client: TestClient):
    """Task in doing with no .current_subtask shows 'no subtask currently running'."""
    mas = project / ".mas"
    task_id = "20260424-cs5-aaaa"
    tdir = _put_doing_task(mas, task_id)
    plan = Plan(
        parent_id=task_id,
        summary="s",
        subtasks=[SubtaskSpec(id="20260424-x-aaaa", role="implementer", goal="do")],
    )
    (tdir / "plan.json").write_text(plan.model_dump_json())

    r = client.get(f"/task/{task_id}")
    assert r.status_code == 200
    body = r.text.lower()
    assert "no subtask" in body or "not running" in body or "idle" in body


# ---------------------------------------------------------------------------
# 9. Elapsed time calculation accuracy
# ---------------------------------------------------------------------------

def test_elapsed_s_accurate_60_seconds_ago(project: Path):
    """Given start_time_iso 60 seconds in the past, elapsed_s is approximately 60."""
    from mas.web.app import _get_elapsed_s

    start = datetime.now(timezone.utc) - timedelta(seconds=60)
    start_iso = start.isoformat()
    elapsed = _get_elapsed_s(start_iso)
    assert 55.0 < elapsed < 65.0