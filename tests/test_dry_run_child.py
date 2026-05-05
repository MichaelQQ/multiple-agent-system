"""Tests for the --dry-run-child mode: tick dispatches implementer/tester
with MAS_DRY_RUN=1 and gates the patch they emit before applying."""

from __future__ import annotations

import subprocess as _sp
from pathlib import Path

from mas.schemas import Result, SubtaskSpec
from mas.verify import (
    _DRY_RUN_PATCH_FILENAME,
    _patch_paths_from_numstat,
    apply_proposed_diff,
    verify_child_result,
)


def _init_worktree(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _sp.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    _sp.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    _sp.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    _sp.run(["git", "-C", str(path), "config", "commit.gpgsign", "false"], check=True)
    (path / "src").mkdir(exist_ok=True)
    (path / "src" / "x.py").write_text("def foo():\n    return 0\n")
    _sp.run(["git", "-C", str(path), "add", "-A"], check=True)
    _sp.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)


def _diff(worktree: Path) -> str:
    """Capture working-tree diff vs HEAD (including intent-to-add untracked
    files staged via `git add -N`), then revert so the patch can be cleanly
    re-applied via apply_proposed_diff."""
    out = _sp.run(
        ["git", "-C", str(worktree), "diff", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout
    _sp.run(["git", "-C", str(worktree), "reset", "-q", "HEAD"], check=True)
    _sp.run(["git", "-C", str(worktree), "checkout", "--", "."], check=True)
    _sp.run(["git", "-C", str(worktree), "clean", "-fd", "-q"], check=True)
    return out


# --- _patch_paths_from_numstat ---------------------------------------------


def test_patch_paths_plain():
    out = "1\t1\tsrc/x.py\n2\t0\tdocs/y.md\n"
    assert _patch_paths_from_numstat(out) == ["src/x.py", "docs/y.md"]


def test_patch_paths_rename_braced():
    out = "0\t0\tsrc/{old.py => new.py}\n"
    assert sorted(_patch_paths_from_numstat(out)) == ["src/new.py", "src/old.py"]


def test_patch_paths_rename_plain():
    out = "0\t0\tsrc/old.py => src/new.py\n"
    assert sorted(_patch_paths_from_numstat(out)) == ["src/new.py", "src/old.py"]


# --- apply_proposed_diff: pass-through cases -------------------------------


def test_apply_passthrough_non_success(tmp_path: Path):
    wt = tmp_path / "wt"
    _init_worktree(wt)
    child_dir = tmp_path / "child"
    child_dir.mkdir()
    spec = SubtaskSpec(id="i-1", role="implementer", goal="i")
    r = Result(task_id="x", status="failure", summary="nope")
    out = apply_proposed_diff(spec, r, wt, child_dir)
    assert out is r


def test_apply_passthrough_non_mutating_role(tmp_path: Path):
    wt = tmp_path / "wt"
    _init_worktree(wt)
    child_dir = tmp_path / "child"
    child_dir.mkdir()
    spec = SubtaskSpec(id="e-1", role="evaluator", goal="e")
    r = Result(task_id="x", status="success", summary="ok", verdict="pass")
    out = apply_proposed_diff(spec, r, wt, child_dir)
    assert out is r


def test_apply_passthrough_docs_only(tmp_path: Path):
    wt = tmp_path / "wt"
    _init_worktree(wt)
    child_dir = tmp_path / "child"
    child_dir.mkdir()
    spec = SubtaskSpec(id="i-1", role="implementer", goal="docs",
                       constraints={"docs_only": True})
    r = Result(task_id="x", status="success", summary="docs")
    out = apply_proposed_diff(spec, r, wt, child_dir)
    assert out is r


# --- apply_proposed_diff: failure cases ------------------------------------


def test_apply_missing_patch_coerces(tmp_path: Path):
    """Implementer claims success but no proposed_diff.patch → failure."""
    wt = tmp_path / "wt"
    _init_worktree(wt)
    child_dir = tmp_path / "child"
    child_dir.mkdir()
    spec = SubtaskSpec(id="i-1", role="implementer", goal="i")
    r = Result(task_id="x", status="success", summary="impl",
               handoff={"final_exit_code": 0})
    out = apply_proposed_diff(spec, r, wt, child_dir)
    assert out.status == "failure"
    assert _DRY_RUN_PATCH_FILENAME in out.summary


def test_apply_malformed_patch_coerces(tmp_path: Path):
    wt = tmp_path / "wt"
    _init_worktree(wt)
    child_dir = tmp_path / "child"
    child_dir.mkdir()
    (child_dir / _DRY_RUN_PATCH_FILENAME).write_text("not a real diff at all\n")

    spec = SubtaskSpec(id="i-1", role="implementer", goal="i")
    r = Result(task_id="x", status="success", summary="impl",
               handoff={"final_exit_code": 0})
    out = apply_proposed_diff(spec, r, wt, child_dir)
    assert out.status == "failure"
    assert "git apply --check" in out.summary


def test_apply_outside_allowed_paths_coerces(tmp_path: Path):
    wt = tmp_path / "wt"
    _init_worktree(wt)
    child_dir = tmp_path / "child"
    child_dir.mkdir()

    # Stage a change that touches both an allowed and a disallowed path,
    # then capture the diff.
    (wt / "src" / "x.py").write_text("def foo():\n    return 1\n")
    (wt / "rogue.py").write_text("nope\n")
    _sp.run(["git", "-C", str(wt), "add", "-N", "rogue.py"], check=True)
    patch = _diff(wt)
    assert "rogue.py" in patch and "src/x.py" in patch
    (child_dir / _DRY_RUN_PATCH_FILENAME).write_text(patch)

    spec = SubtaskSpec(id="i-1", role="implementer", goal="i",
                       constraints={"allowed_paths": ["src/"]})
    r = Result(task_id="x", status="success", summary="impl",
               handoff={"final_exit_code": 0})
    out = apply_proposed_diff(spec, r, wt, child_dir)
    assert out.status == "failure"
    assert "rogue.py" in out.summary
    # Worktree must remain unchanged when the gate rejects.
    assert (wt / "src" / "x.py").read_text() == "def foo():\n    return 0\n"
    assert not (wt / "rogue.py").exists()


def test_apply_worktree_missing_coerces(tmp_path: Path):
    child_dir = tmp_path / "child"
    child_dir.mkdir()
    (child_dir / _DRY_RUN_PATCH_FILENAME).write_text("anything\n")
    spec = SubtaskSpec(id="i-1", role="implementer", goal="i")
    r = Result(task_id="x", status="success", summary="impl")
    out = apply_proposed_diff(spec, r, tmp_path / "missing", child_dir)
    assert out.status == "failure"
    assert "worktree missing" in out.summary


# --- apply_proposed_diff: success cases ------------------------------------


def test_apply_clean_patch_succeeds(tmp_path: Path):
    wt = tmp_path / "wt"
    _init_worktree(wt)
    child_dir = tmp_path / "child"
    child_dir.mkdir()

    (wt / "src" / "x.py").write_text("def foo():\n    return 42\n")
    patch = _diff(wt)
    (child_dir / _DRY_RUN_PATCH_FILENAME).write_text(patch)

    spec = SubtaskSpec(id="i-1", role="implementer", goal="i",
                       constraints={"allowed_paths": ["src/"]})
    r = Result(task_id="x", status="success", summary="impl",
               handoff={"final_exit_code": 0})
    out = apply_proposed_diff(spec, r, wt, child_dir)
    assert out.status == "success"
    assert out is r
    # Patch was actually applied to the worktree.
    assert (wt / "src" / "x.py").read_text() == "def foo():\n    return 42\n"


def test_apply_within_allowlist_passes_for_tester(tmp_path: Path):
    wt = tmp_path / "wt"
    _init_worktree(wt)
    child_dir = tmp_path / "child"
    child_dir.mkdir()

    (wt / "tests").mkdir()
    (wt / "tests" / "test_x.py").write_text("def test_foo():\n    assert False\n")
    _sp.run(["git", "-C", str(wt), "add", "-N", "tests/test_x.py"], check=True)
    patch = _diff(wt)
    (child_dir / _DRY_RUN_PATCH_FILENAME).write_text(patch)

    spec = SubtaskSpec(id="t-1", role="tester", goal="t",
                       constraints={"allowed_paths": ["tests/"]})
    r = Result(task_id="x", status="success", summary="wrote tests",
               handoff={"test_command": "pytest", "initial_exit_code": 1})
    out = apply_proposed_diff(spec, r, wt, child_dir)
    assert out.status == "success"
    assert (wt / "tests" / "test_x.py").exists()


def test_apply_no_allowed_paths_constraint_passes(tmp_path: Path):
    """Without allowed_paths, only the parse check gates apply."""
    wt = tmp_path / "wt"
    _init_worktree(wt)
    child_dir = tmp_path / "child"
    child_dir.mkdir()

    (wt / "anywhere.py").write_text("ok\n")
    _sp.run(["git", "-C", str(wt), "add", "-N", "anywhere.py"], check=True)
    patch = _diff(wt)
    (child_dir / _DRY_RUN_PATCH_FILENAME).write_text(patch)

    spec = SubtaskSpec(id="i-1", role="implementer", goal="i")
    r = Result(task_id="x", status="success", summary="impl",
               handoff={"final_exit_code": 0})
    out = apply_proposed_diff(spec, r, wt, child_dir)
    assert out.status == "success"
    assert (wt / "anywhere.py").exists()


# --- verify_child_result dry_run flag --------------------------------------


def test_verify_child_result_dry_run_skips_log_mention(tmp_path: Path):
    """In dry-run mode the agent does not run the test command, so the
    log-mentions check would false-coerce a valid result. dry_run=True
    suppresses it."""
    spec = SubtaskSpec(id="i-1", role="implementer", goal="i")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "implementer-1.log").write_text(
        "Generated unified diff; did not run tests.\n"
    )
    r = Result(task_id="x", status="success", summary="impl",
               handoff={"final_exit_code": 0, "test_command": "pytest"})
    out = verify_child_result(spec, r, tmp_path, attempt=1, dry_run=True)
    assert out.status == "success"
    assert out is r


def test_verify_child_result_non_dry_run_still_enforces(tmp_path: Path):
    """Sanity: with dry_run=False the log-mentions check still applies."""
    spec = SubtaskSpec(id="i-1", role="implementer", goal="i")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "implementer-1.log").write_text(
        "did not run tests, just thought about them\n"
    )
    r = Result(task_id="x", status="success", summary="impl",
               handoff={"final_exit_code": 0, "test_command": "pytest"})
    out = verify_child_result(spec, r, tmp_path, attempt=1, dry_run=False)
    assert out.status == "failure"


# --- _dispatch_role plumbing of MAS_DRY_RUN + dry_run_block ----------------


def test_dispatch_role_sets_mas_dry_run_env_for_implementer(tmp_path: Path):
    """When TickEnv.dry_run_child=True and role is implementer, the dispatched
    subprocess receives MAS_DRY_RUN=1 and the prompt carries the dry-run
    addendum block."""
    from unittest.mock import patch

    from mas import board
    from mas.adapters import get_adapter
    from mas.adapters.base import DispatchHandle
    from mas.schemas import MasConfig, ProviderConfig, RoleConfig, Task
    from mas.tick import TickEnv, _dispatch_role

    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    (mas / "prompts").mkdir(exist_ok=True)
    (mas / "prompts" / "implementer.md").write_text(
        "goal=$goal\nDRY_RUN_BLOCK_START\n$dry_run_block\nDRY_RUN_BLOCK_END\n"
    )
    cfg = MasConfig(
        providers={"mock": ProviderConfig(cli="sh", max_concurrent=4)},
        roles={
            r: RoleConfig(provider="mock", max_retries=2)
            for r in ("proposer", "orchestrator", "implementer", "tester", "evaluator")
        },
    )
    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg, dry_run_child=True)

    task_dir_ = board.task_dir(mas, "doing", "20260415-t1-aaaa")
    task_dir_.mkdir(parents=True)
    task = Task(id="20260415-t1-aaaa", role="implementer", goal="do thing")

    captured: list[dict] = []

    def fake_dispatch(self, prompt, task_dir, cwd, log_path, role,
                      stdin_text=None, extra_env=None, **_):
        captured.append({"prompt": prompt, "extra_env": extra_env})
        return DispatchHandle(pid=42, provider="mock", role=role,
                              task_dir=task_dir, log_path=log_path)

    adapter_cls = get_adapter("mock")
    with patch.object(adapter_cls, "dispatch", fake_dispatch):
        _dispatch_role(env, task, task_dir_, tmp_path, role="implementer")

    assert captured, "dispatch was not called"
    assert captured[0]["extra_env"] == {"MAS_DRY_RUN": "1"}
    assert "MAS_DRY_RUN" in captured[0]["prompt"]
    assert "proposed_diff.patch" in captured[0]["prompt"]


def test_dispatch_role_no_dry_run_for_evaluator(tmp_path: Path):
    """Even with TickEnv.dry_run_child=True, non-mutating roles like evaluator
    do NOT get MAS_DRY_RUN=1 — the gate is scoped to implementer/tester only."""
    from unittest.mock import patch

    from mas import board
    from mas.adapters import get_adapter
    from mas.adapters.base import DispatchHandle
    from mas.schemas import MasConfig, ProviderConfig, RoleConfig, Task
    from mas.tick import TickEnv, _dispatch_role

    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    (mas / "prompts").mkdir(exist_ok=True)
    (mas / "prompts" / "evaluator.md").write_text("goal=$goal\n$dry_run_block\n")
    cfg = MasConfig(
        providers={"mock": ProviderConfig(cli="sh", max_concurrent=4)},
        roles={
            r: RoleConfig(provider="mock", max_retries=2)
            for r in ("proposer", "orchestrator", "implementer", "tester", "evaluator")
        },
    )
    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg, dry_run_child=True)

    task_dir_ = board.task_dir(mas, "doing", "20260415-t2-aaaa")
    task_dir_.mkdir(parents=True)
    task = Task(id="20260415-t2-aaaa", role="evaluator", goal="evaluate")

    captured: list[dict] = []

    def fake_dispatch(self, prompt, task_dir, cwd, log_path, role,
                      stdin_text=None, extra_env=None, **_):
        captured.append({"prompt": prompt, "extra_env": extra_env})
        return DispatchHandle(pid=42, provider="mock", role=role,
                              task_dir=task_dir, log_path=log_path)

    adapter_cls = get_adapter("mock")
    with patch.object(adapter_cls, "dispatch", fake_dispatch):
        _dispatch_role(env, task, task_dir_, tmp_path, role="evaluator")

    assert captured, "dispatch was not called"
    assert captured[0]["extra_env"] is None
    assert "MAS_DRY_RUN" not in captured[0]["prompt"]
