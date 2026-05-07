import json
import os
import yaml
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.mas.cost_helpers import compute_burn_rate, forecast_exhaustion_days
from src.mas.schemas import MasConfig, ProviderConfig, RoleConfig
from src.mas.web.app import create_app


def create_audit_entry(audit_path, timestamp, cost):
    entry = {
        "timestamp": timestamp.isoformat(),
        "event": "subtask_complete",
        "details": {"cost_usd": cost}
    }
    with open(audit_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def setup_board_root(tmp_path, task_entries):
    board_root = tmp_path / "board"
    board_root.mkdir()
    tasks_dir = board_root / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "done").mkdir()
    (tasks_dir / "doing").mkdir()

    for task_id, is_done, timestamp, cost in task_entries:
        task_dir = tasks_dir / ("done" if is_done else "doing") / task_id
        task_dir.mkdir(parents=True)
        audit_file = task_dir / "audit.jsonl"
        create_audit_entry(audit_file, timestamp, cost)
    return board_root


from fastapi.testclient import TestClient

## compute_burn_rate tests
def test_compute_burn_rate_normal_case(tmp_path):
    entries = []
    total = 0.0
    for day in range(10):
        task_id = f"task_{day}"
        timestamp = datetime(2026, 4, 28) + timedelta(days=day)
        cost = 10.0
        total += cost
        entries.append((task_id, True, timestamp, cost))
    board_root = setup_board_root(tmp_path, entries)
    result = compute_burn_rate(str(board_root))
    assert result["daily_rate"] == pytest.approx(total / 10)
    assert result["total_spent"] == pytest.approx(total)
    assert result["days_of_data"] == 10
    assert len(result["data_points"]) == 10
    for dp in result["data_points"]:
        assert "date" in dp and "cost" in dp
        assert isinstance(dp["date"], str) and isinstance(dp["cost"], float)


def test_compute_burn_rate_sparse_data(tmp_path):
    entries = [
        ("task1", True, datetime(2026, 4, 28), 10.0),
        ("task2", True, datetime(2026, 5, 3), 20.0),
    ]
    board_root = setup_board_root(tmp_path, entries)
    result = compute_burn_rate(str(board_root))
    assert result["daily_rate"] == pytest.approx(30.0 / 6)
    assert result["days_of_data"] == 6
    assert result["total_spent"] == pytest.approx(30.0)


def test_compute_burn_rate_zero_spend(tmp_path):
    board_root = tmp_path / "board"
    board_root.mkdir()
    tasks_dir = board_root / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "done").mkdir()
    (tasks_dir / "doing").mkdir()
    result = compute_burn_rate(str(board_root))
    assert result["daily_rate"] == 0.0 and result["total_spent"] == 0.0 and result["days_of_data"] == 0

    entries = [("task1", True, datetime(2026, 5, 7), 0.0)]
    board_root2 = setup_board_root(tmp_path, entries)
    result2 = compute_burn_rate(str(board_root2))
    assert result2["daily_rate"] == 0.0


def test_compute_burn_rate_less_than_7_days(tmp_path):
    entries = [
        ("task1", True, datetime(2026, 5, 1), 10.0),
        ("task2", True, datetime(2026, 5, 3), 20.0),
    ]
    board_root = setup_board_root(tmp_path, entries)
    result = compute_burn_rate(str(board_root))
    assert result["days_of_data"] == 3
    assert result["daily_rate"] == pytest.approx(30.0 / 3)


def test_compute_burn_rate_no_tasks(tmp_path):
    board_root = tmp_path / "board"
    board_root.mkdir()
    tasks_dir = board_root / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "done").mkdir()
    (tasks_dir / "doing").mkdir()
    result = compute_burn_rate(str(board_root))
    assert result["daily_rate"] == 0.0 and result["total_spent"] == 0.0 and result["days_of_data"] == 0


## forecast_exhaustion_days tests
def test_forecast_exhaustion_days_normal():
    assert forecast_exhaustion_days(10.0, 100.0, 30.0) == pytest.approx(7.0)


def test_forecast_exhaustion_days_zero_burn_rate():
    assert forecast_exhaustion_days(0.0, 100.0, 30.0) is None


def test_forecast_exhaustion_days_budget_exceeded():
    assert forecast_exhaustion_days(10.0, 100.0, 100.0) == pytest.approx(0.0)
    assert forecast_exhaustion_days(10.0, 100.0, 110.0) == pytest.approx(0.0)


def test_forecast_exhaustion_days_large_projection():
    assert forecast_exhaustion_days(1.0, 500.0, 0.0) == pytest.approx(500.0)


## MasConfig tests
def test_masconfig_forecast_field_exists():
    config = MasConfig(providers={"dummy": ProviderConfig(cli="x")}, roles={"proposer": RoleConfig(provider="x", model="x")})
    assert hasattr(config, "forecast_warning_days_threshold")
    assert config.forecast_warning_days_threshold == 7
    assert isinstance(config.forecast_warning_days_threshold, int)


def test_masconfig_forecast_valid_positive():
    config = MasConfig(
        providers={"dummy": ProviderConfig(cli="x")},
        roles={"proposer": RoleConfig(provider="x", model="x")},
        forecast_warning_days_threshold=14,
    )
    assert config.forecast_warning_days_threshold == 14


def test_masconfig_forecast_rejects_negative():
    with pytest.raises(ValidationError):
        MasConfig(
            providers={"dummy": ProviderConfig(cli="x")},
            roles={"proposer": RoleConfig(provider="x", model="x")},
            forecast_warning_days_threshold=-1,
        )


## Web dashboard tests
def test_web_banner_under_threshold(tmp_path):
    mas_dir = tmp_path / ".mas"
    mas_dir.mkdir()
    with open(mas_dir / "config.yaml", "w") as f:
        yaml.dump({"default_cost_budget_usd": 100.0, "forecast_warning_days_threshold": 14, "providers": {"dummy": {"cli": "x"}}, "roles": {"dummy": {"provider": "x", "model": "x"}}}, f)

    board_root = tmp_path / "board"
    board_root.mkdir()
    tasks_dir = board_root / "tasks" / "done"
    tasks_dir.mkdir(parents=True)
    task_dir = tasks_dir / "task1"
    task_dir.mkdir()
    with open(task_dir / "audit.jsonl", "w") as f:
        f.write(json.dumps({"timestamp": datetime(2026, 5, 7).isoformat(), "event": "subtask_complete", "details": {"cost_usd": 30.0}}) + "\n")

    app = create_app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/stats")
        html = response.text
        assert "forecast-warning" in html
        assert "7 days of budget remaining at current burn rate ($10.00/day)" in html


def test_web_banner_over_threshold(tmp_path):
    mas_dir = tmp_path / ".mas"
    mas_dir.mkdir()
    with open(mas_dir / "config.yaml", "w") as f:
        yaml.dump({"default_cost_budget_usd": 100.0, "forecast_warning_days_threshold": 3, "providers": {"dummy": {"cli": "x"}}, "roles": {"dummy": {"provider": "x", "model": "x"}}}, f)

    app = create_app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/stats")
        html = response.text
        assert "forecast-warning" not in html


def test_web_banner_no_budget_set(tmp_path):
    mas_dir = tmp_path / ".mas"
    mas_dir.mkdir()
    with open(mas_dir / "config.yaml", "w") as f:
        yaml.dump({"forecast_warning_days_threshold": 7, "providers": {"dummy": {"cli": "x"}}, "roles": {"dummy": {"provider": "x", "model": "x"}}}, f)

    app = create_app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/stats")
        html = response.text
        assert "forecast-warning" not in html


def test_web_banner_zero_burn_rate(tmp_path):
    mas_dir = tmp_path / ".mas"
    mas_dir.mkdir()
    with open(mas_dir / "config.yaml", "w") as f:
        yaml.dump({"default_cost_budget_usd": 100.0, "forecast_warning_days_threshold": 14, "providers": {"dummy": {"cli": "x"}}, "roles": {"dummy": {"provider": "x", "model": "x"}}}, f)

    app = create_app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/stats")
        html = response.text
        assert "forecast-warning" not in html
