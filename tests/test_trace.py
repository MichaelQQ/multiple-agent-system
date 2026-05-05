import json
import warnings
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch
import pytest
from typer.testing import CliRunner
from mas.cli import app
from mas.schemas import Task, Result

PROJECT_ROOT = Path(__file__).parent.parent

runner = CliRunner()

@pytest.fixture
def mas_dir(tmp_path):
    d = tmp_path / ".mas"
    (d / "tasks" / "done").mkdir(parents=True)
    (d / "tasks" / "doing").mkdir(parents=True)
    (d / "tasks" / "failed").mkdir(parents=True)
    return d

def test_trace_happy_path_json(mas_dir):
    task_id = "20260427-happy-path-a1b2"
    task_dir = mas_dir / "tasks" / "done" / task_id
    task_dir.mkdir(parents=True)
    
    # Task data
    task = Task(id=task_id, role="orchestrator", goal="do something big")
    (task_dir / "task.json").write_text(task.model_dump_json())
    
    # Audit events
    events = [
        {
            "timestamp": "2026-04-27T10:00:00+00:00",
            "event": "dispatch",
            "role": "implementer",
            "task_id": task_id,
            "subtask_id": "impl-1",
            "details": {"cycle": 0}
        },
        {
            "timestamp": "2026-04-27T10:05:00+00:00",
            "event": "completion",
            "role": "implementer",
            "task_id": task_id,
            "subtask_id": "impl-1",
            "status": "success",
            "duration_s": 300.0,
            "details": {"cycle": 0}
        },
        {
            "timestamp": "2026-04-27T10:06:00+00:00",
            "event": "dispatch",
            "role": "tester",
            "task_id": task_id,
            "subtask_id": "test-1",
            "details": {"cycle": 0}
        },
        {
            "timestamp": "2026-04-27T10:10:00+00:00",
            "event": "completion",
            "role": "tester",
            "task_id": task_id,
            "subtask_id": "test-1",
            "status": "success",
            "duration_s": 240.0,
            "details": {"cycle": 0}
        },
        {
            "timestamp": "2026-04-27T10:16:00+00:00",
            "event": "dispatch",
            "role": "implementer",
            "task_id": task_id,
            "subtask_id": "impl-1",
            "details": {"cycle": 1}
        },
        {
            "timestamp": "2026-04-27T10:20:00+00:00",
            "event": "completion",
            "role": "implementer",
            "task_id": task_id,
            "subtask_id": "impl-1",
            "status": "success",
            "duration_s": 240.0,
            "details": {"cycle": 1}
        }
    ]
    (task_dir / "audit.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    
    # Subtask results
    # We follow the layout: .mas/tasks/.../<task-id>/<subtask-id>/result.json
    # For cycle 1 of impl-1, we assume it's in a specific subdir or the implementation handles it.
    # The prompt says: "subtask subdirs each with a result.json"
    
    # impl-1 rev-0
    d0 = task_dir / "impl-1"
    d0.mkdir(parents=True)
    res0 = Result(task_id="impl-1", status="success", summary="done", duration_s=300.0, cost_usd=0.05)
    (d0 / "result.json").write_text(res0.model_dump_json())
    
    # test-1 rev-0
    dt = task_dir / "test-1"
    dt.mkdir(parents=True)
    rest = Result(task_id="test-1", status="success", summary="done", duration_s=240.0, cost_usd=0.02)
    (dt / "result.json").write_text(rest.model_dump_json())
    
    # impl-1 rev-1
    d1 = task_dir / "impl-1-rev-1"
    d1.mkdir(parents=True)
    res1 = Result(task_id="impl-1", status="success", summary="done", duration_s=240.0, cost_usd=0.04)
    (d1 / "result.json").write_text(res1.model_dump_json())

    # Transitions
    (task_dir / "transitions.jsonl").write_text(
        "2026-04-27T10:00:00+00:00|proposed|doing|start\n"
        "2026-04-27T10:21:00+00:00|doing|done|finished\n"
    )

    with patch("mas.cli.project_dir", return_value=mas_dir):
        result = runner.invoke(app, ["trace", task_id, "--json"])
    
    assert result.exit_code == 0
    data = json.loads(result.output)
    
    # (1) Assert JSON shape
    assert set(data.keys()) == {
        "task_id", "goal", "started_at", "ended_at",
        "total_duration_s", "total_cost_usd", "stages",
        "graph", "transitions"
    }
    assert isinstance(data["graph"], dict)
    assert set(data["graph"].keys()) == {"nodes", "edges"}
    assert isinstance(data["transitions"], list)
    # transitions.jsonl had two lines for this task
    assert len(data["transitions"]) == 2
    assert data["transitions"][0]["from"] == "proposed"
    assert data["transitions"][0]["to"] == "doing"
    assert data["task_id"] == task_id
    assert data["goal"] == "do something big"
    assert data["started_at"] == "2026-04-27T10:00:00+00:00"
    assert data["ended_at"] == "2026-04-27T10:21:00+00:00"
    
    # (1) Assert stages ordered by started_at
    stages = data["stages"]
    assert len(stages) == 3
    assert stages[0]["subtask_id"] == "impl-1"
    assert stages[0]["cycle"] == "rev-0"
    assert stages[1]["subtask_id"] == "test-1"
    assert stages[1]["cycle"] == "rev-0"
    assert stages[2]["subtask_id"] == "impl-1"
    assert stages[2]["cycle"] == "rev-1"
    
    # (1) Assert stage keys
    for s in stages:
        assert set(s.keys()) == {
            "subtask_id", "role", "cycle", "started_at", "ended_at", 
            "duration_s", "status", "cost_usd"
        }

    # (5) total_duration_s and total_cost_usd sum
    expected_dur = 300 + 240 + 240
    expected_cost = 0.05 + 0.02 + 0.04
    assert abs(data["total_duration_s"] - expected_dur) < 0.1
    assert abs(data["total_cost_usd"] - expected_cost) < 0.001

def test_trace_in_flight_doing(mas_dir):
    task_id = "20260427-in-flight-c3d4"
    task_dir = mas_dir / "tasks" / "doing" / task_id
    task_dir.mkdir(parents=True)
    
    task = Task(id=task_id, role="orchestrator", goal="still working")
    (task_dir / "task.json").write_text(task.model_dump_json())
    
    events = [
        {
            "timestamp": "2026-04-27T12:00:00+00:00",
            "event": "dispatch",
            "role": "implementer",
            "task_id": task_id,
            "subtask_id": "impl-1",
            "details": {"cycle": 0}
        }
    ]
    (task_dir / "audit.jsonl").write_text(json.dumps(events[0]) + "\n")
    (task_dir / "transitions.jsonl").write_text(
        "2026-04-27T12:00:00+00:00|proposed|doing|start\n"
    )

    with patch("mas.cli.project_dir", return_value=mas_dir):
        result = runner.invoke(app, ["trace", task_id, "--json"])
    
    assert result.exit_code == 0
    data = json.loads(result.output)
    
    # (2) in-flight doing/ task with one subtask still running
    # stage has ended_at == null and status == "running"
    stage = data["stages"][0]
    assert stage["ended_at"] is None
    assert stage["status"] == "running"
    
    # (2) total_ended_at reflects 'now' (top-level ended_at)
    assert data["ended_at"] is not None
    dt_ended = datetime.fromisoformat(data["ended_at"].replace("Z", "+00:00"))
    assert (datetime.now(timezone.utc) - dt_ended).total_seconds() < 60

def test_trace_unknown_id(mas_dir):
    with patch("mas.cli.project_dir", return_value=mas_dir):
        # (3) unknown task id exits with code 1 and a clear error on stderr
        result = runner.invoke(app, ["trace", "non-existent-id-8888"])
    
    assert result.exit_code == 1
    # result.output is the combined stdout+stderr (mix_stderr=True default)
    combined_output = result.output.lower()
    assert "not found" in combined_output or "unknown" in combined_output

def test_trace_empty_malformed_files(mas_dir):
    task_id = "20260427-empty-e5f6"
    task_dir = mas_dir / "tasks" / "doing" / task_id
    task_dir.mkdir(parents=True)
    
    task = Task(id=task_id, role="orchestrator", goal="empty")
    (task_dir / "task.json").write_text(task.model_dump_json())
    
    # (4) empty/malformed transitions.jsonl and audit.jsonl do not crash
    (task_dir / "transitions.jsonl").write_text("")
    (task_dir / "audit.jsonl").write_text("this is not json\n")
    
    with patch("mas.cli.project_dir", return_value=mas_dir):
        # Capture warnings emitted during command execution
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            # (4) produce a 'no stage data yet' message in the default Rich view
            result_default = runner.invoke(app, ["trace", task_id])
        assert result_default.exit_code == 0
        assert "no stage data yet" in result_default.output.lower()
        # (4) malformed-line warning emitted (mirroring mas audit behavior)
        assert any(
            "malformed" in str(w.message)
            for w in captured
            if issubclass(w.category, UserWarning)
        )

        # (4) --json still emits valid JSON with an empty stages list
        result_json = runner.invoke(app, ["trace", task_id, "--json"])
        assert result_json.exit_code == 0
        data = json.loads(result_json.output)
        assert data["stages"] == []

def test_trace_rich_labels(mas_dir):
    task_id = "20260427-labels-9999"
    task_dir = mas_dir / "tasks" / "done" / task_id
    task_dir.mkdir(parents=True)

    task = Task(id=task_id, role="orchestrator", goal="check labels")
    (task_dir / "task.json").write_text(task.model_dump_json())

    events = [
        {
            "timestamp": "2026-04-27T14:00:00+00:00",
            "event": "dispatch",
            "role": "implementer",
            "task_id": task_id,
            "subtask_id": "impl-1",
            "details": {"cycle": 0}
        }
    ]
    (task_dir / "audit.jsonl").write_text(json.dumps(events[0]) + "\n")
    (task_dir / "transitions.jsonl").write_text("2026-04-27T14:00:00+00:00|proposed|doing|start\n")

    with patch("mas.cli.project_dir", return_value=mas_dir):
        result = runner.invoke(app, ["trace", task_id])

    assert result.exit_code == 0
    # "at least one role[cycle] label"
    assert "implementer[rev-0]" in result.output


def test_trace_renders_graph_and_transitions_log(mas_dir):
    """build_trace should expose graph.json and read the production
    `.transitions.log` filename, not just the legacy `transitions.jsonl`."""
    task_id = "20260505-graph-trace-7777"
    task_dir = mas_dir / "tasks" / "done" / task_id
    task_dir.mkdir(parents=True)

    task = Task(id=task_id, role="orchestrator", goal="exercise graph + transitions")
    (task_dir / "task.json").write_text(task.model_dump_json())

    graph_payload = {
        "nodes": [
            {
                "subtask_id": "impl-1", "role": "implementer", "cycle": 0,
                "status": "success", "verdict": None,
                "summary": "implemented X",
                "feedback": None,
                "artifacts": [], "handoff": None,
            },
            {
                "subtask_id": "eval-1", "role": "evaluator", "cycle": 0,
                "status": "success", "verdict": "needs_revision",
                "summary": "missing test", "feedback": "add coverage for X",
                "artifacts": [], "handoff": None,
            },
            {
                "subtask_id": "rev-1-impl", "role": "implementer", "cycle": 1,
                "status": "success", "verdict": None,
                "summary": "fixed", "feedback": None,
                "artifacts": [], "handoff": None,
            },
        ],
        "edges": [
            {"from_id": "impl-1", "to_id": "eval-1", "kind": "sequence", "reason": None},
            {"from_id": "eval-1", "to_id": "rev-1-impl", "kind": "revision",
             "reason": "add coverage for X"},
        ],
    }
    (task_dir / "graph.json").write_text(json.dumps(graph_payload))

    # Production filename — the trace must read it without a transitions.jsonl fallback
    (task_dir / ".transitions.log").write_text(
        "2026-05-05T09:00:00+00:00|proposed|doing|start\n"
        "2026-05-05T09:30:00+00:00|doing|done|finished\n"
    )
    # No audit events — stages will be empty, but graph + transitions must still surface
    (task_dir / "audit.jsonl").write_text("")

    with patch("mas.cli.project_dir", return_value=mas_dir):
        result_json = runner.invoke(app, ["trace", task_id, "--json"])
    assert result_json.exit_code == 0
    data = json.loads(result_json.output)

    # graph payload preserved (nodes + edges projection)
    assert len(data["graph"]["nodes"]) == 3
    assert {n["subtask_id"] for n in data["graph"]["nodes"]} == {"impl-1", "eval-1", "rev-1-impl"}
    revision_edges = [e for e in data["graph"]["edges"] if e["kind"] == "revision"]
    assert len(revision_edges) == 1
    assert revision_edges[0]["from_id"] == "eval-1"
    assert revision_edges[0]["to_id"] == "rev-1-impl"
    assert "add coverage" in revision_edges[0]["reason"]

    # transitions read from .transitions.log
    assert len(data["transitions"]) == 2
    assert data["transitions"][-1]["to"] == "done"

    # default Rich view surfaces graph + transitions even with no stages
    with patch("mas.cli.project_dir", return_value=mas_dir):
        result_rich = runner.invoke(app, ["trace", task_id])
    assert result_rich.exit_code == 0
    out = result_rich.output
    assert "Graph nodes" in out
    assert "Graph edges" in out
    assert "Transitions" in out
    assert "revision" in out
    assert "rev-1-impl" in out


def test_readme_trace_documents_json_object_shape():
    readme = (PROJECT_ROOT / "README.md").read_text()

    # The --json example must NOT be a bare top-level JSON array
    assert "# JSON array, one object per stage" not in readme, (
        "README inline comment still says 'JSON array, one object per stage' — "
        "update to reflect the wrapping object shape"
    )
    assert "JSON array, one object per stage" not in readme, (
        "README still describes --json output as a JSON array — "
        "the actual output is a single JSON object"
    )

    # The example block must contain the wrapping object keys
    assert '"task_id"' in readme, "README --json example must include top-level 'task_id' key"
    assert '"stages"' in readme, "README --json example must include top-level 'stages' key"
    assert '"total_duration_s"' in readme, (
        "README --json example must include 'total_duration_s' key"
    )

    # cycle values must be shown as strings (\"rev-0\"), not integers (0)
    # Find the Trace section and ensure cycle: 0 (integer) is not in the example
    trace_section_start = readme.find("## Trace")
    assert trace_section_start != -1, "README must contain a ## Trace section"
    next_section = readme.find("\n## ", trace_section_start + 1)
    trace_section = readme[trace_section_start:next_section] if next_section != -1 else readme[trace_section_start:]
    assert '"cycle": 0' not in trace_section, (
        "README Trace example shows cycle as integer 0 — "
        "it must be the string \"rev-0\" to match the implementation"
    )
    assert '"rev-0"' in trace_section or "'rev-0'" in trace_section, (
        "README Trace example must show cycle as string \"rev-0\", not integer"
    )


def test_changelog_trace_describes_json_object():
    changelog = (PROJECT_ROOT / "CHANGELOG.md").read_text()

    assert "JSON array instead of a Rich table" not in changelog, (
        "CHANGELOG still says '--json emits a JSON array instead of a Rich table' — "
        "the actual output is a wrapping JSON object with task_id, goal, stages, etc."
    )

    # The CHANGELOG must describe the wrapping object shape
    assert "JSON object" in changelog or (
        "task_id" in changelog and "stages" in changelog
    ), (
        "CHANGELOG mas trace bullet must describe the --json output as a JSON object "
        "with task_id, goal, started_at, ended_at, total_duration_s, total_cost_usd, and stages"
    )
