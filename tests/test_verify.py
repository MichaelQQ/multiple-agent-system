from __future__ import annotations

from pathlib import Path

from mas.schemas import Result, SubtaskSpec
from mas.verify import verify_child_result


def _make_logs(child_dir: Path, role: str, attempt: int, body: str) -> None:
    (child_dir / "logs").mkdir(parents=True, exist_ok=True)
    (child_dir / "logs" / f"{role}-{attempt}.log").write_text(body)


def test_verify_passes_through_evaluator(tmp_path: Path):
    spec = SubtaskSpec(id="eval-1", role="evaluator", goal="e")
    r = Result(task_id="x", status="success", summary="ok", verdict="pass")
    out = verify_child_result(spec, r, tmp_path, attempt=1)
    assert out is r


def test_verify_passes_through_failure(tmp_path: Path):
    """Already-failed results are not re-verified."""
    spec = SubtaskSpec(id="t-1", role="tester", goal="t")
    r = Result(task_id="x", status="failure", summary="nope")
    out = verify_child_result(spec, r, tmp_path, attempt=1)
    assert out is r


def test_verify_tester_missing_handoff_fields(tmp_path: Path):
    spec = SubtaskSpec(id="t-1", role="tester", goal="t")
    r = Result(task_id="x", status="success", summary="ok", handoff={})
    out = verify_child_result(spec, r, tmp_path, attempt=1)
    assert out.status == "failure"
    assert "test_command" in out.summary
    assert "initial_exit_code" in out.summary


def test_verify_tester_initial_exit_zero(tmp_path: Path):
    """If tester claims tests are passing already, that's a tester bug."""
    spec = SubtaskSpec(id="t-1", role="tester", goal="t")
    _make_logs(tmp_path, "tester", 1, "$ .venv/bin/pytest tests/test_x.py\nF\n")
    r = Result(task_id="x", status="success", summary="ok",
               handoff={"test_command": "pytest", "initial_exit_code": 0})
    out = verify_child_result(spec, r, tmp_path, attempt=1)
    assert out.status == "failure"
    assert "not failing" in out.summary


def test_verify_tester_log_missing_pytest(tmp_path: Path):
    spec = SubtaskSpec(id="t-1", role="tester", goal="t")
    _make_logs(tmp_path, "tester", 1, "I did static analysis only\n")
    r = Result(task_id="x", status="success", summary="ok",
               handoff={"test_command": "pytest", "initial_exit_code": 1})
    out = verify_child_result(spec, r, tmp_path, attempt=1)
    assert out.status == "failure"
    assert "pytest" in out.summary.lower()


def test_verify_tester_sandbox_blocked_is_env_error(tmp_path: Path):
    spec = SubtaskSpec(id="t-1", role="tester", goal="t")
    _make_logs(tmp_path, "tester", 1,
               "pytest was blocked by the sandbox in this session\n")
    r = Result(task_id="x", status="success", summary="ok",
               handoff={"test_command": "pytest", "initial_exit_code": 1})
    out = verify_child_result(spec, r, tmp_path, attempt=1)
    assert out.status == "environment_error"


def test_verify_tester_happy_path(tmp_path: Path):
    spec = SubtaskSpec(id="t-1", role="tester", goal="t")
    _make_logs(tmp_path, "tester", 1,
               "$ .venv/bin/pytest tests/test_x.py -q\n1 failed in 0.1s\n")
    r = Result(task_id="x", status="success", summary="wrote tests",
               handoff={"test_command": ".venv/bin/pytest", "initial_exit_code": 1})
    out = verify_child_result(spec, r, tmp_path, attempt=1)
    assert out.status == "success"
    assert out is r


def test_verify_implementer_final_exit_nonzero(tmp_path: Path):
    spec = SubtaskSpec(id="i-1", role="implementer", goal="i")
    _make_logs(tmp_path, "implementer", 1, "$ pytest\n1 failed\n")
    r = Result(task_id="x", status="success", summary="ok",
               handoff={"final_exit_code": 1})
    out = verify_child_result(spec, r, tmp_path, attempt=1)
    assert out.status == "failure"
    assert "final_exit_code is 1" in out.summary


def test_verify_implementer_docs_only_passthrough(tmp_path: Path):
    spec = SubtaskSpec(id="docs-1", role="implementer", goal="docs",
                       constraints={"docs_only": True})
    r = Result(task_id="x", status="success", summary="docs updated")
    out = verify_child_result(spec, r, tmp_path, attempt=1)
    assert out.status == "success"
    assert out is r


def test_verify_implementer_happy_path(tmp_path: Path):
    spec = SubtaskSpec(id="i-1", role="implementer", goal="i")
    _make_logs(tmp_path, "implementer", 1,
               "$ .venv/bin/pytest\n10 passed in 0.2s\n")
    r = Result(task_id="x", status="success", summary="impl",
               handoff={"final_exit_code": 0})
    out = verify_child_result(spec, r, tmp_path, attempt=1)
    assert out.status == "success"
    assert out is r


# --- language-agnostic coverage --------------------------------------------

def test_verify_tester_go_test_happy(tmp_path: Path):
    spec = SubtaskSpec(id="t-1", role="tester", goal="t")
    _make_logs(tmp_path, "tester", 1,
               "$ go test ./...\n--- FAIL: TestAdd\nFAIL\nexit status 1\n")
    r = Result(task_id="x", status="success", summary="wrote tests",
               handoff={"test_command": "go test ./...", "initial_exit_code": 1})
    out = verify_child_result(spec, r, tmp_path, attempt=1)
    assert out.status == "success"
    assert out is r


def test_verify_tester_npm_test_happy(tmp_path: Path):
    spec = SubtaskSpec(id="t-1", role="tester", goal="t")
    _make_logs(tmp_path, "tester", 1, "$ npm test\n1 failing\n")
    r = Result(task_id="x", status="success", summary="wrote tests",
               handoff={"test_command": "npm test", "initial_exit_code": 1})
    out = verify_child_result(spec, r, tmp_path, attempt=1)
    assert out.status == "success"
    assert out is r


def test_verify_tester_cargo_test_missing_in_log(tmp_path: Path):
    """Claimed cargo test but log has no 'cargo' mention → coerce."""
    spec = SubtaskSpec(id="t-1", role="tester", goal="t")
    _make_logs(tmp_path, "tester", 1, "only wrote test files, did not run\n")
    r = Result(task_id="x", status="success", summary="wrote tests",
               handoff={"test_command": "cargo test --release", "initial_exit_code": 1})
    out = verify_child_result(spec, r, tmp_path, attempt=1)
    assert out.status == "failure"
    assert "cargo" in out.summary


# --- implementer test re-run verifier --------------------------------------

from mas.verify import verify_implementer_test_rerun


def test_verify_rerun_passthrough_non_implementer(tmp_path: Path):
    spec = SubtaskSpec(id="t-1", role="tester", goal="t")
    r = Result(task_id="x", status="success", summary="ok",
               handoff={"test_command": "true", "initial_exit_code": 1})
    out = verify_implementer_test_rerun(spec, r, tmp_path, "true")
    assert out is r


def test_verify_rerun_passthrough_failure(tmp_path: Path):
    spec = SubtaskSpec(id="i-1", role="implementer", goal="i")
    r = Result(task_id="x", status="failure", summary="nope",
               handoff={"final_exit_code": 1})
    out = verify_implementer_test_rerun(spec, r, tmp_path, "false")
    assert out is r


def test_verify_rerun_passthrough_docs_only(tmp_path: Path):
    spec = SubtaskSpec(id="i-1", role="implementer", goal="docs",
                       constraints={"docs_only": True})
    r = Result(task_id="x", status="success", summary="docs",
               handoff={"final_exit_code": 0})
    out = verify_implementer_test_rerun(spec, r, tmp_path, "false")
    assert out is r


def test_verify_rerun_skipped_without_test_command(tmp_path: Path):
    spec = SubtaskSpec(id="i-1", role="implementer", goal="i")
    r = Result(task_id="x", status="success", summary="impl",
               handoff={"final_exit_code": 0})
    out = verify_implementer_test_rerun(spec, r, tmp_path, None)
    assert out is r


def test_verify_rerun_skipped_when_worktree_missing(tmp_path: Path):
    spec = SubtaskSpec(id="i-1", role="implementer", goal="i")
    r = Result(task_id="x", status="success", summary="impl",
               handoff={"final_exit_code": 0})
    out = verify_implementer_test_rerun(spec, r, tmp_path / "nope", "false")
    assert out is r


def test_verify_rerun_passes_when_command_succeeds(tmp_path: Path):
    spec = SubtaskSpec(id="i-1", role="implementer", goal="i")
    r = Result(task_id="x", status="success", summary="impl",
               handoff={"final_exit_code": 0})
    out = verify_implementer_test_rerun(spec, r, tmp_path, "true")
    assert out.status == "success"
    assert out is r


def test_verify_rerun_coerces_when_command_fails(tmp_path: Path):
    spec = SubtaskSpec(id="i-1", role="implementer", goal="i")
    r = Result(task_id="x", status="success", summary="impl",
               handoff={"final_exit_code": 0})
    out = verify_implementer_test_rerun(spec, r, tmp_path, "false")
    assert out.status == "failure"
    assert "headless re-run" in out.summary
    assert "exit_code=1" in out.summary


def test_verify_rerun_captures_output_tail_on_failure(tmp_path: Path):
    spec = SubtaskSpec(id="i-1", role="implementer", goal="i")
    r = Result(task_id="x", status="success", summary="impl",
               handoff={"final_exit_code": 0})
    out = verify_implementer_test_rerun(
        spec, r, tmp_path,
        "echo MARKER_LINE_42 >&2; exit 7",
    )
    assert out.status == "failure"
    assert "exit_code=7" in out.summary
    assert "MARKER_LINE_42" in out.summary


def test_verify_rerun_runs_in_worktree_cwd(tmp_path: Path):
    spec = SubtaskSpec(id="i-1", role="implementer", goal="i")
    (tmp_path / "marker").write_text("ok")
    r = Result(task_id="x", status="success", summary="impl",
               handoff={"final_exit_code": 0})
    out = verify_implementer_test_rerun(spec, r, tmp_path, "test -f marker")
    assert out.status == "success"
    assert out is r


def test_verify_rerun_timeout_coerces(tmp_path: Path):
    spec = SubtaskSpec(id="i-1", role="implementer", goal="i")
    r = Result(task_id="x", status="success", summary="impl",
               handoff={"final_exit_code": 0})
    out = verify_implementer_test_rerun(spec, r, tmp_path, "sleep 5", timeout_s=1)
    assert out.status == "failure"
    assert "timed out" in out.summary


def test_verify_rerun_skips_when_implementer_claims_nonzero(tmp_path: Path):
    """Implementer that didn't claim 0 is left to verify_child_result; the
    re-run shouldn't fire (and shouldn't accidentally upgrade)."""
    spec = SubtaskSpec(id="i-1", role="implementer", goal="i")
    r = Result(task_id="x", status="success", summary="impl",
               handoff={"final_exit_code": 1})
    out = verify_implementer_test_rerun(spec, r, tmp_path, "true")
    assert out is r


def test_test_command_signature_extraction():
    from mas.verify import _test_command_signature
    assert _test_command_signature("pytest -q") == "pytest"
    assert _test_command_signature(".venv/bin/pytest tests/") == "pytest"
    assert _test_command_signature("go test ./...") == "go"
    assert _test_command_signature("npm test") == "npm"
    assert _test_command_signature("cargo test --release") == "cargo"
    assert _test_command_signature("PYTHONPATH=src pytest -q") == "pytest"


# --- evaluator verification ------------------------------------------------

from mas.verify import verify_evaluator_result


def test_verify_evaluator_no_constraints_passthrough(tmp_path: Path):
    spec = SubtaskSpec(id="e-1", role="evaluator", goal="e")
    r = Result(task_id="x", status="success", summary="ok", verdict="pass")
    out = verify_evaluator_result(spec, r, tmp_path)
    assert out is r


def test_verify_evaluator_required_artifact_missing_downgrades(tmp_path: Path):
    spec = SubtaskSpec(
        id="e-1", role="evaluator", goal="e",
        constraints={"required_artifacts": ["src/mas/audit.py"]},
    )
    r = Result(task_id="x", status="success", summary="lgtm", verdict="pass")
    out = verify_evaluator_result(spec, r, tmp_path)
    assert out.verdict == "needs_revision"
    assert out.status == "needs_revision"
    assert "missing file: src/mas/audit.py" in out.feedback


def test_verify_evaluator_required_artifact_present_pass(tmp_path: Path):
    (tmp_path / "src/mas").mkdir(parents=True)
    (tmp_path / "src/mas/audit.py").write_text("log_event = None\n")
    spec = SubtaskSpec(
        id="e-1", role="evaluator", goal="e",
        constraints={"required_artifacts": ["src/mas/audit.py"]},
    )
    r = Result(task_id="x", status="success", summary="lgtm", verdict="pass")
    out = verify_evaluator_result(spec, r, tmp_path)
    assert out is r


def test_verify_evaluator_required_grep_short_downgrades(tmp_path: Path):
    (tmp_path / "src/mas").mkdir(parents=True)
    (tmp_path / "src/mas/tick.py").write_text(
        "import audit\naudit.log_event(task_dir, 'dispatch')\n"
    )
    spec = SubtaskSpec(
        id="e-1", role="evaluator", goal="e",
        constraints={"required_grep": [
            {"pattern": r"audit\.log_event", "file_glob": "src/mas/*.py", "count_min": 3},
        ]},
    )
    r = Result(task_id="x", status="success", summary="lgtm", verdict="pass")
    out = verify_evaluator_result(spec, r, tmp_path)
    assert out.verdict == "needs_revision"
    assert "found 1, need ≥3" in out.feedback


def test_verify_evaluator_required_grep_met_passes(tmp_path: Path):
    (tmp_path / "src/mas").mkdir(parents=True)
    (tmp_path / "src/mas/tick.py").write_text(
        "audit.log_event(d,'dispatch')\naudit.log_event(d,'completion')\naudit.log_event(d,'error')\n"
    )
    spec = SubtaskSpec(
        id="e-1", role="evaluator", goal="e",
        constraints={"required_grep": [
            {"pattern": r"audit\.log_event", "file_glob": "src/mas/*.py", "count_min": 3},
        ]},
    )
    r = Result(task_id="x", status="success", summary="lgtm", verdict="pass")
    out = verify_evaluator_result(spec, r, tmp_path)
    assert out is r


def test_verify_evaluator_nonpass_verdict_passthrough(tmp_path: Path):
    """Already-failing verdicts are not upgraded by verification."""
    spec = SubtaskSpec(
        id="e-1", role="evaluator", goal="e",
        constraints={"required_artifacts": ["src/mas/audit.py"]},
    )
    r = Result(task_id="x", status="needs_revision", summary="r", verdict="needs_revision")
    out = verify_evaluator_result(spec, r, tmp_path)
    assert out is r


# --- allowed_paths post-hoc enforcement -------------------------------------

import subprocess as _sp

from mas.verify import (
    _path_allowed,
    capture_worktree_baseline,
    verify_allowed_paths,
)


def _init_worktree(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _sp.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    _sp.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    _sp.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    _sp.run(["git", "-C", str(path), "config", "commit.gpgsign", "false"], check=True)
    (path / ".gitkeep").write_text("")
    _sp.run(["git", "-C", str(path), "add", "-A"], check=True)
    _sp.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)


def test_path_allowed_exact():
    assert _path_allowed("src/mas/tick.py", ["src/mas/tick.py"])
    assert not _path_allowed("src/mas/board.py", ["src/mas/tick.py"])


def test_path_allowed_glob():
    assert _path_allowed("src/mas/tick.py", ["src/mas/*.py"])
    assert _path_allowed("README.md", ["*.md"])
    assert not _path_allowed("src/mas/sub/x.py", ["src/mas/*.py"])


def test_path_allowed_directory_prefix():
    assert _path_allowed("src/mas/sub/x.py", ["src/mas/"])
    assert _path_allowed("src/mas/sub/x.py", ["src/mas"])
    assert not _path_allowed("docs/x.md", ["src/mas/"])


def test_verify_allowed_paths_passthrough_no_constraint(tmp_path: Path):
    spec = SubtaskSpec(id="i-1", role="implementer", goal="i")
    r = Result(task_id="x", status="success", summary="ok",
               handoff={"final_exit_code": 0})
    out = verify_allowed_paths(spec, r, tmp_path, tmp_path)
    assert out is r


def test_verify_allowed_paths_passthrough_failure(tmp_path: Path):
    spec = SubtaskSpec(id="i-1", role="implementer", goal="i",
                       constraints={"allowed_paths": ["src/x.py"]})
    r = Result(task_id="x", status="failure", summary="nope")
    out = verify_allowed_paths(spec, r, tmp_path, tmp_path)
    assert out is r


def test_verify_allowed_paths_passthrough_docs_only(tmp_path: Path):
    spec = SubtaskSpec(id="i-1", role="implementer", goal="docs",
                       constraints={"docs_only": True, "allowed_paths": ["src/x.py"]})
    r = Result(task_id="x", status="success", summary="ok")
    out = verify_allowed_paths(spec, r, tmp_path, tmp_path)
    assert out is r


def test_verify_allowed_paths_within_allowlist_passes(tmp_path: Path):
    wt = tmp_path / "wt"
    _init_worktree(wt)
    child_dir = tmp_path / "child"
    child_dir.mkdir()
    capture_worktree_baseline(wt, child_dir)

    (wt / "src").mkdir()
    (wt / "src" / "tick.py").write_text("print('x')\n")

    spec = SubtaskSpec(id="i-1", role="implementer", goal="i",
                       constraints={"allowed_paths": ["src/tick.py"]})
    r = Result(task_id="x", status="success", summary="ok",
               handoff={"final_exit_code": 0})
    out = verify_allowed_paths(spec, r, wt, child_dir)
    assert out.status == "success"
    assert out is r


def test_verify_allowed_paths_outside_allowlist_coerces(tmp_path: Path):
    wt = tmp_path / "wt"
    _init_worktree(wt)
    child_dir = tmp_path / "child"
    child_dir.mkdir()
    capture_worktree_baseline(wt, child_dir)

    (wt / "docs").mkdir()
    (wt / "docs" / "extra.md").write_text("oops\n")

    spec = SubtaskSpec(id="i-1", role="implementer", goal="i",
                       constraints={"allowed_paths": ["src/tick.py"]})
    r = Result(task_id="x", status="success", summary="ok",
               handoff={"final_exit_code": 0})
    out = verify_allowed_paths(spec, r, wt, child_dir)
    assert out.status == "failure"
    assert "outside allowed_paths" in out.summary
    assert "docs/extra.md" in out.summary


def test_verify_allowed_paths_ignores_pre_existing_dirty(tmp_path: Path):
    """Files already dirty at dispatch don't count as this subtask's changes."""
    wt = tmp_path / "wt"
    _init_worktree(wt)
    (wt / "old_dirty.txt").write_text("from a previous subtask\n")

    child_dir = tmp_path / "child"
    child_dir.mkdir()
    capture_worktree_baseline(wt, child_dir)

    (wt / "src").mkdir()
    (wt / "src" / "tick.py").write_text("ok\n")

    spec = SubtaskSpec(id="i-1", role="implementer", goal="i",
                       constraints={"allowed_paths": ["src/tick.py"]})
    r = Result(task_id="x", status="success", summary="ok",
               handoff={"final_exit_code": 0})
    out = verify_allowed_paths(spec, r, wt, child_dir)
    assert out.status == "success"


def test_verify_allowed_paths_missing_worktree(tmp_path: Path):
    spec = SubtaskSpec(id="i-1", role="implementer", goal="i",
                       constraints={"allowed_paths": ["src/x.py"]})
    r = Result(task_id="x", status="success", summary="ok")
    out = verify_allowed_paths(spec, r, tmp_path / "nope", tmp_path)
    assert out is r


def test_verify_allowed_paths_no_baseline_uses_full_dirty(tmp_path: Path):
    """Without a baseline file, every dirty file is attributed to the subtask."""
    wt = tmp_path / "wt"
    _init_worktree(wt)
    (wt / "outside.txt").write_text("nope\n")

    child_dir = tmp_path / "child"
    child_dir.mkdir()

    spec = SubtaskSpec(id="i-1", role="implementer", goal="i",
                       constraints={"allowed_paths": ["src/tick.py"]})
    r = Result(task_id="x", status="success", summary="ok")
    out = verify_allowed_paths(spec, r, wt, child_dir)
    assert out.status == "failure"
    assert "outside.txt" in out.summary


def test_verify_allowed_paths_detects_committed_changes(tmp_path: Path):
    """Files committed between dispatch and result-collection still count."""
    wt = tmp_path / "wt"
    _init_worktree(wt)
    child_dir = tmp_path / "child"
    child_dir.mkdir()
    capture_worktree_baseline(wt, child_dir)

    (wt / "rogue.bin").write_text("data\n")
    _sp.run(["git", "-C", str(wt), "add", "-A"], check=True)
    _sp.run(["git", "-C", str(wt), "commit", "-q", "-m", "intermediate"], check=True)

    spec = SubtaskSpec(id="i-1", role="implementer", goal="i",
                       constraints={"allowed_paths": ["src/x.py"]})
    r = Result(task_id="x", status="success", summary="ok")
    out = verify_allowed_paths(spec, r, wt, child_dir)
    assert out.status == "failure"
    assert "rogue.bin" in out.summary


def test_verify_allowed_paths_directory_prefix_pattern(tmp_path: Path):
    wt = tmp_path / "wt"
    _init_worktree(wt)
    child_dir = tmp_path / "child"
    child_dir.mkdir()
    capture_worktree_baseline(wt, child_dir)

    (wt / "src" / "deep").mkdir(parents=True)
    (wt / "src" / "deep" / "x.py").write_text("ok\n")

    spec = SubtaskSpec(id="i-1", role="implementer", goal="i",
                       constraints={"allowed_paths": ["src/"]})
    r = Result(task_id="x", status="success", summary="ok",
               handoff={"final_exit_code": 0})
    out = verify_allowed_paths(spec, r, wt, child_dir)
    assert out.status == "success"
