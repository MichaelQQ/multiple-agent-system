import json
import pytest
from pathlib import Path
from mas.cost_helpers import compute_role_baselines, detect_anomalies


def test_baseline_empty_history(tmp_path):
    mas_dir = tmp_path / "mas"
    mas_dir.mkdir()
    (mas_dir / "done").mkdir()
    result = compute_role_baselines(str(mas_dir))
    assert result == {}


def test_baseline_single_task(tmp_path):
    mas_dir = tmp_path / "mas"
    mas_dir.mkdir()
    done_dir = mas_dir / "done"
    done_dir.mkdir()
    task_id = "20260506-single-task-1234"
    task_dir = done_dir / task_id
    task_dir.mkdir()
    (task_dir / "task.json").write_text(json.dumps({"role": "implementer"}))
    (task_dir / "result.json").write_text(json.dumps({"cost_usd": 0.50}))
    baselines = compute_role_baselines(str(mas_dir))
    assert "implementer" in baselines
    assert baselines["implementer"] == pytest.approx(0.50)


def test_baseline_multiple_tasks(tmp_path):
    mas_dir = tmp_path / "mas"
    mas_dir.mkdir()
    done_dir = mas_dir / "done"
    done_dir.mkdir()
    tasks = [
        ("task1", "implementer", 0.5),
        ("task2", "implementer", 1.0),
        ("task3", "implementer", 1.5),
        ("task4", "tester", 0.2),
        ("task5", "tester", 0.4),
    ]
    for task_id, role, cost in tasks:
        task_dir = done_dir / task_id
        task_dir.mkdir()
        (task_dir / "task.json").write_text(json.dumps({"role": role}))
        (task_dir / "result.json").write_text(json.dumps({"cost_usd": cost}))
    baselines = compute_role_baselines(str(mas_dir))
    assert baselines["implementer"] == pytest.approx(1.0)
    assert baselines["tester"] == pytest.approx(0.3)


def test_baseline_uses_p75(tmp_path):
    mas_dir = tmp_path / "mas"
    mas_dir.mkdir()
    done_dir = mas_dir / "done"
    done_dir.mkdir()
    tasks = [
        ("task1", "implementer", 1.0),
        ("task2", "implementer", 2.0),
        ("task3", "implementer", 3.0),
        ("task4", "implementer", 4.0),
        ("task5", "implementer", 5.0),
    ]
    for task_id, role, cost in tasks:
        task_dir = done_dir / task_id
        task_dir.mkdir()
        (task_dir / "task.json").write_text(json.dumps({"role": role}))
        (task_dir / "result.json").write_text(json.dumps({"cost_usd": cost}))
    median_baselines = compute_role_baselines(str(mas_dir))
    p75_baselines = compute_role_baselines(str(mas_dir), percentile='p75')
    assert median_baselines["implementer"] == pytest.approx(3.0)
    assert p75_baselines["implementer"] == pytest.approx(4.0)


def test_flag_anomaly_above_threshold(tmp_path):
    mas_dir = tmp_path / "mas"
    mas_dir.mkdir()
    done_dir = mas_dir / "done"
    done_dir.mkdir()
    for i in range(3):
        task_dir = done_dir / f"baseline_task_{i}"
        task_dir.mkdir()
        (task_dir / "task.json").write_text(json.dumps({"role": "implementer"}))
        (task_dir / "result.json").write_text(json.dumps({"cost_usd": 1.0}))
    anomalous_dir = done_dir / "anomalous_task"
    anomalous_dir.mkdir()
    (anomalous_dir / "task.json").write_text(json.dumps({"role": "implementer"}))
    (anomalous_dir / "result.json").write_text(json.dumps({"cost_usd": 2.5}))
    normal_dir = done_dir / "normal_task"
    normal_dir.mkdir()
    (normal_dir / "task.json").write_text(json.dumps({"role": "implementer"}))
    (normal_dir / "result.json").write_text(json.dumps({"cost_usd": 1.5}))
    anomalies = detect_anomalies(str(mas_dir))
    anomalous_ids = [a["task_id"] for a in anomalies]
    assert "anomalous_task" in anomalous_ids
    assert "normal_task" not in anomalous_ids


def test_flag_anomaly_custom_threshold(tmp_path):
    mas_dir = tmp_path / "mas"
    mas_dir.mkdir()
    done_dir = mas_dir / "done"
    done_dir.mkdir()
    for i in range(3):
        task_dir = done_dir / f"baseline_{i}"
        task_dir.mkdir()
        (task_dir / "task.json").write_text(json.dumps({"role": "implementer"}))
        (task_dir / "result.json").write_text(json.dumps({"cost_usd": 1.0}))
    task_2_5x = done_dir / "task_2_5x"
    task_2_5x.mkdir()
    (task_2_5x / "task.json").write_text(json.dumps({"role": "implementer"}))
    (task_2_5x / "result.json").write_text(json.dumps({"cost_usd": 2.5}))
    task_3_5x = done_dir / "task_3_5x"
    task_3_5x.mkdir()
    (task_3_5x / "task.json").write_text(json.dumps({"role": "implementer"}))
    (task_3_5x / "result.json").write_text(json.dumps({"cost_usd": 3.5}))
    anomalies = detect_anomalies(str(mas_dir), multiplier=3.0)
    assert "task_2_5x" not in [a["task_id"] for a in anomalies]
    assert "task_3_5x" in [a["task_id"] for a in anomalies]


def test_anomaly_result_structure(tmp_path):
    mas_dir = tmp_path / "mas"
    mas_dir.mkdir()
    done_dir = mas_dir / "done"
    done_dir.mkdir()
    for i in range(3):
        task_dir = done_dir / f"baseline_{i}"
        task_dir.mkdir()
        (task_dir / "task.json").write_text(json.dumps({"role": "implementer"}))
        (task_dir / "result.json").write_text(json.dumps({"cost_usd": 1.0}))
    anomalous_dir = done_dir / "anomalous"
    anomalous_dir.mkdir()
    (anomalous_dir / "task.json").write_text(json.dumps({"role": "implementer"}))
    (anomalous_dir / "result.json").write_text(json.dumps({"cost_usd": 3.0}))
    anomalies = detect_anomalies(str(mas_dir))
    assert len(anomalies) == 1
    anomaly = anomalies[0]
    required_keys = {"task_id", "role", "actual_cost", "baseline", "delta", "multiplier_exceeded"}
    assert required_keys.issubset(anomaly.keys())
    assert anomaly["task_id"] == "anomalous"
    assert anomaly["role"] == "implementer"
    assert anomaly["actual_cost"] == pytest.approx(3.0)
    assert anomaly["baseline"] == pytest.approx(1.0)
    assert anomaly["delta"] == pytest.approx(2.0)
    assert anomaly["multiplier_exceeded"] == pytest.approx(3.0)


def test_no_anomalies_when_all_normal(tmp_path):
    mas_dir = tmp_path / "mas"
    mas_dir.mkdir()
    done_dir = mas_dir / "done"
    done_dir.mkdir()
    for i in range(5):
        task_dir = done_dir / f"task_{i}"
        task_dir.mkdir()
        (task_dir / "task.json").write_text(json.dumps({"role": "implementer"}))
        (task_dir / "result.json").write_text(json.dumps({"cost_usd": 1.0}))
    anomalies = detect_anomalies(str(mas_dir))
    assert anomalies == []
