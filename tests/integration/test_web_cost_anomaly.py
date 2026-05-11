import json
import pytest
from fastapi.testclient import TestClient
from mas.cost_helpers import compute_role_baselines, detect_anomalies


@pytest.fixture
def client_with_anomalies(tmp_path):
    from mas.web.app import create_app

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    mas_dir = project_dir / ".mas"
    mas_dir.mkdir()
    done_dir = mas_dir / "tasks" / "done"
    done_dir.mkdir(parents=True)

    for i in range(3):
        task_id = f"20260506-baseline-{i:04d}"
        task_dir = done_dir / task_id
        task_dir.mkdir()
        task_data = {"id": task_id, "role": "implementer", "goal": "baseline task"}
        (task_dir / "task.json").write_text(json.dumps(task_data))
        result_data = {"task_id": task_id, "status": "success", "summary": "done", "cost_usd": 1.0}
        (task_dir / "result.json").write_text(json.dumps(result_data))

    anomalous_id = "20260506-anomalous-task-1234"
    anomalous_dir = done_dir / anomalous_id
    anomalous_dir.mkdir()
    task_data = {"id": anomalous_id, "role": "implementer", "goal": "anomalous task"}
    (anomalous_dir / "task.json").write_text(json.dumps(task_data))
    result_data = {"task_id": anomalous_id, "status": "success", "summary": "done", "cost_usd": 3.0}
    (anomalous_dir / "result.json").write_text(json.dumps(result_data))

    app = create_app(project=project_dir)
    return TestClient(app)


@pytest.fixture
def client_no_anomalies(tmp_path):
    from mas.web.app import create_app

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    mas_dir = project_dir / ".mas"
    mas_dir.mkdir()
    done_dir = mas_dir / "tasks" / "done"
    done_dir.mkdir(parents=True)

    for i in range(3):
        task_id = f"20260506-normal-{i:04d}"
        task_dir = done_dir / task_id
        task_dir.mkdir()
        task_data = {"id": task_id, "role": "implementer", "goal": "normal task"}
        (task_dir / "task.json").write_text(json.dumps(task_data))
        result_data = {"task_id": task_id, "status": "success", "summary": "done", "cost_usd": 1.0}
        (task_dir / "result.json").write_text(json.dumps(result_data))

    app = create_app(project=project_dir)
    return TestClient(app)


def test_stats_page_anomaly_section(client_with_anomalies):
    resp = client_with_anomalies.get("/stats")
    assert resp.status_code == 200
    html = resp.text
    # Look for a specific Cost Anomalies section heading
    # This will fail because the section isn't implemented yet
    assert "Cost Anomalies Section" in html or "anomaly-detection" in html.lower()


def test_task_page_anomaly_badge(client_with_anomalies):
    resp = client_with_anomalies.get("/task/20260506-anomalous-task-1234")
    assert resp.status_code == 200
    html = resp.text
    # Look for a specific anomaly indicator (CSS class, badge, etc.)
    # This will fail because the anomaly feature isn't implemented yet
    assert "anomaly-badge" in html.lower() or "anomaly-indicator" in html.lower()


def test_task_page_no_badge_when_normal(client_no_anomalies):
    resp = client_no_anomalies.get("/task/20260506-normal-0000")
    assert resp.status_code == 200
    html = resp.text
    assert "anomaly" not in html.lower()
