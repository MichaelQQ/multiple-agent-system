"""Tests for parsing robustness in mas.

These tests encode the expected behavior for handling malformed/unexpected input to
parsing functions. The key requirement is that parsing should raise clear,
specific exceptions (not raw json.JSONDecodeError or generic ValidationError)
with messages identifying what went wrong and where.

Additionally, some functions should gracefully degrade when possible (e.g., extract
core fields even when extra fields are present).
"""

import json
from pathlib import Path

import pytest

from mas import board
from mas.roles import parse_plan
from mas.schemas import Result, Task


@pytest.fixture
def mas(tmp_path: Path) -> Path:
    d = tmp_path / ".mas"
    board.ensure_layout(d)
    return d


# === parse_plan() tests ===

def test_parse_plan_malformed_json_raises_specific_error(tmp_path: Path):
    """Malformed JSON should raise a clear, specific error (not raw ValidationError).

    Currently raises pydantic ValidationError with "json_invalid" in message.
    Expected: custom PlanParseError with clear message.
    """
    plan_path = tmp_path / "plan.json"
    plan_path.write_text("{invalid json}")

    with pytest.raises(Exception) as exc_info:
        parse_plan(plan_path, "parent-1")

    exc = exc_info.value
    assert "PlanParseError" in type(exc).__name__ or "JSONDecodeError" in type(exc).__name__, \
        f"Expected custom PlanParseError, got {type(exc).__name__}: {exc}"


def test_parse_plan_tolerates_missing_parent_id(tmp_path: Path):
    """Missing parent_id in file should use the passed parent_id parameter.

    Currently works because parse_plan calls setdefault("parent_id", parent_id).
    This test verifies that behavior (not a failure case).
    """
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"summary": "test", "subtasks": []}))

    result = parse_plan(plan_path, "parent-1")
    assert result.parent_id == "parent-1"


def test_parse_plan_missing_summary_raises_specific_error(tmp_path: Path):
    """Missing required field 'summary' should raise a clear error.

    Currently raises pydantic ValidationError.
    Expected: custom PlanParseError with "summary" in message.
    """
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"parent_id": "p1", "subtasks": []}))

    with pytest.raises(Exception) as exc_info:
        parse_plan(plan_path, "parent-1")

    exc = exc_info.value
    assert "PlanParseError" in type(exc).__name__, \
        f"Expected custom PlanParseError, got {type(exc).__name__}: {exc}"
    assert "summary" in str(exc).lower()


def test_parse_plan_missing_subtasks_raises_specific_error(tmp_path: Path):
    """Missing required field 'subtasks' should raise a clear error.

    Currently raises pydantic ValidationError.
    Expected: custom PlanParseError with "subtasks" in message.
    """
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"parent_id": "p1", "summary": "test"}))

    with pytest.raises(Exception) as exc_info:
        parse_plan(plan_path, "parent-1")

    exc = exc_info.value
    assert "PlanParseError" in type(exc).__name__, \
        f"Expected custom PlanParseError, got {type(exc).__name__}: {exc}"
    assert "subtasks" in str(exc).lower()


def test_parse_plan_rejects_extra_fields(tmp_path: Path):
    """Extra unknown fields must be rejected (strict schema, extra="forbid")."""
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({
        "parent_id": "p1",
        "summary": "test",
        "subtasks": [],
        "unknown_field": "ignored",
        "extra_data": {"foo": "bar"},
    }))

    with pytest.raises(Exception) as exc_info:
        parse_plan(plan_path, "parent-1")
    assert "PlanParseError" in type(exc_info.value).__name__


def test_parse_plan_truncated_input_raises_specific_error(tmp_path: Path):
    """Truncated input should raise a clear error.

    Currently raises pydantic ValidationError.
    Expected: custom PlanParseError.
    """
    plan_path = tmp_path / "plan.json"
    plan_path.write_text('{"parent_id": "p1", "summary": "test", "subtasks": [')

    with pytest.raises(Exception) as exc_info:
        parse_plan(plan_path, "parent-1")

    exc = exc_info.value
    assert "PlanParseError" in type(exc).__name__ or "JSONDecodeError" in type(exc).__name__, \
        f"Expected custom error, got {type(exc).__name__}: {exc}"


def test_parse_plan_empty_file_raises_specific_error(tmp_path: Path):
    """Empty file should raise a clear error.

    Currently raises pydantic ValidationError.
    Expected: custom PlanParseError with "empty" in message.
    """
    plan_path = tmp_path / "plan.json"
    plan_path.write_text("")

    with pytest.raises(Exception) as exc_info:
        parse_plan(plan_path, "parent-1")

    exc = exc_info.value
    assert "PlanParseError" in type(exc).__name__ or "JSONDecodeError" in type(exc).__name__, \
        f"Expected custom error, got {type(exc).__name__}: {exc}"
    assert "empty" in str(exc).lower()


def test_parse_plan_non_utf8_file_raises_specific_error(tmp_path: Path):
    """Non-UTF8 file content should raise a clear error.

    Currently raises UnicodeDecodeError.
    Expected: custom PlanParseError with clear message about encoding.
    """
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes('{"parent_id": "p1", "summary": "s", "subtasks": []}'.encode("utf-16"))

    with pytest.raises(Exception) as exc_info:
        parse_plan(plan_path, "parent-1")

    exc = exc_info.value
    assert "PlanParseError" in type(exc).__name__, \
        f"Expected custom PlanParseError, got {type(exc).__name__}"
    assert "UTF" in str(exc).upper() or "encoding" in str(exc).lower()


# === read_task() tests ===

def test_read_task_malformed_json_raises_specific_error(mas: Path):
    """Malformed JSON should raise a clear error (not raw pydantic ValidationError).

    Currently raises pydantic ValidationError.
    Expected: custom TaskReadError with clear message.
    """
    task_dir = board.task_dir(mas, "proposed", "t1")
    task_dir.mkdir(parents=True)
    (task_dir / "task.json").write_text("{invalid")

    with pytest.raises(Exception) as exc_info:
        board.read_task(task_dir)

    exc = exc_info.value
    assert "TaskReadError" in type(exc).__name__, \
        f"Expected custom TaskReadError, got {type(exc).__name__}"


def test_read_task_missing_id_raises_specific_error(mas: Path):
    """Missing 'id' field should raise a clear error.

    Currently raises pydantic ValidationError.
    Expected: custom TaskReadError.
    """
    task_dir = board.task_dir(mas, "proposed", "t1")
    task_dir.mkdir(parents=True)
    (task_dir / "task.json").write_text(json.dumps({
        "role": "proposer",
        "goal": "test goal",
    }))

    with pytest.raises(Exception) as exc_info:
        board.read_task(task_dir)

    exc = exc_info.value
    assert "TaskReadError" in type(exc).__name__, \
        f"Expected custom TaskReadError, got {type(exc).__name__}"


def test_read_task_missing_role_raises_specific_error(mas: Path):
    """Missing 'role' field should raise a clear error.

    Currently raises pydantic ValidationError.
    Expected: custom TaskReadError.
    """
    task_dir = board.task_dir(mas, "proposed", "t1")
    task_dir.mkdir(parents=True)
    (task_dir / "task.json").write_text(json.dumps({
        "id": "t1",
        "goal": "test goal",
    }))

    with pytest.raises(Exception) as exc_info:
        board.read_task(task_dir)

    exc = exc_info.value
    assert "TaskReadError" in type(exc).__name__, \
        f"Expected custom TaskReadError, got {type(exc).__name__}"


def test_read_task_missing_goal_raises_specific_error(mas: Path):
    """Missing 'goal' field should raise a clear error.

    Currently raises pydantic ValidationError.
    Expected: custom TaskReadError.
    """
    task_dir = board.task_dir(mas, "proposed", "t1")
    task_dir.mkdir(parents=True)
    (task_dir / "task.json").write_text(json.dumps({
        "id": "t1",
        "role": "proposer",
    }))

    with pytest.raises(Exception) as exc_info:
        board.read_task(task_dir)

    exc = exc_info.value
    assert "TaskReadError" in type(exc).__name__, \
        f"Expected custom TaskReadError, got {type(exc).__name__}"


def test_read_task_extra_fields_rejected(mas: Path):
    """Extra fields must be rejected (strict schema, extra="forbid")."""
    task_dir = board.task_dir(mas, "proposed", "20260415-t1-aaaa")
    task_dir.mkdir(parents=True)
    (task_dir / "task.json").write_text(json.dumps({
        "id": "20260415-t1-aaaa",
        "role": "proposer",
        "goal": "test goal",
        "extra_field1": "ignored",
        "extra_field2": {"nested": "ignored"},
    }))

    with pytest.raises(Exception) as exc_info:
        board.read_task(task_dir)
    assert "TaskReadError" in type(exc_info.value).__name__


def test_read_task_empty_file_raises_specific_error(mas: Path):
    """Empty file should raise a clear error.

    Currently raises pydantic ValidationError.
    Expected: custom TaskReadError with "empty" message.
    """
    task_dir = board.task_dir(mas, "proposed", "t1")
    task_dir.mkdir(parents=True)
    (task_dir / "task.json").write_text("")

    with pytest.raises(Exception) as exc_info:
        board.read_task(task_dir)

    exc = exc_info.value
    assert "TaskReadError" in type(exc).__name__, \
        f"Expected custom TaskReadError, got {type(exc).__name__}"
    assert "empty" in str(exc).lower()


# === read_result() tests ===

def test_read_result_malformed_json_raises_specific_error(mas: Path):
    """Malformed JSON should raise a clear error.

    Currently raises pydantic ValidationError.
    Expected: custom ResultReadError.
    """
    task_dir = board.task_dir(mas, "doing", "t1")
    task_dir.mkdir(parents=True)
    (task_dir / "result.json").write_text("{invalid")

    with pytest.raises(Exception) as exc_info:
        board.read_result(task_dir)

    exc = exc_info.value
    assert "ResultReadError" in type(exc).__name__, \
        f"Expected custom ResultReadError, got {type(exc).__name__}"


def test_read_result_missing_task_id_raises_specific_error(mas: Path):
    """Missing 'task_id' should raise a clear error.

    Currently raises pydantic ValidationError.
    Expected: custom ResultReadError.
    """
    task_dir = board.task_dir(mas, "doing", "t1")
    task_dir.mkdir(parents=True)
    (task_dir / "result.json").write_text(json.dumps({
        "status": "success",
        "summary": "test",
    }))

    with pytest.raises(Exception) as exc_info:
        board.read_result(task_dir)

    exc = exc_info.value
    assert "ResultReadError" in type(exc).__name__, \
        f"Expected custom ResultReadError, got {type(exc).__name__}"


def test_read_result_missing_status_raises_specific_error(mas: Path):
    """Missing 'status' should raise a clear error.

    Currently raises pydantic ValidationError.
    Expected: custom ResultReadError.
    """
    task_dir = board.task_dir(mas, "doing", "t1")
    task_dir.mkdir(parents=True)
    (task_dir / "result.json").write_text(json.dumps({
        "task_id": "t1",
        "summary": "test",
    }))

    with pytest.raises(Exception) as exc_info:
        board.read_result(task_dir)

    exc = exc_info.value
    assert "ResultReadError" in type(exc).__name__, \
        f"Expected custom ResultReadError, got {type(exc).__name__}"


def test_read_result_missing_summary_raises_specific_error(mas: Path):
    """Missing 'summary' should raise a clear error.

    Currently raises pydantic ValidationError.
    Expected: custom ResultReadError.
    """
    task_dir = board.task_dir(mas, "doing", "t1")
    task_dir.mkdir(parents=True)
    (task_dir / "result.json").write_text(json.dumps({
        "task_id": "t1",
        "status": "success",
    }))

    with pytest.raises(Exception) as exc_info:
        board.read_result(task_dir)

    exc = exc_info.value
    assert "ResultReadError" in type(exc).__name__, \
        f"Expected custom ResultReadError, got {type(exc).__name__}"


def test_read_result_empty_file_raises_specific_error(mas: Path):
    """Empty file should raise a clear error.

    Currently raises pydantic ValidationError.
    Expected: custom ResultReadError with "empty" message.
    """
    task_dir = board.task_dir(mas, "doing", "t1")
    task_dir.mkdir(parents=True)
    (task_dir / "result.json").write_text("")

    with pytest.raises(Exception) as exc_info:
        board.read_result(task_dir)

    exc = exc_info.value
    assert "ResultReadError" in type(exc).__name__, \
        f"Expected custom ResultReadError, got {type(exc).__name__}"
    assert "empty" in str(exc).lower()


def test_read_result_partial_truncated_json_raises_specific_error(mas: Path):
    """Partial/truncated JSON should raise a clear error.

    Currently raises pydantic ValidationError.
    Expected: custom ResultReadError.
    """
    task_dir = board.task_dir(mas, "doing", "t1")
    task_dir.mkdir(parents=True)
    (task_dir / "result.json").write_text('{"task_id": "t1", "status": "success"')

    with pytest.raises(Exception) as exc_info:
        board.read_result(task_dir)

    exc = exc_info.value
    assert "ResultReadError" in type(exc).__name__, \
        f"Expected custom ResultReadError, got {type(exc).__name__}"


def test_read_result_nonexistent_file(mas: Path):
    """Nonexistent result.json should return None."""
    task_dir = board.task_dir(mas, "doing", "t1")
    task_dir.mkdir(parents=True)

    result = board.read_result(task_dir)
    assert result is None


# === _materialize_proposal() tests ===

def test_materialize_proposal_empty_handoff_skips(mas: Path):
    """Empty handoff should skip materialization gracefully (no crash).

    Currently crashes because goal is required and code tries to use None as string.
    Expected behavior: should log warning and return early (no crash).
    """
    from mas import tick
    from mas.schemas import Result

    result = Result(
        task_id="t1",
        status="success",
        summary="test",
        handoff={},
    )

    class FakeCfg:
        max_proposed = 10

    env = type("TickEnv", (), {"cfg": FakeCfg(), "mas": mas})()

    tick._materialize_proposal(env, result)


# === _materialize_plan() tests ===

def test_materialize_plan_missing_subtasks(tmp_path: Path):
    """Handoff missing subtasks should return False (not crash)."""
    from mas import tick
    from mas.schemas import Result

    result = Result(
        task_id="t1",
        status="success",
        summary="test",
        handoff={
            "parent_id": "p1",
            "summary": "test plan",
        },
    )

    parent_dir = tmp_path / "parent"
    parent_dir.mkdir()

    ok = tick._materialize_plan(parent_dir, result)
    assert ok is False


def test_materialize_plan_missing_summary(tmp_path: Path):
    """Handoff missing summary should return False (not crash)."""
    from mas import tick
    from mas.schemas import Result

    result = Result(
        task_id="t1",
        status="success",
        summary="test",
        handoff={
            "parent_id": "p1",
            "subtasks": [],
        },
    )

    parent_dir = tmp_path / "parent"
    parent_dir.mkdir()

    ok = tick._materialize_plan(parent_dir, result)
    assert ok is False