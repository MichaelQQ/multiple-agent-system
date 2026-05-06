import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.mas.cost_helpers import aggregate_costs_by_role, at_risk_tasks
import pytest

@pytest.fixture
def task_dir_with_subtasks(tmp_path):
    task_dir = tmp_path / "task1"
    task_dir.mkdir()
    subtasks_dir = task_dir / "subtasks"
    subtasks_dir.mkdir()

    sub1_dir = subtasks_dir / "sub1"
    sub1_dir.mkdir()
    (sub1_dir / "task.json").write_text(json.dumps({"task_id": "sub1", "role": "implementer"}))
    (sub1_dir / "result.json").write_text(json.dumps({
        "task_id": "sub1", "status": "success", "summary": "done",
        "cost_usd": 1.5, "tokens_in": 100, "tokens_out": 200, "duration_s": 30
    }))

    sub2_dir = subtasks_dir / "sub2"
    sub2_dir.mkdir()
    (sub2_dir / "task.json").write_text(json.dumps({"task_id": "sub2", "role": "tester"}))
    (sub2_dir / "result.json").write_text(json.dumps({
        "task_id": "sub2", "status": "success", "summary": "done",
        "cost_usd": 0.5, "tokens_in": 50, "tokens_out": 100, "duration_s": 15
    }))

    return task_dir

@pytest.fixture
def board_root_with_tasks(tmp_path):
    board = tmp_path / "board"
    doing_dir = board / "doing"
    doing_dir.mkdir(parents=True)

    task1_dir = doing_dir / "20260505-task1-aaaa"
    task1_dir.mkdir()
    (task1_dir / "task.json").write_text(json.dumps({"task_id": "20260505-task1-aaaa", "cost_budget_usd": 10.0}))
    subtasks_dir1 = task1_dir / "subtasks"
    subtasks_dir1.mkdir()
    for sub_id, cost in [("sub1a", 5.0), ("sub1b", 4.0)]:
        sub_dir = subtasks_dir1 / sub_id
        sub_dir.mkdir()
        (sub_dir / "task.json").write_text(json.dumps({"role": "implementer" if sub_id == "sub1a" else "tester", "task_id": sub_id}))
        (sub_dir / "result.json").write_text(json.dumps({
            "task_id": sub_id, "status": "success", "cost_usd": cost,
            "tokens_in": 100, "tokens_out": 200, "duration_s": 30
        }))

    task2_dir = doing_dir / "20260505-task2-bbbb"
    task2_dir.mkdir()
    (task2_dir / "task.json").write_text(json.dumps({"task_id": "20260505-task2-bbbb", "cost_budget_usd": 10.0}))
    subtasks_dir2 = task2_dir / "subtasks"
    subtasks_dir2.mkdir()
    sub_dir = subtasks_dir2 / "sub2a"
    sub_dir.mkdir()
    (sub_dir / "task.json").write_text(json.dumps({"role": "implementer", "task_id": "sub2a"}))
    (sub_dir / "result.json").write_text(json.dumps({
        "task_id": "sub2a", "status": "success", "cost_usd": 5.0,
        "tokens_in": 100, "tokens_out": 200, "duration_s": 30
    }))

    return board

def test_aggregate_costs_by_role_groups_by_role(task_dir_with_subtasks):
    result = aggregate_costs_by_role(task_dir_with_subtasks)
    assert "implementer" in result
    assert "tester" in result

def test_aggregate_costs_by_role_counts_subtasks(task_dir_with_subtasks):
    result = aggregate_costs_by_role(task_dir_with_subtasks)
    assert result["implementer"]["count"] == 1
    assert result["tester"]["count"] == 1

def test_aggregate_costs_by_role_sums_costs(task_dir_with_subtasks):
    result = aggregate_costs_by_role(task_dir_with_subtasks)
    assert result["implementer"]["cost_usd"] == 1.5
    assert result["tester"]["cost_usd"] == 0.5

def test_aggregate_costs_by_role_sums_tokens(task_dir_with_subtasks):
    result = aggregate_costs_by_role(task_dir_with_subtasks)
    assert result["implementer"]["tokens_in"] == 100
    assert result["implementer"]["tokens_out"] == 200

def test_aggregate_costs_by_role_empty_when_no_subtasks(tmp_path):
    task_dir = tmp_path / "empty_task"
    task_dir.mkdir()
    assert aggregate_costs_by_role(task_dir) == {}

def test_at_risk_tasks_flags_over_budget_tasks(board_root_with_tasks):
    result = at_risk_tasks(board_root_with_tasks)
    assert "20260505-task1-aaaa" in result
    assert "20260505-task2-bbbb" not in result

def test_at_risk_tasks_uses_custom_threshold(board_root_with_tasks):
    result = at_risk_tasks(board_root_with_tasks, threshold=0.9)
    assert "20260505-task1-aaaa" not in result

def test_at_risk_tasks_empty_when_no_budget_set(tmp_path):
    board = tmp_path / "board"
    doing_dir = board / "doing"
    doing_dir.mkdir(parents=True)
    task_dir = doing_dir / "task3"
    task_dir.mkdir()
    (task_dir / "task.json").write_text(json.dumps({"task_id": "task3"}))
    assert at_risk_tasks(board) == []

def test_at_risk_tasks_empty_when_no_tasks(tmp_path):
    board = tmp_path / "board"
    board.mkdir()
    assert at_risk_tasks(board) == []
