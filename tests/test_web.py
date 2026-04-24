"""Tests for the mas web UI.

Covers: board view renders, task detail renders with plan + audit + logs,
and POST actions invoke the same board/daemon helpers the CLI uses.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from mas import board
from mas.audit import append_event
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


def test_board_renders_tasks_by_column(project: Path, client: TestClient):
    mas = project / ".mas"
    _put_task(mas, "proposed", "20260423-alpha-aaaa", goal="propose something")
    _put_task(mas, "doing", "20260423-beta-bbbb", goal="in flight")
    _put_task(mas, "done", "20260423-gamma-cccc", goal="finished work")

    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "20260423-alpha-aaaa" in body
    assert "20260423-beta-bbbb" in body
    assert "20260423-gamma-cccc" in body
    assert "propose something" in body
    assert "daemon" in body.lower()


def test_task_view_shows_plan_and_subtasks(project: Path, client: TestClient):
    mas = project / ".mas"
    task_id = "20260423-plan-dddd"
    tdir = _put_task(mas, "doing", task_id)
    plan = Plan(
        parent_id=task_id,
        summary="decompose",
        subtasks=[
            SubtaskSpec(id="20260423-planimp-1111", role="implementer", goal="impl"),
            SubtaskSpec(id="20260423-plantst-2222", role="tester", goal="test"),
        ],
    )
    (tdir / "plan.json").write_text(plan.model_dump_json())
    sub_impl = tdir / "subtasks" / "20260423-planimp-1111"
    sub_impl.mkdir(parents=True)
    Result(
        task_id="20260423-planimp-1111",
        status="success",
        summary="wrote code",
        verdict="pass",
        tokens_in=100,
        tokens_out=50,
        cost_usd=0.01,
    ).model_dump_json()
    (sub_impl / "result.json").write_text(
        Result(
            task_id="20260423-planimp-1111",
            status="success",
            summary="wrote code",
            verdict="pass",
            tokens_in=100,
            tokens_out=50,
            cost_usd=0.01,
        ).model_dump_json()
    )

    append_event(tdir, event="dispatch", task_id=task_id, role="orchestrator", status="success", summary="dispatched")

    r = client.get(f"/task/{task_id}")
    assert r.status_code == 200
    body = r.text
    assert "20260423-planimp-1111" in body
    assert "20260423-plantst-2222" in body
    assert "wrote code" in body
    assert "dispatched" in body


def test_task_view_renders_goal_as_markdown(project: Path, client: TestClient):
    mas = project / ".mas"
    task_id = "20260424-mdgo-aaaa"
    tdir = board.task_dir(mas, "doing", task_id)
    tdir.mkdir(parents=True)
    goal = "## Heading\n\n- item one\n- item two\n\n```py\nprint('hi')\n```"
    board.write_task(tdir, Task(id=task_id, role="implementer", goal=goal))

    r = client.get(f"/task/{task_id}")
    assert r.status_code == 200
    body = r.text
    assert "<h2>Heading</h2>" in body
    assert "<li>item one</li>" in body
    assert "<code" in body and "language-py" in body


def test_task_view_shows_task_info_fields(project: Path, client: TestClient):
    mas = project / ".mas"
    task_id = "20260424-info-bbbb"
    tdir = board.task_dir(mas, "doing", task_id)
    tdir.mkdir(parents=True)
    board.write_task(
        tdir,
        Task(
            id=task_id,
            role="implementer",
            goal="g",
            inputs={"key": "val"},
            constraints={"budget": 10},
            previous_failure="**boom** happened",
            cycle=2,
            attempt=3,
        ),
    )
    r = client.get(f"/task/{task_id}")
    assert r.status_code == 200
    body = r.text
    assert "Task info" in body
    assert "key" in body and "val" in body
    assert "budget" in body
    assert "<strong>boom</strong>" in body


def test_task_view_404_for_missing(client: TestClient):
    r = client.get("/task/20260423-nope-ffff")
    assert r.status_code == 404


def test_promote_moves_proposed_to_doing(project: Path, client: TestClient):
    mas = project / ".mas"
    task_id = "20260423-prop-eeee"
    _put_task(mas, "proposed", task_id, role="proposer", goal="proposal")
    r = client.post(f"/task/{task_id}/promote", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/task/{task_id}"
    assert (mas / "tasks" / "doing" / task_id).is_dir()
    assert not (mas / "tasks" / "proposed" / task_id).exists()


def test_retry_moves_failed_to_doing_and_resets(project: Path, client: TestClient):
    mas = project / ".mas"
    task_id = "20260423-fail-ffff"
    tdir = _put_task(mas, "failed", task_id)
    (tdir / "result.json").write_text('{"task_id": "x", "status": "failure", "summary": "s"}')
    (tdir / "plan.json").write_text("{}")

    r = client.post(f"/task/{task_id}/retry", follow_redirects=False)
    assert r.status_code == 303
    moved = mas / "tasks" / "doing" / task_id
    assert moved.is_dir()
    assert not (moved / "result.json").exists()
    assert not (moved / "plan.json").exists()


def test_delete_removes_task_from_any_column(project: Path, client: TestClient):
    mas = project / ".mas"
    for col in ("proposed", "doing", "done", "failed"):
        task_id = f"20260424-del{col[0]}-aaaa"
        tdir = _put_task(mas, col, task_id)
        r = client.post(f"/task/{task_id}/delete", follow_redirects=False)
        assert r.status_code == 303, f"{col}: {r.text}"
        assert f"deleted={task_id}" in r.headers["location"]
        assert not tdir.exists()


def test_delete_missing_task_returns_404(client: TestClient):
    r = client.post("/task/20260424-nope-0000/delete", follow_redirects=False)
    assert r.status_code == 404


def test_bulk_delete_removes_multiple_tasks(project: Path, client: TestClient):
    mas = project / ".mas"
    ids = [
        "20260424-bulkp-aaaa",
        "20260424-bulkd-bbbb",
        "20260424-bulko-cccc",
    ]
    _put_task(mas, "proposed", ids[0])
    _put_task(mas, "doing", ids[1])
    _put_task(mas, "done", ids[2])

    r = client.post(
        "/tasks/delete",
        data={"task_ids": ids},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "deleted_count=3" in r.headers["location"]
    for tid in ids:
        assert board.find_task(mas, tid) is None


def test_bulk_delete_skips_missing_and_counts_existing(project: Path, client: TestClient):
    mas = project / ".mas"
    real = "20260424-bulk-dead"
    _put_task(mas, "proposed", real)
    r = client.post(
        "/tasks/delete",
        data={"task_ids": [real, "20260424-nope-0000"]},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "deleted_count=1" in r.headers["location"]


def test_bulk_delete_with_no_selection_redirects(client: TestClient):
    r = client.post("/tasks/delete", data={}, follow_redirects=False)
    assert r.status_code == 303


def test_board_renders_bulk_delete_form(project: Path, client: TestClient):
    mas = project / ".mas"
    _put_task(mas, "proposed", "20260424-render-aaaa")
    r = client.get("/")
    assert r.status_code == 200
    assert 'action="/tasks/delete"' in r.text
    assert 'name="task_ids"' in r.text


def test_tick_spawns_subprocess(project: Path, client: TestClient, monkeypatch):
    captured: dict = {}

    def fake_spawn(proj):
        captured["project"] = proj
        return 4242

    monkeypatch.setattr("mas.web.app._spawn_tick", fake_spawn)
    r = client.post("/tick", follow_redirects=False)
    assert r.status_code == 303
    assert "tick_pid=4242" in r.headers["location"]
    assert captured["project"] == project.resolve()


def test_log_endpoint_returns_tail(project: Path, client: TestClient):
    mas = project / ".mas"
    task_id = "20260423-log-1234"
    tdir = _put_task(mas, "doing", task_id)
    log_dir = tdir / "logs"
    log_dir.mkdir()
    (log_dir / "implementer.codex.log").write_text("line1\nline2\nline3\n")

    r = client.get(f"/task/{task_id}/log/implementer.codex.log")
    assert r.status_code == 200
    assert "line3" in r.text


def test_log_endpoint_rejects_traversal(project: Path, client: TestClient):
    mas = project / ".mas"
    task_id = "20260423-log-5678"
    _put_task(mas, "doing", task_id)
    r = client.get(f"/task/{task_id}/log/..%2Fetc%2Fpasswd")
    # FastAPI returns 400 for our explicit rejection or 404 for missing file.
    assert r.status_code in (400, 404)


def test_daemon_stop_delegates_to_helper(project: Path, client: TestClient, monkeypatch):
    calls: list = []

    def fake_stop(p):
        calls.append(p)
        return True

    monkeypatch.setattr("mas.web.app.daemon.stop", fake_stop)
    r = client.post("/daemon/stop", follow_redirects=False)
    assert r.status_code == 303
    assert calls == [project.resolve()]
