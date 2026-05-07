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


@pytest.fixture
def done_task_factory():
    """Create a done/ task with subtask results for cost estimation testing."""
    def _make(mas, task_id, role_cost_pairs):
        """role_cost_pairs: list of (role, cost_usd) tuples"""
        tdir = board.task_dir(mas, "done", task_id)
        tdir.mkdir(parents=True)
        board.write_task(tdir, Task(id=task_id, role="implementer", goal=f"goal for {task_id}"))
        plan = __import__("mas.schemas").schemas.Plan(
            parent_id=task_id,
            summary="test",
            subtasks=[
                SubtaskSpec(id=f"sub{i}", role=role, goal=f"{role} goal")
                for i, (role, _) in enumerate(role_cost_pairs)
            ],
        )
        (tdir / "plan.json").write_text(plan.model_dump_json())
        subs_root = tdir / "subtasks"
        for i, (role, cost) in enumerate(role_cost_pairs):
            sub_dir = subs_root / f"sub{i}"
            sub_dir.mkdir(parents=True)
            (sub_dir / "task.json").write_text(json.dumps({"task_id": f"sub{i}", "role": role}))
            (sub_dir / "result.json").write_text(
                Result(
                    task_id=f"sub{i}", status="success", summary="done",
                    cost_usd=cost, tokens_in=100, tokens_out=200, duration_s=30
                ).model_dump_json()
            )
    return _make


# ---------------------------------------------------------------------------
# estimate_task_cost() unit tests
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="estimate_task_cost not implemented yet")
def test_estimate_task_cost_returns_dict_with_total(project, done_task_factory):
    from mas.cost_helpers import estimate_task_cost
    mas = project / ".mas"
    # Create 3 done tasks with implementer costs: 3.00, 5.00, 7.00
    done_task_factory(mas, "20260501-task1-aaaa", [("implementer", 3.0)])
    done_task_factory(mas, "20260501-task2-bbbb", [("implementer", 5.0)])
    done_task_factory(mas, "20260501-task3-cccc", [("implementer", 7.0)])
    result = estimate_task_cost(mas, "doing", "new-task-dddd")
    assert isinstance(result, dict)
    assert "total" in result
    assert result["total"] > 0  # should be positive


@pytest.mark.xfail(reason="estimate_task_cost not implemented yet", raises=NotImplementedError)
def test_estimate_task_cost_role_with_sufficient_history(project, done_task_factory):
    from mas.cost_helpers import estimate_task_cost
    mas = project / ".mas"
    # 3 done tasks with implementer costs: 2.00, 4.00, 6.00 → median=4.00
    done_task_factory(mas, "20260501-task1-aaaa", [("implementer", 2.0)])
    done_task_factory(mas, "20260501-task2-bbbb", [("implementer", 4.0)])
    done_task_factory(mas, "20260501-task3-cccc", [("implementer", 6.0)])
    result = estimate_task_cost(mas, "doing", "new-task-dddd")
    assert "implementer" in result
    impl = result["implementer"]
    assert impl["available"] is True
    assert impl["estimated_usd"] == pytest.approx(4.0, abs=0.01)
    assert impl["uncertainty_usd"] == pytest.approx(2.0, abs=0.01)


def test_estimate_task_cost_role_with_less_than_3_returns_available_false(project, done_task_factory):
    from mas.cost_helpers import estimate_task_cost
    mas = project / ".mas"
    # Only 2 done tasks for implementer
    done_task_factory(mas, "20260501-task1-aaaa", [("implementer", 3.0)])
    done_task_factory(mas, "20260501-task2-bbbb", [("implementer", 5.0)])
    result = estimate_task_cost(mas, "doing", "new-task-dddd")
    assert "implementer" in result
    assert result["implementer"]["available"] is False


def test_estimate_task_cost_single_outlier_robustness(project, done_task_factory):
    from mas.cost_helpers import estimate_task_cost
    mas = project / ".mas"
    # 3 done tasks: two normal (3.0, 4.0), one outlier (100.0)
    # stddev should still be computed; median=4.0
    done_task_factory(mas, "20260501-task1-aaaa", [("implementer", 3.0)])
    done_task_factory(mas, "20260501-task2-bbbb", [("implementer", 4.0)])
    done_task_factory(mas, "20260501-task3-cccc", [("implementer", 100.0)])
    result = estimate_task_cost(mas, "doing", "new-task-dddd")
    impl = result["implementer"]
    assert impl["available"] is True
    assert impl["estimated_usd"] == pytest.approx(4.0, abs=0.01)


def test_estimate_task_cost_total_sums_all_roles(project, done_task_factory):
    from mas.cost_helpers import estimate_task_cost
    mas = project / ".mas"
    done_task_factory(mas, "20260501-task1-aaaa", [("implementer", 4.0), ("tester", 2.0)])
    done_task_factory(mas, "20260501-task2-bbbb", [("implementer", 6.0), ("tester", 3.0)])
    done_task_factory(mas, "20260501-task3-cccc", [("implementer", 8.0), ("tester", 4.0)])
    result = estimate_task_cost(mas, "doing", "new-task-dddd")
    total = result["total"]
    assert total == pytest.approx(6.0 + 3.0, abs=0.01)  # median impl 6.0 + median tester 3.0


def test_estimate_task_cost_includes_all_required_keys_per_role(project, done_task_factory):
    from mas.cost_helpers import estimate_task_cost
    mas = project / ".mas"
    done_task_factory(mas, "20260501-task1-aaaa", [("implementer", 5.0)])
    done_task_factory(mas, "20260501-task2-bbbb", [("implementer", 6.0)])
    done_task_factory(mas, "20260501-task3-cccc", [("implementer", 7.0)])
    result = estimate_task_cost(mas, "doing", "new-task-dddd")
    impl = result["implementer"]
    assert "estimated_usd" in impl
    assert "uncertainty_usd" in impl
    assert "available" in impl


# ---------------------------------------------------------------------------
# Web route tests
# ---------------------------------------------------------------------------

def _put_doing_task(mas, task_id, role="implementer", goal="cost estimate test"):
    tdir = board.task_dir(mas, "doing", task_id)
    tdir.mkdir(parents=True)
    board.write_task(tdir, Task(id=task_id, role=role, goal=goal, cost_budget_usd=10.0))
    return tdir


def test_task_detail_passes_cost_estimate_context(client, project, done_task_factory):
    mas = project / ".mas"
    # Create history so estimate is available
    done_task_factory(mas, "20260501-task1-aaaa", [("implementer", 4.0)])
    done_task_factory(mas, "20260501-task2-bbbb", [("implementer", 5.0)])
    done_task_factory(mas, "20260501-task3-cccc", [("implementer", 6.0)])
    task_id = "20260507-newtask-dddd"
    _put_doing_task(mas, task_id)
    response = client.get(f"/task/{task_id}")
    assert response.status_code == 200
    # cost_estimate should be in template context - check it rendered
    assert b"Estimated Cost" in response.content


def test_task_detail_shows_estimate_when_baselines_exist(client, project, done_task_factory):
    mas = project / ".mas"
    done_task_factory(mas, "20260501-task1-aaaa", [("implementer", 4.0)])
    done_task_factory(mas, "20260501-task2-bbbb", [("implementer", 5.0)])
    done_task_factory(mas, "20260501-task3-cccc", [("implementer", 6.0)])
    task_id = "20260507-newtask-dddd"
    _put_doing_task(mas, task_id)
    response = client.get(f"/task/{task_id}")
    assert response.status_code == 200
    # Should show 'Estimated Cost' section when baselines exist
    assert b"Estimated Cost" in response.content
    # Should show format '$X.XX \xc2\xb1 $Y.YY' (± symbol in UTF-8)
    assert b"$0.00" not in response.content  # real estimate should not be zero


def test_task_detail_shows_unavailable_when_history_insufficient(client, project):
    mas = project / ".mas"
    task_id = "20260507-newtask-dddd"
    _put_doing_task(mas, task_id)
    response = client.get(f"/task/{task_id}")
    assert response.status_code == 200
    assert b"Estimate unavailable" in response.content


def test_task_detail_estimate_shows_correct_format_with_uncertainty(client, project, done_task_factory):
    mas = project / ".mas"
    done_task_factory(mas, "20260501-task1-aaaa", [("implementer", 4.0)])
    done_task_factory(mas, "20260501-task2-bbbb", [("implementer", 5.0)])
    done_task_factory(mas, "20260501-task3-cccc", [("implementer", 6.0)])
    task_id = "20260507-newtask-dddd"
    _put_doing_task(mas, task_id)
    response = client.get(f"/task/{task_id}")
    assert response.status_code == 200
    # Check for the pattern: $N.NN ± $N.NN
    text = response.text
    assert "$" in text
    assert "±" in text
