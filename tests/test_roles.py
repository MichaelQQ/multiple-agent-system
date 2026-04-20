"""Tests for mas.roles - render_prompt, gather_proposer_signals, _shallow_tree, parse_plan."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from mas.errors import PlanParseError
from mas.roles import (
    _list_proposed_tasks,
    _run,
    _shallow_tree,
    gather_proposer_signals,
    parse_plan,
    render_prompt,
)
from mas.schemas import Plan, ProposerSignals, Result, Task


class TestRenderPrompt:
    """Tests for render_prompt function."""

    @pytest.fixture
    def template_file(self, tmp_path):
        content = """\
Role: $role
Task ID: $task_id
Goal: $goal
Cycle: $cycle
Attempt: $attempt
Parent ID: [$parent_id]
Previous Failure: [$previous_failure]
Inputs: $inputs_json
Constraints: $constraints_json
Prior Results:
$prior_results_json
Result Schema: $result_schema
Extra field: $extra_field
Unmatched: $unmatched_var
"""
        p = tmp_path / "test.tmpl"
        p.write_text(content)
        return p

    def test_proposer_role(self, template_file):
        task = Task(id="20260415-t1-aaaa", role="proposer", goal="suggest work")
        result = render_prompt(template_file, task)
        assert "Role: proposer" in result
        assert "Task ID: 20260415-t1-aaaa" in result
        assert "Cycle: 0" in result
        assert "Attempt: 1" in result

    def test_orchestrator_role(self, template_file):
        task = Task(id="20260415-t2-aaaa", role="orchestrator", goal="plan tasks")
        result = render_prompt(template_file, task)
        assert "Role: orchestrator" in result

    def test_implementer_role(self, template_file):
        task = Task(id="20260415-t3-aaaa", role="implementer", goal="write code")
        result = render_prompt(template_file, task)
        assert "Role: implementer" in result

    def test_tester_role(self, template_file):
        task = Task(id="20260415-t4-aaaa", role="tester", goal="verify code")
        result = render_prompt(template_file, task)
        assert "Role: tester" in result

    def test_evaluator_role(self, template_file):
        task = Task(id="20260415-t5-aaaa", role="evaluator", goal="judge results")
        result = render_prompt(template_file, task)
        assert "Role: evaluator" in result

    def test_safe_substitute_leaves_unmatched(self, template_file):
        """safe_substitute should leave unmatched $var patterns intact, not raise."""
        task = Task(id="20260415-t1-aaaa", role="proposer", goal="g")
        result = render_prompt(template_file, task)
        assert "$unmatched_var" in result

    def test_extra_kwargs_substituted(self, template_file):
        task = Task(id="20260415-t1-aaaa", role="proposer", goal="g")
        result = render_prompt(template_file, task, extra_field="custom_value")
        assert "Extra field: custom_value" in result

    def test_cycle_is_stringified(self, template_file):
        task = Task(id="20260415-t1-aaaa", role="proposer", goal="g", cycle=3)
        result = render_prompt(template_file, task)
        assert "Cycle: 3" in result

    def test_attempt_is_stringified(self, template_file):
        task = Task(id="20260415-t1-aaaa", role="proposer", goal="g", attempt=5)
        result = render_prompt(template_file, task)
        assert "Attempt: 5" in result

    def test_empty_parent_id(self, template_file):
        """parent_id=None should result in empty string."""
        task = Task(id="20260415-t1-aaaa", role="proposer", goal="g", parent_id=None)
        result = render_prompt(template_file, task)
        assert "Parent ID: []" in result

    def test_empty_previous_failure(self, template_file):
        """previous_failure=None should result in empty string."""
        task = Task(id="20260415-t1-aaaa", role="proposer", goal="g", previous_failure=None)
        result = render_prompt(template_file, task)
        assert "Previous Failure: []" in result

    def test_prior_results_serialized(self, template_file):
        task = Task(
            id="20260415-t1-aaaa",
            role="proposer",
            goal="g",
            prior_results=[
                Result(task_id="r1", status="success", summary="ok"),
            ],
        )
        result = render_prompt(template_file, task)
        assert "Prior Results:" in result
        assert '"task_id": "r1"' in result
        assert '"status": "success"' in result

    def test_inputs_serialized(self, template_file):
        task = Task(
            id="20260415-t1-aaaa",
            role="proposer",
            goal="g",
            inputs={"target_module": "src/foo.py"},
        )
        result = render_prompt(template_file, task)
        assert "Inputs:" in result
        assert "src/foo.py" in result

    def test_constraints_serialized(self, template_file):
        task = Task(
            id="20260415-t1-aaaa",
            role="proposer",
            goal="g",
            constraints={"tests_only": True},
        )
        result = render_prompt(template_file, task)
        assert "Constraints:" in result
        assert "tests_only" in result

    def test_result_schema_included(self, template_file):
        task = Task(id="20260415-t1-aaaa", role="proposer", goal="g")
        result = render_prompt(template_file, task)
        assert "Result Schema:" in result
        assert "task_id" in result
        assert "status" in result


class TestGatherProposerSignals:
    """Tests for gather_proposer_signals function."""

    def test_returns_proposer_signals(self, tmp_path):
        """Should return ProposerSignals model instance."""
        signals = gather_proposer_signals(tmp_path)
        assert isinstance(signals, ProposerSignals)

    @patch("mas.roles._run")
    def test_git_log_gathering(self, mock_run, tmp_path):
        mock_run.return_value = "abc123 2024-01-01 initial commit"
        signals = gather_proposer_signals(tmp_path, git_log_limit=10)
        assert signals.git_log == "abc123 2024-01-01 initial commit"

    @patch("mas.roles._run")
    def test_recent_diffs_gathering(self, mock_run, tmp_path):
        mock_run.return_value = "diff content"
        signals = gather_proposer_signals(tmp_path)
        assert "recent_diffs" in signals.model_dump()

    def test_ideas_md_loaded_when_exists(self, tmp_path):
        ideas = tmp_path / "ideas.md"
        ideas.write_text(" Idea 1\n Idea 2")
        signals = gather_proposer_signals(tmp_path, ideas_path=ideas)
        assert "Idea 1" in signals.ideas
        assert "Idea 2" in signals.ideas

    def test_ideas_empty_when_missing(self, tmp_path):
        signals = gather_proposer_signals(tmp_path, ideas_path=tmp_path / "nonexistent.md")
        assert signals.ideas == ""

    @patch("mas.roles._run")
    def test_ci_command_execution(self, mock_run, tmp_path):
        mock_run.return_value = "CI output line 1\nline 2"
        signals = gather_proposer_signals(
            tmp_path, ci_command=["make", "ci"]
        )
        mock_run.assert_called()
        assert "ci_output" in signals.model_dump()

    @patch("mas.roles._run")
    def test_ci_output_truncated_to_20k(self, mock_run, tmp_path):
        long_output = "x" * 30_000
        mock_run.return_value = long_output
        signals = gather_proposer_signals(
            tmp_path, ci_command=["make", "ci"]
        )
        assert len(signals.ci_output) <= 20_000

    def test_repo_scan_in_signals(self, tmp_path):
        signals = gather_proposer_signals(tmp_path)
        assert "repo_scan" in signals.model_dump()


class TestShallowTree:
    """Tests for _shallow_tree function."""

    def test_max_depth_limit(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "b").mkdir()
        (tmp_path / "a" / "b" / "c").mkdir()
        result = _shallow_tree(tmp_path, max_depth=0, max_entries=100)
        lines = result.strip().split("\n")
        assert all("b" not in line for line in lines)
        assert "a" in result

    def test_max_depth_allows_exact_depth(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "b").mkdir()
        (tmp_path / "a" / "b" / "c").mkdir()
        result = _shallow_tree(tmp_path, max_depth=1, max_entries=100)
        lines = result.strip().split("\n")
        assert "a" in result
        assert "a/b" in result

    def test_max_entries_limit(self, tmp_path):
        for i in range(10):
            (tmp_path / f"file{i}.txt").touch()
        result = _shallow_tree(tmp_path, max_depth=10, max_entries=5)
        lines = result.strip().split("\n")
        assert len(lines) <= 5

    def test_skips_git_directory(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "config").touch()
        result = _shallow_tree(tmp_path, max_depth=2, max_entries=100)
        assert ".git" not in result

    def test_skips_pycache(self, tmp_path):
        pycache = tmp_path / "__pycache__"
        pycache.mkdir()
        (pycache / "cache.pyc").touch()
        result = _shallow_tree(tmp_path, max_depth=2, max_entries=100)
        assert "__pycache__" not in result

    def test_skips_node_modules(self, tmp_path):
        node_modules = tmp_path / "node_modules"
        node_modules.mkdir()
        (node_modules / "package.json").touch()
        result = _shallow_tree(tmp_path, max_depth=2, max_entries=100)
        assert "node_modules" not in result

    def test_skips_venv(self, tmp_path):
        venv = tmp_path / ".venv"
        venv.mkdir()
        (venv / "pyvenv.cfg").touch()
        result = _shallow_tree(tmp_path, max_depth=2, max_entries=100)
        assert ".venv" not in result

    def test_skips_dotfiles(self, tmp_path):
        dotfile = tmp_path / ".hidden"
        dotfile.touch()
        (tmp_path / "visible.txt").touch()
        result = _shallow_tree(tmp_path, max_depth=2, max_entries=100)
        assert ".hidden" not in result
        assert "visible.txt" in result

    def test_oserror_handling(self, tmp_path):
        with patch("pathlib.Path.iterdir") as mock_iter:
            mock_iter.side_effect = OSError("Permission denied")
            result = _shallow_tree(tmp_path, max_depth=2, max_entries=100)
            assert result == ""


class TestListProposedTasks:
    """Tests for _list_proposed_tasks function."""

    def test_empty_when_no_proposed(self, tmp_path):
        mas = tmp_path / ".mas"
        mas.mkdir(parents=True)
        result = _list_proposed_tasks(mas)
        assert result == []

    def test_extracts_goal_from_task_json(self, tmp_path):
        mas = tmp_path / ".mas"
        task_dir = mas / "tasks" / "proposed" / "task-123"
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text(json.dumps({"goal": "Do something"}))
        result = _list_proposed_tasks(mas)
        assert "Do something" in result

    def test_falls_back_to_summary(self, tmp_path):
        mas = tmp_path / ".mas"
        task_dir = mas / "tasks" / "proposed" / "task-456"
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text(json.dumps({"summary": "Summary text"}))
        result = _list_proposed_tasks(mas)
        assert "Summary text" in result

    def test_falls_back_to_dirname(self, tmp_path):
        mas = tmp_path / ".mas"
        task_dir = mas / "tasks" / "proposed" / "my-task-id"
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text("{}")
        result = _list_proposed_tasks(mas)
        assert "my-task-id" in result

    def test_handles_malformed_json(self, tmp_path):
        mas = tmp_path / ".mas"
        task_dir = mas / "tasks" / "proposed" / "bad-task"
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text("not valid json{{")
        result = _list_proposed_tasks(mas)
        assert "bad-task" in result


class TestRun:
    """Tests for _run helper function."""

    @patch("subprocess.run")
    def test_returns_stdout(self, mock_run, tmp_path):
        mock_run.return_value.stdout = "output"
        mock_run.return_value.stderr = ""
        result = _run(["echo", "hello"], cwd=tmp_path)
        assert "output" in result

    @patch("subprocess.run")
    def test_includes_stderr(self, mock_run, tmp_path):
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = "error"
        result = _run(["false"], cwd=tmp_path)
        assert "[stderr]" in result
        assert "error" in result

    @patch("subprocess.run")
    def test_timeout_returns_error(self, mock_run, tmp_path):
        mock_run.side_effect = subprocess.TimeoutExpired(["cmd"], 30)
        result = _run(["sleep", "100"], cwd=tmp_path, timeout=1)
        assert "[error running" in result
        assert "timed out" in result.lower() or "timeout" in result.lower()

    @patch("subprocess.run")
    def test_oserror_returns_error(self, mock_run, tmp_path):
        mock_run.side_effect = OSError("No such command")
        result = _run(["nonexistent"], cwd=tmp_path)
        assert "[error running" in result
        assert "No such command" in result


class TestParsePlan:
    """Tests for parse_plan function."""

    @pytest.fixture
    def plan_file(self, tmp_path):
        return tmp_path / "plan.json"

    def test_valid_plan_parsing(self, plan_file):
        data = {
            "parent_id": "parent-1",
            "summary": "Test plan",
            "subtasks": [
                {"id": "s1", "role": "implementer", "goal": "do it"},
            ],
        }
        plan_file.write_text(json.dumps(data))
        plan = parse_plan(plan_file, "parent-1")
        assert isinstance(plan, Plan)
        assert plan.parent_id == "parent-1"
        assert plan.summary == "Test plan"
        assert len(plan.subtasks) == 1

    def test_parent_id_default_injected(self, plan_file):
        data = {
            "summary": "Plan without parent",
            "subtasks": [{"id": "s1", "role": "tester", "goal": "test"}],
        }
        plan_file.write_text(json.dumps(data))
        plan = parse_plan(plan_file, "injected-parent")
        assert plan.parent_id == "injected-parent"

    def test_malformed_json(self, plan_file):
        plan_file.write_text("{invalid json")
        with pytest.raises(PlanParseError):
            parse_plan(plan_file, "parent")

    def test_truncated_json(self, plan_file):
        plan_file.write_text('{"parent_id": "p"')
        with pytest.raises(PlanParseError):
            parse_plan(plan_file, "parent")

    def test_missing_required_fields(self, plan_file):
        plan_file.write_text('{"parent_id": "p"}')
        with pytest.raises(PlanParseError):
            parse_plan(plan_file, "p")

    def test_extra_fields_rejected(self, plan_file):
        data = {
            "parent_id": "p",
            "summary": "s",
            "subtasks": [],
            "extra_field": "not allowed",
        }
        plan_file.write_text(json.dumps(data))
        with pytest.raises(PlanParseError):
            parse_plan(plan_file, "p")


class TestGatherProposerSignalsIntegration:
    """Integration tests combining multiple functions."""

    @patch("mas.roles._run")
    def test_repo_scan_via_shallow_tree(self, mock_run, tmp_path):
        mock_run.return_value = ""
        with patch("mas.roles._shallow_tree") as mock_tree:
            mock_tree.return_value = "src/main.py"
            signals = gather_proposer_signals(tmp_path)
            assert signals.repo_scan == "src/main.py"

    @patch("mas.roles._run")
    def test_already_proposed_listed(self, mock_run, tmp_path):
        mock_run.return_value = ""
        mas = tmp_path / ".mas"
        proposed_dir = mas / "tasks" / "proposed"
        proposed_dir.mkdir(parents=True)
        task_dir = proposed_dir / "task-999"
        task_dir.mkdir()
        (task_dir / "task.json").write_text(json.dumps({"goal": "Proposed task"}))

        signals = gather_proposer_signals(tmp_path, mas_root=mas)
        assert "already_proposed" in signals.model_dump()
        assert "Proposed task" in signals.already_proposed