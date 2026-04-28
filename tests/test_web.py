"""Tests for the mas web UI.

Covers: board view renders, task detail renders with plan + audit + logs,
and POST actions invoke the same board/daemon helpers the CLI uses.
"""
from __future__ import annotations

from pathlib import Path

import re

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


def test_retry_clears_per_attempt_logs(project: Path, client: TestClient):
    """Stale `{role}-{attempt}.log` files trip orphan detection in the next
    tick after attempt counters are reset, so retry must wipe them too."""
    mas = project / ".mas"
    task_id = "20260424-stale-aaaa"
    tdir = _put_task(mas, "failed", task_id, role="orchestrator")
    (tdir / ".orchestrator_attempt").write_text("3")
    (tdir / ".previous_failure").write_text("prev")
    (tdir / "logs").mkdir()
    for i in (1, 2, 3):
        (tdir / "logs" / f"orchestrator-{i}.log").write_text(f"attempt {i}\n")
    child = tdir / "subtasks" / "impl-1"
    child.mkdir(parents=True)
    (child / ".attempt").write_text("3")
    (child / "logs").mkdir()
    (child / "logs" / "implementer-1.log").write_text("attempt 1\n")
    (child / "logs" / "implementer-2.log").write_text("attempt 2\n")

    r = client.post(f"/task/{task_id}/retry", follow_redirects=False)
    assert r.status_code == 303
    moved = mas / "tasks" / "doing" / task_id

    assert not list((moved / "logs").glob("*.log"))
    assert not (moved / ".orchestrator_attempt").exists()
    assert not list((moved / "subtasks" / "impl-1" / "logs").glob("*.log"))
    assert (moved / "subtasks" / "impl-1" / ".attempt").read_text().strip() == "1"


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


# ---------------------------------------------------------------------------
# Stats page (Acceptances #4, #5, #6)
# ---------------------------------------------------------------------------

def _put_task_with_result(
    mas: Path,
    column: str,
    task_id: str,
    role: str = "implementer",
    goal: str = "do a thing",
    cost_usd: float = 0.25,
    tokens_in: int = 100,
    tokens_out: int = 50,
    provider: str | None = None,
    duration_s: float | None = None,
    environment_error: bool = False,
) -> Path:
    inputs: dict = {}
    if provider is not None:
        inputs["provider"] = provider
    tdir = board.task_dir(mas, column, task_id)
    tdir.mkdir(parents=True)
    board.write_task(tdir, Task(id=task_id, role=role, goal=goal, inputs=inputs))
    if environment_error:
        status = "environment_error"
    elif column == "done":
        status = "success"
    else:
        status = "failure"
    result = Result(
        task_id=task_id,
        status=status,
        summary=f"result for {task_id}",
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        duration_s=duration_s,
    )
    (tdir / "result.json").write_text(result.model_dump_json())
    return tdir


class TestStatsPage:
    def test_stats_page_renders_board_counts_and_total_cost(
        self, project: Path, client: TestClient
    ):
        """GET /stats returns 200 with per-column task counts and total cost_usd."""
        mas = project / ".mas"
        _put_task_with_result(
            mas, "done", "20260427-stdone-aa00",
            goal="done work", cost_usd=0.25, tokens_in=100, tokens_out=50,
        )
        _put_task_with_result(
            mas, "failed", "20260427-stfail-bb00",
            role="evaluator", goal="failed work", cost_usd=0.25, tokens_in=200, tokens_out=100,
        )

        r = client.get("/stats")
        assert r.status_code == 200
        body = r.text
        # Per-column counts: board has 1 done and 1 failed
        assert "done" in body.lower()
        assert "failed" in body.lower()
        # Total cost_usd = 0.25 + 0.25 = 0.50; assert formatted value present
        assert "0.50" in body

    def test_stats_page_since_1h_returns_200(
        self, project: Path, client: TestClient
    ):
        """GET /stats?since=1h returns 200."""
        r = client.get("/stats?since=1h")
        assert r.status_code == 200

    def test_stats_page_since_garbage_returns_200_with_error_banner(
        self, project: Path, client: TestClient
    ):
        """GET /stats?since=garbage returns 200 with a visible error banner."""
        r = client.get("/stats?since=garbage")
        assert r.status_code == 200
        assert "Invalid since" in r.text

    def test_nav_has_stats_link(self, project: Path, client: TestClient):
        """The board (index) page nav contains a link to /stats with text 'Stats'."""
        r = client.get("/")
        assert r.status_code == 200
        body = r.text
        assert 'href="/stats"' in body
        assert "Stats" in body

    def test_stats_page_renders_all_metric_sections(
        self, project: Path, client: TestClient
    ):
        """GET /stats renders Outcome Rates, Role Durations, Provider Activity,
        and Environment Errors sections with real data from compute_stats()."""
        mas = project / ".mas"
        # done task: implementer role, claude_code provider, 10s duration
        _put_task_with_result(
            mas, "done", "20260427-stall-aaaa",
            role="implementer", goal="implement",
            cost_usd=0.10, tokens_in=100, tokens_out=50,
            provider="claude_code", duration_s=10.0,
        )
        # failed task: evaluator role, codex provider, 5s duration, environment_error
        _put_task_with_result(
            mas, "failed", "20260427-stall-bbbb",
            role="evaluator", goal="evaluate",
            cost_usd=0.05, tokens_in=50, tokens_out=20,
            provider="codex", duration_s=5.0,
            environment_error=True,
        )

        r = client.get("/stats")
        assert r.status_code == 200
        body = r.text

        # Section headings (h2) for the four missing metric groups
        assert "Outcome Rates" in body or "outcome" in body.lower(), \
            "Expected an Outcome Rates section heading"
        assert "Role" in body and ("Duration" in body or "duration" in body.lower()), \
            "Expected a Role Durations section heading"
        assert "Provider" in body, \
            "Expected a Provider Activity section heading"
        assert "Environment Error" in body or "env_error" in body.lower(), \
            "Expected an Environment Errors section heading"

        # Per-role row: implementer with timing data
        assert "implementer" in body, "Expected implementer role row"
        # Per-provider row
        assert "claude_code" in body or "codex" in body, \
            "Expected at least one provider row"
        # env_errors is 1 (the environment_error task) — anchored to the section
        assert re.search(r"Environment Error[s]?[\s\S]{0,300}1", body), \
            "Expected env_errors=1 rendered under the Environment Errors section"

        # Outcome rates formatted as percentages
        # success_rate = 1/2 = 50%, revision_rate = 0%
        assert "50.0%" in body or "50%" in body, \
            "Expected 50% success rate rendered as a percentage"
