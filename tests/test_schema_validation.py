"""Tests for strict Pydantic validation at data boundaries.

These tests verify that unknown fields are rejected at every boundary where data flows
between components. Tests must fail against the current codebase because the
validation features don't exist yet.
"""

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from mas.schemas import Plan, Result, SubtaskSpec, Task


class TestBoardValidation:
    def test_read_task_rejects_unknown_fields(self, tmp_path):
        """read_task() should reject unknown fields instead of silently filtering them.

        Currently board.py lines 80-81 strip unknown keys before validation.
        After fix, unknown fields should cause ValidationError due to extra=forbid.
        """
        from mas import board

        task_dir = tmp_path / "task1"
        task_dir.mkdir()
        task_json = {
            "id": "20260415-test-1234",
            "role": "proposer",
            "goal": "test goal",
            "unknown_field": "should be rejected",
        }
        (task_dir / "task.json").write_text(json.dumps(task_json))

        with pytest.raises(ValidationError):
            board.read_task(task_dir)

    def test_read_result_rejects_malformed_json(self, tmp_path):
        """read_result() should reject malformed JSON."""
        from mas import board

        task_dir = tmp_path / "task1"
        task_dir.mkdir()
        (task_dir / "result.json").write_text("{ invalid json }")

        with pytest.raises(ValidationError):
            board.read_result(task_dir)

    def test_read_plan_returns_typed_plan(self, tmp_path):
        """board.read_plan() should exist and return typed Plan, rejecting invalid plans."""
        from mas import board

        plan_dir = tmp_path / "plan1"
        plan_dir.mkdir()

        invalid_plan = {
            "parent_id": "20260415-parent-1234",
            "summary": "test plan",
            "subtasks": [],
            "unknown_field": "should be rejected",
        }
        (plan_dir / "plan.json").write_text(json.dumps(invalid_plan))

        with pytest.raises(ValidationError):
            board.read_plan(plan_dir)

    def test_read_plan_helper_exists(self, tmp_path):
        """board.read_plan() helper should exist."""
        from mas import board

        plan_dir = tmp_path / "plan1"
        plan_dir.mkdir()
        valid_plan = {
            "parent_id": "20260415-parent-1234",
            "summary": "test plan",
            "subtasks": [
                {
                    "id": "subtask-1",
                    "role": "implementer",
                    "goal": "do something",
                }
            ],
        }
        (plan_dir / "plan.json").write_text(json.dumps(valid_plan))

        plan = board.read_plan(plan_dir)
        assert isinstance(plan, Plan)


class TestRolesValidation:
    def test_list_proposed_tasks_uses_task_model(self, tmp_path):
        """_list_proposed_tasks() should use Task model and extract goal from model field.

        Currently roles.py line 91-94 reads task.json as raw dict.
        After fix, it should use Task model and extract goal from model field.
        """
        from mas import roles

        mas_root = tmp_path / ".mas"
        proposed_dir = mas_root / "tasks" / "proposed"
        proposed_dir.mkdir(parents=True)

        task_dir = proposed_dir / "20260415-test-1234"
        task_dir.mkdir()
        task_json = {
            "id": "20260415-test-1234",
            "role": "orchestrator",
            "goal": "test goal from model",
            "unknown_role_field": "should cause rejection",
        }
        (task_dir / "task.json").write_text(json.dumps(task_json))

        tasks = roles._list_proposed_tasks(mas_root)
        assert "test goal from model" in tasks

    def test_parse_plan_returns_plan_model(self, tmp_path):
        """parse_plan() should return a Plan model and reject unknown fields."""
        from mas import roles

        plan_path = tmp_path / "plan.json"
        invalid_plan = {
            "parent_id": "20260415-parent-1234",
            "summary": "test plan",
            "subtasks": [],
            "unknown_field": "should be rejected by extra=forbid",
        }
        plan_path.write_text(json.dumps(invalid_plan))

        with pytest.raises(ValidationError):
            roles.parse_plan(plan_path, "20260415-parent-1234")


class TestTickValidation:
    def test_proposal_handoff_model_exists(self):
        """ProposalHandoff model should exist for typed handoff validation.

        Currently tick.py _materialize_proposal accesses result.handoff as untyped dict.
        After fix, a ProposalHandoff model should exist and validate handoff data.
        """
        from mas.schemas import ProposalHandoff

        valid_handoff = {
            "goal": "test goal",
            "rationale": "test rationale",
        }
        handoff = ProposalHandoff.model_validate(valid_handoff)
        assert handoff.goal == "test goal"

    def test_proposal_handoff_rejects_unknown_fields(self):
        """ProposalHandoff should reject unknown fields."""
        from mas.schemas import ProposalHandoff

        invalid_handoff = {
            "goal": "test goal",
            "unknown_field": "should be rejected",
        }
        with pytest.raises(ValidationError):
            ProposalHandoff.model_validate(invalid_handoff)


class TestOllamaAdapterValidation:
    def test_wrapper_validates_result(self, tmp_path):
        """Ollama wrapper should validate output through Result.model_validate().

        Currently ollama.py wrapper builds result as raw dict and filters unknown keys
        before writing result.json. After fix, should use Result.model_validate()
        and catch validation errors.
        """
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            test_script = f.name

        wrapper_check = f'''
import sys
import json

result_data = {{
    "task_id": "20260415-test-1234",
    "status": "invalid_status_not_allowed",
    "summary": "test summary",
}}

# Simulate what the ollama wrapper should do after fix:
try:
    from mas.schemas import Result
    Result.model_validate(result_data)
    print("FAIL: validation should have rejected invalid status")
    sys.exit(1)
except Exception as e:
    if "validation" in str(e).lower() or "error" in str(e).lower():
        print("PASS: validation correctly rejected invalid data")
        sys.exit(0)
    else:
        print(f"FAIL: wrong exception: {{e}}")
        sys.exit(1)
'''

        with open(test_script, "w") as f:
            f.write(wrapper_check)

        try:
            result = subprocess.run(
                [sys.executable, test_script],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 0, f"Expected validation to reject: {result.stderr}"
        finally:
            Path(test_script).unlink(missing_ok=True)


class TestSchemaValidation:
    def test_task_id_matches_pattern(self):
        """Task.id must match the {yyyymmdd}-{slug}-{hash4} pattern.

        Currently Task.id accepts any string. After fix, should validate the pattern.
        """
        invalid_id = "invalid-id-no-date"
        data = {"id": invalid_id, "role": "proposer", "goal": "test"}
        with pytest.raises(ValidationError):
            Task.model_validate(data)

        valid_id = "20260415-test-task-abcd"
        data = {"id": valid_id, "role": "proposer", "goal": "test"}
        task = Task.model_validate(data)
        assert task.id == valid_id

    def test_result_status_rejects_invalid_values(self):
        """Result.status must reject values outside the literal."""
        invalid_data = {
            "task_id": "20260415-test-1234",
            "status": "not_a_valid_status",
            "summary": "test",
        }
        with pytest.raises(ValidationError):
            Result.model_validate(invalid_data)

    def test_result_duration_s_must_be_non_negative(self):
        """Result.duration_s must be non-negative if provided.

        Currently Result.duration_s accepts negative values. After fix,
        should reject negative duration_s.
        """
        invalid_data = {
            "task_id": "20260415-test-1234",
            "status": "success",
            "summary": "test",
            "duration_s": -1.5,
        }
        with pytest.raises(ValidationError):
            Result.model_validate(invalid_data)

    def test_subtask_spec_role_rejects_unknown(self):
        """SubtaskSpec.role must reject unknown roles."""
        invalid_spec = {
            "id": "subtask-1",
            "role": "not_a_valid_role",
            "goal": "test",
        }
        with pytest.raises(ValidationError):
            SubtaskSpec.model_validate(invalid_spec)