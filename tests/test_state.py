"""Tests for parent_dir/state.json accumulation across child completions."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mas.schemas import Result, SubtaskSpec
from mas.state import (
    ParentState,
    RejectedAttempt,
    read_state,
    state_path,
    update_state_from_result,
    write_state,
)


def test_read_state_missing_returns_empty(tmp_path: Path):
    s = read_state(tmp_path)
    assert isinstance(s, ParentState)
    assert s.worktree_files_touched == []
    assert s.test_command is None
    assert s.last_known_green_sha is None
    assert s.accepted_artifacts == []
    assert s.rejected_attempts == []


def test_write_read_roundtrip(tmp_path: Path):
    s = ParentState(
        worktree_files_touched=["src/a.py"],
        test_command="pytest",
        last_known_green_sha="abc123",
        accepted_artifacts=["src/a.py"],
        rejected_attempts=[
            RejectedAttempt(subtask_id="t-1", role="tester", status="failure", summary="x")
        ],
    )
    write_state(tmp_path, s)
    assert state_path(tmp_path).exists()
    out = read_state(tmp_path)
    assert out.model_dump() == s.model_dump()


def test_invalid_state_falls_back_to_empty(tmp_path: Path):
    state_path(tmp_path).write_text("{not json")
    s = read_state(tmp_path)
    assert s == ParentState()


def test_update_from_tester_success_sets_test_command_and_files(tmp_path: Path):
    spec = SubtaskSpec(id="t-1", role="tester", goal="t")
    r = Result(
        task_id="x",
        status="success",
        summary="wrote tests",
        handoff={
            "test_command": ".venv/bin/pytest tests/test_foo.py",
            "test_files": ["tests/test_foo.py"],
            "stub_files": ["src/foo.py"],
            "initial_exit_code": 1,
        },
        artifacts=["tests/test_foo.py", "src/foo.py"],
    )
    s = update_state_from_result(tmp_path, spec, r)
    assert s.test_command == ".venv/bin/pytest tests/test_foo.py"
    assert "tests/test_foo.py" in s.worktree_files_touched
    assert "src/foo.py" in s.worktree_files_touched
    assert s.accepted_artifacts == []  # only implementer fills accepted_artifacts
    assert s.rejected_attempts == []


def test_update_from_implementer_success_records_accepted(tmp_path: Path):
    spec = SubtaskSpec(id="i-1", role="implementer", goal="impl")
    r = Result(
        task_id="x",
        status="success",
        summary="green",
        handoff={
            "changed_files": ["src/foo.py", "src/bar.py"],
            "final_exit_code": 0,
            "test_command": "pytest",
        },
    )
    s = update_state_from_result(tmp_path, spec, r)
    assert s.accepted_artifacts == ["src/foo.py", "src/bar.py"]
    assert "src/foo.py" in s.worktree_files_touched
    assert "src/bar.py" in s.worktree_files_touched
    assert s.test_command == "pytest"
    assert s.rejected_attempts == []


def test_update_from_implementer_failure_records_rejection(tmp_path: Path):
    spec = SubtaskSpec(id="i-1", role="implementer", goal="impl")
    r = Result(
        task_id="x",
        status="failure",
        summary="tests still failing",
        handoff={"changed_files": ["src/foo.py"], "final_exit_code": 1},
    )
    s = update_state_from_result(tmp_path, spec, r, attempt=2)
    assert s.accepted_artifacts == []  # not accepted on failure
    assert "src/foo.py" in s.worktree_files_touched
    assert len(s.rejected_attempts) == 1
    rej = s.rejected_attempts[0]
    assert rej.subtask_id == "i-1"
    assert rej.role == "implementer"
    assert rej.status == "failure"
    assert rej.attempt == 2
    assert "tests still failing" in rej.summary


def test_update_from_evaluator_needs_revision_records_rejection(tmp_path: Path):
    spec = SubtaskSpec(id="e-1", role="evaluator", goal="eval")
    r = Result(
        task_id="x",
        status="success",
        summary="thin tests",
        verdict="needs_revision",
        feedback="add coverage for edge case Y",
    )
    s = update_state_from_result(tmp_path, spec, r)
    assert s.last_known_green_sha is None
    assert len(s.rejected_attempts) == 1
    assert s.rejected_attempts[0].status == "needs_revision"


def test_update_from_evaluator_pass_captures_sha(tmp_path: Path, git_repo: Path):
    """On evaluator pass, capture worktree HEAD SHA as last_known_green_sha."""
    spec = SubtaskSpec(id="e-1", role="evaluator", goal="eval")
    r = Result(task_id="x", status="success", summary="ok", verdict="pass")
    s = update_state_from_result(tmp_path, spec, r, worktree=git_repo)
    expected = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert s.last_known_green_sha == expected


def test_update_from_evaluator_pass_tags_green_cycle_zero(tmp_path: Path, git_repo: Path):
    """Initial evaluator pass tags HEAD as mas/{task_id}/green-0."""
    parent_dir = tmp_path / "task-foo"
    parent_dir.mkdir()
    spec = SubtaskSpec(id="e-1", role="evaluator", goal="eval")
    r = Result(task_id="x", status="success", summary="ok", verdict="pass")
    s = update_state_from_result(parent_dir, spec, r, worktree=git_repo)
    tag = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "refs/tags/mas/task-foo/green-0"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert s.last_known_green_sha == tag


def test_update_from_evaluator_pass_tags_green_revision_cycle(tmp_path: Path, git_repo: Path):
    """Revision-cycle evaluator pass tags HEAD as mas/{task_id}/green-N."""
    parent_dir = tmp_path / "task-bar"
    parent_dir.mkdir()
    spec = SubtaskSpec(id="rev-2-evaluator", role="evaluator", goal="eval rev 2")
    r = Result(task_id="x", status="success", summary="ok", verdict="pass")
    update_state_from_result(parent_dir, spec, r, worktree=git_repo)
    tag = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "refs/tags/mas/task-bar/green-2"],
        capture_output=True, text=True, check=True,
    )
    assert tag.returncode == 0


def test_update_evaluator_needs_revision_does_not_tag(tmp_path: Path, git_repo: Path):
    """Tag is only created on `pass`, not on needs_revision."""
    parent_dir = tmp_path / "task-baz"
    parent_dir.mkdir()
    spec = SubtaskSpec(id="e-1", role="evaluator", goal="eval")
    r = Result(task_id="x", status="success", summary="ok", verdict="needs_revision")
    update_state_from_result(parent_dir, spec, r, worktree=git_repo)
    r2 = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "--verify", "refs/tags/mas/task-baz/green-0"],
        capture_output=True, text=True, check=False,
    )
    assert r2.returncode != 0


def test_update_accumulates_across_completions(tmp_path: Path):
    """Successive updates merge — files dedupe, attempts append."""
    spec_t = SubtaskSpec(id="t-1", role="tester", goal="t")
    spec_i = SubtaskSpec(id="i-1", role="implementer", goal="i")
    update_state_from_result(
        tmp_path, spec_t,
        Result(task_id="x", status="success", summary="ok",
               handoff={"test_command": "pytest", "test_files": ["t.py"],
                        "stub_files": ["s.py"], "initial_exit_code": 1}),
    )
    update_state_from_result(
        tmp_path, spec_i,
        Result(task_id="x", status="failure", summary="red",
               handoff={"changed_files": ["s.py"], "final_exit_code": 1}),
    )
    s = update_state_from_result(
        tmp_path, spec_i,
        Result(task_id="x", status="success", summary="green",
               handoff={"changed_files": ["s.py"], "final_exit_code": 0}),
        attempt=2,
    )
    # files dedupe
    assert s.worktree_files_touched.count("s.py") == 1
    assert "t.py" in s.worktree_files_touched
    assert s.test_command == "pytest"
    assert s.accepted_artifacts == ["s.py"]
    assert len(s.rejected_attempts) == 1
    assert s.rejected_attempts[0].subtask_id == "i-1"
    assert s.rejected_attempts[0].status == "failure"


def test_environment_error_recorded_as_rejection(tmp_path: Path):
    spec = SubtaskSpec(id="i-1", role="implementer", goal="impl")
    r = Result(task_id="x", status="environment_error", summary="sandbox blocked")
    s = update_state_from_result(tmp_path, spec, r)
    assert len(s.rejected_attempts) == 1
    assert s.rejected_attempts[0].status == "environment_error"
