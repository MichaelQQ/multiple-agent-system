import json
import pytest
from pathlib import Path
from datetime import datetime
from mas.tick import _materialize_proposal, TickEnv
from mas.patterns import FailurePattern, write_patterns
from mas.roles import goal_similarity
from mas.schemas import Result
from mas import board
from tests.test_proposals import _cfg


def _make_result(goal, task_id="proposer-task-1234"):
    return Result(
        task_id=task_id,
        status="success",
        summary="proposer handoff",
        handoff={"goal": goal},
    )


def test_skips_proposal_matching_pattern_count_gte_2(tmp_path):
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())
    pattern = FailurePattern(
        signature="sig_count_2",
        terminal_reason="unknown",
        goal_sample="fix login bug",
        count=2,
        last_seen=datetime.now().isoformat(),
        task_ids=["t1", "t2"],
        rejected_attempts_sample=[]
    )
    write_patterns(mas, [pattern])
    result = _make_result("fix login bug")
    _materialize_proposal(env, result)
    proposed_tasks = list((mas / "tasks" / "proposed").iterdir())
    assert len(proposed_tasks) == 0, "Proposal should be skipped for count >=2"


def test_skips_proposal_matching_terminal_reason_revision_cycles_exhausted(tmp_path):
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())
    pattern = FailurePattern(
        signature="sig_rev_cycles",
        terminal_reason="revision_cycles_exhausted",
        goal_sample="implement user profile",
        count=1,
        last_seen=datetime.now().isoformat(),
        task_ids=["t3"],
        rejected_attempts_sample=[]
    )
    write_patterns(mas, [pattern])
    result = _make_result("implement user profile")
    _materialize_proposal(env, result)
    proposed_tasks = list((mas / "tasks" / "proposed").iterdir())
    assert len(proposed_tasks) == 0, "Proposal should be skipped for revision_cycles_exhausted"


def test_skips_proposal_matching_terminal_reason_max_retries_exceeded(tmp_path):
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())
    pattern = FailurePattern(
        signature="sig_max_retries",
        terminal_reason="max_retries_exceeded",
        goal_sample="add payment gateway",
        count=1,
        last_seen=datetime.now().isoformat(),
        task_ids=["t4"],
        rejected_attempts_sample=[]
    )
    write_patterns(mas, [pattern])
    result = _make_result("add payment gateway")
    _materialize_proposal(env, result)
    proposed_tasks = list((mas / "tasks" / "proposed").iterdir())
    assert len(proposed_tasks) == 0, "Proposal should be skipped for max_retries_exceeded"


def test_skips_proposal_matching_terminal_reason_convergence_detected(tmp_path):
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())
    pattern = FailurePattern(
        signature="sig_convergence",
        terminal_reason="convergence_detected",
        goal_sample="optimize database queries",
        count=1,
        last_seen=datetime.now().isoformat(),
        task_ids=["t5"],
        rejected_attempts_sample=[]
    )
    write_patterns(mas, [pattern])
    result = _make_result("optimize database queries")
    _materialize_proposal(env, result)
    proposed_tasks = list((mas / "tasks" / "proposed").iterdir())
    assert len(proposed_tasks) == 0, "Proposal should be skipped for convergence_detected"


def test_allows_proposal_when_count_1_and_non_terminal_reason(tmp_path):
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())
    pattern = FailurePattern(
        signature="sig_count_1",
        terminal_reason="unknown",
        goal_sample="add dark mode",
        count=1,
        last_seen=datetime.now().isoformat(),
        task_ids=["t6"],
        rejected_attempts_sample=[]
    )
    write_patterns(mas, [pattern])
    result = _make_result("add dark mode")
    _materialize_proposal(env, result)
    proposed_tasks = list((mas / "tasks" / "proposed").iterdir())
    assert len(proposed_tasks) == 1, "Proposal should be materialized for count=1 and non-terminal"


def test_allows_proposal_with_no_matching_pattern(tmp_path):
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())
    pattern = FailurePattern(
        signature="sig_unrelated",
        terminal_reason="unknown",
        goal_sample="unrelated goal here",
        count=2,
        last_seen=datetime.now().isoformat(),
        task_ids=["t7"],
        rejected_attempts_sample=[]
    )
    write_patterns(mas, [pattern])
    result = _make_result("completely different goal")
    _materialize_proposal(env, result)
    proposed_tasks = list((mas / "tasks" / "proposed").iterdir())
    assert len(proposed_tasks) == 1, "Proposal should be materialized with no matching pattern"


def test_allows_proposal_when_no_patterns_file(tmp_path):
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    patterns_file = mas / "patterns.jsonl"
    if patterns_file.exists():
        patterns_file.unlink()
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())
    result = _make_result("another goal")
    _materialize_proposal(env, result)
    proposed_tasks = list((mas / "tasks" / "proposed").iterdir())
    assert len(proposed_tasks) == 1, "Proposal should be materialized when no patterns file"


def test_logs_skip_reason_on_pattern_match(caplog, tmp_path):
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())
    pattern = FailurePattern(
        signature="sig_log",
        terminal_reason="unknown",
        goal_sample="test log goal",
        count=2,
        last_seen=datetime.now().isoformat(),
        task_ids=["t8"],
        rejected_attempts_sample=[]
    )
    write_patterns(mas, [pattern])
    result = _make_result("test log goal")
    with caplog.at_level("INFO", logger="mas.tick"):
        _materialize_proposal(env, result)
    assert any("pattern" in rec.message.lower() for rec in caplog.records), "Log should mention pattern"
    assert any("skip" in rec.message.lower() for rec in caplog.records), "Log should mention skip"


def test_pattern_match_uses_goal_similarity(tmp_path):
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())
    pattern = FailurePattern(
        signature="sig_similarity",
        terminal_reason="unknown",
        goal_sample="fix login bug",
        count=2,
        last_seen=datetime.now().isoformat(),
        task_ids=["t9"],
        rejected_attempts_sample=[]
    )
    write_patterns(mas, [pattern])
    candidate_goal = "fix the login bug"
    sim = goal_similarity(pattern.goal_sample, candidate_goal)
    assert sim >= 0.6, f"Similarity should be >=0.6, got {sim}"
    result = _make_result(candidate_goal)
    _materialize_proposal(env, result)
    proposed_tasks = list((mas / "tasks" / "proposed").iterdir())
    assert len(proposed_tasks) == 0, "Proposal should be skipped due to similar goal"


def test_pattern_filter_runs_before_similarity_dedup(tmp_path):
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())
    pattern = FailurePattern(
        signature="sig_dedup",
        terminal_reason="unknown",
        goal_sample="dedup test goal",
        count=2,
        last_seen=datetime.now().isoformat(),
        task_ids=["t10"],
        rejected_attempts_sample=[]
    )
    write_patterns(mas, [pattern])
    # Create an existing task in proposed/ to trigger similarity dedup if pattern filter didn't run first
    from mas.schemas import Task
    from mas.ids import task_id as make_task_id
    existing_id = make_task_id("dedup test goal", salt="existing")
    existing_dir = mas / "tasks" / "proposed" / existing_id
    existing_dir.mkdir(parents=True, exist_ok=True)
    existing_task = Task(id=existing_id, role="orchestrator", goal="dedup test goal")
    (existing_dir / "task.json").write_text(existing_task.model_dump_json())
    result = _make_result("dedup test goal")
    _materialize_proposal(env, result)
    rejected_files = list((mas / "proposals").glob("rejected.jsonl"))
    assert len(rejected_files) == 0, "rejected.jsonl should not exist when skipped by pattern"
    proposed_tasks = list((mas / "tasks" / "proposed").iterdir())
    assert len(proposed_tasks) == 1, "Only existing task should be present"
