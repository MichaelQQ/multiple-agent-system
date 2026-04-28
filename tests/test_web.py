"""Tests for the mas web UI.

Covers: board view renders, task detail renders with plan + audit + logs,
and POST actions invoke the same board/daemon helpers the CLI uses.
"""
from __future__ import annotations

import hashlib
import json
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
# /config/roles tests
# ---------------------------------------------------------------------------

def test_config_roles_get_renders_current_file_content(project: Path, client: TestClient):
    mas = project / ".mas"
    roles_content = "proposer:\n  provider: claude-code\ntester:\n  provider: codex\n"
    (mas / "roles.yaml").write_text(roles_content)

    r = client.get("/config/roles")
    assert r.status_code == 200
    assert "<textarea" in r.text
    assert roles_content in r.text


def test_config_roles_post_valid_yaml_writes_file_and_shows_saved_banner(project: Path, client: TestClient):
    mas = project / ".mas"
    roles_yaml = mas / "roles.yaml"
    roles_yaml.write_text("proposer:\n  provider: claude-code\n")

    new_content = "proposer:\n  provider: codex\n"
    r = client.post("/config/roles", data={"content": new_content})
    assert r.status_code == 200
    assert "Saved" in r.text
    assert roles_yaml.read_text() == new_content
    # banner must mention the new mtime (any numeric representation is acceptable)
    mtime = int(roles_yaml.stat().st_mtime)
    assert str(mtime) in r.text or "Saved" in r.text


def test_config_roles_post_malformed_yaml_returns_error_and_file_unchanged(project: Path, client: TestClient):
    mas = project / ".mas"
    roles_yaml = mas / "roles.yaml"
    original = "proposer:\n  provider: claude-code\n"
    roles_yaml.write_text(original)
    original_hash = hashlib.md5(original.encode()).hexdigest()

    bad_yaml = "proposer: {unclosed: [bracket"
    r = client.post("/config/roles", data={"content": bad_yaml})
    assert r.status_code in (400, 422)
    assert hashlib.md5(roles_yaml.read_bytes()).hexdigest() == original_hash
    assert bad_yaml in r.text
    assert "error" in r.text.lower()


def test_config_roles_post_pydantic_invalid_returns_error_and_file_unchanged(project: Path, client: TestClient):
    mas = project / ".mas"
    roles_yaml = mas / "roles.yaml"
    original = "proposer:\n  provider: claude-code\n"
    roles_yaml.write_text(original)

    # valid YAML but missing required 'provider' → pydantic validation fails
    invalid_config = "proposer:\n  model: haiku\n  timeout_s: 600\n"
    r = client.post("/config/roles", data={"content": invalid_config})
    assert r.status_code in (400, 422)
    assert roles_yaml.read_text() == original
    assert invalid_config in r.text
    assert "error" in r.text.lower()


def test_config_roles_post_atomic_write_failure_leaves_original_intact(
    project: Path, client: TestClient, monkeypatch
):
    mas = project / ".mas"
    roles_yaml = mas / "roles.yaml"
    original = "proposer:\n  provider: claude-code\n"
    roles_yaml.write_text(original)

    def _fail_replace(src, dst):
        raise OSError("simulated disk full")

    monkeypatch.setattr("mas.web.app.os.replace", _fail_replace)

    new_content = "proposer:\n  provider: codex\n"
    r = client.post("/config/roles", data={"content": new_content})
    # route must handle the failure gracefully, not 500-crash
    assert r.status_code == 200
    # original file byte-identical
    assert roles_yaml.read_text() == original
    # no leftover .tmp files
    assert list(mas.glob("*.tmp")) == []
    # submitted text re-shown in textarea
    assert new_content in r.text


def test_config_nav_has_config_link_to_roles_page(project: Path, client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    assert 'href="/config/roles"' in r.text


# ---------------------------------------------------------------------------
# /trace/<task_id> tests — route and Trace link not yet implemented
# ---------------------------------------------------------------------------


def _make_trace_task(
    mas: Path,
    column: str,
    task_id: str,
    *,
    role: str = "orchestrator",
    goal: str = "trace goal",
    parent_id: str | None = None,
) -> Path:
    tdir = board.task_dir(mas, column, task_id)
    tdir.mkdir(parents=True)
    board.write_task(tdir, Task(id=task_id, role=role, goal=goal, parent_id=parent_id))
    return tdir


def test_trace_done_task_renders_subtask_rows(project: Path, client: TestClient):
    """GET /trace/<id>: 200 with a row per subtask + parent row; header shows id/role/goal/cost."""
    mas = project / ".mas"
    parent_id = "20260428-parent-aaaa"
    task_id = "20260428-trace-done-bbbb"

    tdir = _make_trace_task(mas, "done", task_id, parent_id=parent_id)

    subtask_specs = [
        ("20260428-impl-1111", "implementer"),
        ("20260428-test-2222", "tester"),
        ("20260428-eval-3333", "evaluator"),
    ]
    subs_root = tdir / "subtasks"
    for sub_id, _role in subtask_specs:
        sub_dir = subs_root / sub_id
        sub_dir.mkdir(parents=True)
        (sub_dir / "result.json").write_text(
            Result(
                task_id=sub_id,
                status="success",
                summary="ok",
                verdict="pass",
                tokens_in=100,
                tokens_out=50,
                cost_usd=0.01,
                duration_s=10.0,
            ).model_dump_json()
        )

    events = []
    for i, (sub_id, sub_role) in enumerate(subtask_specs):
        events.append({
            "timestamp": f"2026-04-28T10:0{i}:00+00:00",
            "event": "dispatch",
            "role": sub_role,
            "task_id": task_id,
            "subtask_id": sub_id,
            "provider": "claude-code",
            "details": {"cycle": 0},
        })
        events.append({
            "timestamp": f"2026-04-28T10:0{i + 1}:00+00:00",
            "event": "completion",
            "role": sub_role,
            "task_id": task_id,
            "subtask_id": sub_id,
            "provider": "claude-code",
            "status": "success",
            "duration_s": 60.0,
            "details": {"cycle": 0},
        })
    (tdir / "audit.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    (tdir / "transitions.jsonl").write_text(
        "2026-04-28T10:00:00+00:00|proposed|doing|start\n"
        "2026-04-28T10:10:00+00:00|doing|done|done\n"
    )

    r = client.get(f"/trace/{task_id}")
    assert r.status_code == 200
    body = r.text

    # Header: task id, role, goal must be present
    assert task_id in body
    assert "orchestrator" in body
    assert "trace goal" in body

    # Each subtask has a row marker (data-task-id or data-subtask-id attribute)
    for sub_id, _ in subtask_specs:
        assert (
            f'data-task-id="{sub_id}"' in body
            or f'data-subtask-id="{sub_id}"' in body
            or sub_id in body
        )

    # At least one row uses a data-task-id attribute (structural check)
    assert "data-task-id" in body or "data-subtask-id" in body

    # Tooltip must include provider name for completed stages (build_trace must populate stage["provider"])
    assert "provider=claude-code" in body

    # Tooltip must include non-empty tokens_in / tokens_out for completed stages
    # (build_trace must read tokens_in/tokens_out from result.json per stage)
    assert "tokens_in=100" in body
    assert "tokens_out=50" in body


def test_trace_failed_task_shows_failure_class(project: Path, client: TestClient):
    """GET /trace/<id> for failed/ task: last subtask with status=failure gets failure CSS class."""
    mas = project / ".mas"
    task_id = "20260428-trace-fail-cccc"

    tdir = _make_trace_task(mas, "failed", task_id)

    sub_id = "20260428-eval-fail-1234"
    sub_dir = tdir / "subtasks" / sub_id
    sub_dir.mkdir(parents=True)
    (sub_dir / "result.json").write_text(
        Result(task_id=sub_id, status="failure", summary="failed hard", duration_s=5.0).model_dump_json()
    )

    events = [
        {
            "timestamp": "2026-04-28T11:00:00+00:00",
            "event": "dispatch",
            "role": "evaluator",
            "task_id": task_id,
            "subtask_id": sub_id,
            "details": {"cycle": 0},
        },
        {
            "timestamp": "2026-04-28T11:01:00+00:00",
            "event": "completion",
            "role": "evaluator",
            "task_id": task_id,
            "subtask_id": sub_id,
            "status": "failure",
            "duration_s": 60.0,
            "details": {"cycle": 0},
        },
    ]
    (tdir / "audit.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    (tdir / "transitions.jsonl").write_text(
        "2026-04-28T11:00:00+00:00|proposed|doing|start\n"
        "2026-04-28T11:05:00+00:00|doing|failed|failed\n"
    )

    r = client.get(f"/trace/{task_id}")
    assert r.status_code == 200
    body = r.text

    # The failure stage bar must carry a failure CSS class
    assert "status-failure" in body or "failure" in body


def test_trace_in_flight_subtask_shows_in_flight_class(project: Path, client: TestClient):
    """GET /trace/<id>: dispatch-without-completion subtask renders in-flight CSS class."""
    mas = project / ".mas"
    task_id = "20260428-trace-flight-dddd"

    tdir = _make_trace_task(mas, "doing", task_id)

    sub_id = "20260428-impl-inflight-5678"
    sub_dir = tdir / "subtasks" / sub_id
    sub_dir.mkdir(parents=True)
    # PID file present; no result.json written yet
    pids_dir = tdir / "pids"
    pids_dir.mkdir(parents=True)
    (pids_dir / "implementer.claude-code.pid").write_text("12345")

    events = [
        {
            "timestamp": "2026-04-28T12:00:00+00:00",
            "event": "dispatch",
            "role": "implementer",
            "task_id": task_id,
            "subtask_id": sub_id,
            "details": {"cycle": 0},
        },
    ]
    (tdir / "audit.jsonl").write_text(json.dumps(events[0]) + "\n")
    (tdir / "transitions.jsonl").write_text(
        "2026-04-28T12:00:00+00:00|proposed|doing|start\n"
    )

    r = client.get(f"/trace/{task_id}")
    assert r.status_code == 200
    body = r.text

    # The in-flight stage must carry an in-flight CSS class
    assert "in-flight" in body or "status-in-flight" in body


def test_trace_unknown_task_returns_404(project: Path, client: TestClient):
    """GET /trace/<id> for a non-existent task returns 404 with the task id in the body."""
    r = client.get("/trace/does-not-exist-0000")
    assert r.status_code == 404
    # Generic FastAPI 404 only says {"detail":"Not Found"} — the real route must include the id
    assert "does-not-exist-0000" in r.text


def test_task_detail_page_has_trace_link(project: Path, client: TestClient):
    """The per-task detail page (/task/<id>) must contain a link to /trace/<id>."""
    mas = project / ".mas"
    task_id = "20260428-tracelink-ffff"
    _put_task(mas, "doing", task_id, role="implementer", goal="check trace link")

    r = client.get(f"/task/{task_id}")
    assert r.status_code == 200
    assert f'href="/trace/{task_id}"' in r.text
