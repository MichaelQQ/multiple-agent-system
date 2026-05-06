import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from mas import board
from mas.schemas import Result, SubtaskSpec, Task
from mas.web.app import create_app

@pytest.fixture
def project(tmp_path):
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    return tmp_path

@pytest.fixture
def client(project):
    app = create_app(project)
    return TestClient(app)

def _put_task(mas, column, task_id, role="implementer", goal="cost test"):
    tdir = board.task_dir(mas, column, task_id)
    tdir.mkdir(parents=True)
    board.write_task(tdir, Task(id=task_id, role=role, goal=goal, cost_budget_usd=10.0))
    return tdir

def test_get_costs_returns_json(client, project):
    mas = project / ".mas"
    task_id = "20260505-task1-aaaa"
    tdir = _put_task(mas, "doing", task_id)
    plan = __import__("mas.schemas").schemas.Plan(
        parent_id=task_id,
        summary="test",
        subtasks=[
            SubtaskSpec(id="sub1", role="implementer", goal="impl"),
            SubtaskSpec(id="sub2", role="tester", goal="test"),
        ],
    )
    (tdir / "plan.json").write_text(plan.model_dump_json())
    for sub_id, cost in [("sub1", 5.0), ("sub2", 4.0)]:
        sub_dir = tdir / "subtasks" / sub_id
        sub_dir.mkdir(parents=True)
        (sub_dir / "task.json").write_text(json.dumps({"task_id": sub_id, "role": "implementer" if sub_id == "sub1" else "tester"}))
        (sub_dir / "result.json").write_text(
            Result(
                task_id=sub_id, status="success", summary="done",
                cost_usd=cost, tokens_in=100, tokens_out=200, duration_s=30
            ).model_dump_json()
        )

    response = client.get("/costs")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"

def test_costs_json_has_roles_and_total(client, project):
    mas = project / ".mas"
    task_id = "20260505-task1-aaaa"
    tdir = _put_task(mas, "doing", task_id)
    plan = __import__("mas.schemas").schemas.Plan(
        parent_id=task_id,
        summary="test",
        subtasks=[
            SubtaskSpec(id="sub1", role="implementer", goal="impl"),
        ],
    )
    (tdir / "plan.json").write_text(plan.model_dump_json())
    sub_dir = tdir / "subtasks" / "sub1"
    sub_dir.mkdir(parents=True)
    (sub_dir / "task.json").write_text(json.dumps({"task_id": "sub1", "role": "implementer"}))
    (sub_dir / "result.json").write_text(
        Result(
            task_id="sub1", status="success", summary="done",
            cost_usd=5.0, tokens_in=100, tokens_out=200, duration_s=30
        ).model_dump_json()
    )

    response = client.get("/costs")
    data = response.json()
    assert "roles" in data
    assert "total" in data

def test_costs_roles_have_required_fields(client, project):
    mas = project / ".mas"
    task_id = "20260505-task1-aaaa"
    tdir = _put_task(mas, "doing", task_id)
    plan = __import__("mas.schemas").schemas.Plan(
        parent_id=task_id,
        summary="test",
        subtasks=[
            SubtaskSpec(id="sub1", role="implementer", goal="impl"),
        ],
    )
    (tdir / "plan.json").write_text(plan.model_dump_json())
    sub_dir = tdir / "subtasks" / "sub1"
    sub_dir.mkdir(parents=True)
    (sub_dir / "task.json").write_text(json.dumps({"task_id": "sub1", "role": "implementer"}))
    (sub_dir / "result.json").write_text(
        Result(
            task_id="sub1", status="success", summary="done",
            cost_usd=5.0, tokens_in=100, tokens_out=200, duration_s=30
        ).model_dump_json()
    )

    response = client.get("/costs")
    data = response.json()
    assert len(data["roles"]) > 0
    for role, info in data["roles"].items():
        assert "tokens_in" in info
        assert "tokens_out" in info
        assert "cost_usd" in info
        assert "count" in info

def test_costs_at_risk_returns_json(client, project):
    mas = project / ".mas"
    task_id = "20260505-task1-aaaa"
    tdir = _put_task(mas, "doing", task_id)
    plan = __import__("mas.schemas").schemas.Plan(
        parent_id=task_id,
        summary="test",
        subtasks=[
            SubtaskSpec(id="sub1", role="implementer", goal="impl"),
            SubtaskSpec(id="sub2", role="tester", goal="test"),
        ],
    )
    (tdir / "plan.json").write_text(plan.model_dump_json())
    for sub_id, cost in [("sub1", 5.0), ("sub2", 4.0)]:
        sub_dir = tdir / "subtasks" / sub_id
        sub_dir.mkdir(parents=True)
        (sub_dir / "task.json").write_text(json.dumps({"task_id": sub_id, "role": "implementer" if sub_id == "sub1" else "tester"}))
        (sub_dir / "result.json").write_text(
            Result(
                task_id=sub_id, status="success", summary="done",
                cost_usd=cost, tokens_in=100, tokens_out=200, duration_s=30
            ).model_dump_json()
        )

    response = client.get("/costs/at-risk")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"

def test_costs_at_risk_returns_task_ids(client, project):
    mas = project / ".mas"
    task_id = "20260505-task1-aaaa"
    tdir = _put_task(mas, "doing", task_id)
    plan = __import__("mas.schemas").schemas.Plan(
        parent_id=task_id,
        summary="test",
        subtasks=[
            SubtaskSpec(id="sub1", role="implementer", goal="impl"),
            SubtaskSpec(id="sub2", role="tester", goal="test"),
        ],
    )
    (tdir / "plan.json").write_text(plan.model_dump_json())
    for sub_id, cost in [("sub1", 5.0), ("sub2", 4.0)]:
        sub_dir = tdir / "subtasks" / sub_id
        sub_dir.mkdir(parents=True)
        (sub_dir / "task.json").write_text(json.dumps({"task_id": sub_id, "role": "implementer" if sub_id == "sub1" else "tester"}))
        (sub_dir / "result.json").write_text(
            Result(
                task_id=sub_id, status="success", summary="done",
                cost_usd=cost, tokens_in=100, tokens_out=200, duration_s=30
            ).model_dump_json()
        )

    response = client.get("/costs/at-risk")
    data = response.json()
    assert task_id in data

def test_task_detail_has_cost_by_role_section(client, project):
    mas = project / ".mas"
    task_id = "20260505-task1-aaaa"
    tdir = _put_task(mas, "doing", task_id)
    plan = __import__("mas.schemas").schemas.Plan(
        parent_id=task_id,
        summary="test",
        subtasks=[
            SubtaskSpec(id="sub1", role="implementer", goal="impl"),
        ],
    )
    (tdir / "plan.json").write_text(plan.model_dump_json())
    sub_dir = tdir / "subtasks" / "sub1"
    sub_dir.mkdir(parents=True)
    (sub_dir / "task.json").write_text(json.dumps({"task_id": "sub1", "role": "implementer"}))
    (sub_dir / "result.json").write_text(
        Result(
            task_id="sub1", status="success", summary="done",
            cost_usd=5.0, tokens_in=100, tokens_out=200, duration_s=30
        ).model_dump_json()
    )

    response = client.get(f"/task/{task_id}")
    assert response.status_code == 200
    assert b"Cost by Role" in response.content

def test_task_detail_has_subtask_cost_table(client, project):
    mas = project / ".mas"
    task_id = "20260505-task1-aaaa"
    tdir = _put_task(mas, "doing", task_id)
    plan = __import__("mas.schemas").schemas.Plan(
        parent_id=task_id,
        summary="test",
        subtasks=[
            SubtaskSpec(id="sub1", role="implementer", goal="impl"),
        ],
    )
    (tdir / "plan.json").write_text(plan.model_dump_json())
    sub_dir = tdir / "subtasks" / "sub1"
    sub_dir.mkdir(parents=True)
    (sub_dir / "task.json").write_text(json.dumps({"task_id": "sub1", "role": "implementer"}))
    (sub_dir / "result.json").write_text(
        Result(
            task_id="sub1", status="success", summary="done",
            cost_usd=5.0, tokens_in=100, tokens_out=200, duration_s=30
        ).model_dump_json()
    )

    response = client.get(f"/task/{task_id}")
    assert b"Tokens In" in response.content
    assert b"Tokens Out" in response.content
    assert b"Duration" in response.content
    assert b"Cost" in response.content
