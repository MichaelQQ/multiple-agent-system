"""Failing tests for `mas pr <task-id>`.

Each test encodes expected behaviour and MUST fail until the real implementation
is written.  The stub in src/mas/cli.py raises NotImplementedError, which gives
exit_code=1 with no output — every assertion about exit codes, output content,
or subprocess call patterns will therefore fail with AssertionError (semantic),
not with import/fixture/harness errors.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mas import board
from mas.cli import app
from mas.schemas import Result, Task

runner = CliRunner()

TASK_ID = "20260101-test-pr-abcd"
BRANCH = f"mas/{TASK_ID}"
PR_URL = "https://github.com/org/repo/pull/42"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_board(tmp_path, monkeypatch):
    """Isolated .mas/ layout with project_dir patched to the temp dir."""
    mas_dir = tmp_path / ".mas"
    board.ensure_layout(mas_dir)
    monkeypatch.setattr("mas.cli.project_dir", lambda *args: mas_dir)
    monkeypatch.setattr("mas.cli.project_root", lambda *args: tmp_path)
    return mas_dir


def _write_done_task(
    mas_dir: Path,
    task_id: str = TASK_ID,
    goal: str = "Implement feature X for the dashboard",
    summary: str = "Implementation complete with all tests passing",
    feedback: str = "Good implementation",
    cost_usd: float = 0.05,
    tokens_in: int = 1000,
    tokens_out: int = 500,
) -> Path:
    task_dir = mas_dir / "tasks" / "done" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.json").write_text(
        Task(id=task_id, role="implementer", goal=goal).model_dump_json(indent=2)
    )
    (task_dir / "result.json").write_text(
        Result(
            task_id=task_id,
            status="success",
            summary=summary,
            verdict="pass",
            feedback=feedback,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            duration_s=120.0,
        ).model_dump_json(indent=2)
    )
    return task_dir


def _cp(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _make_dispatch(
    branch: str = BRANCH,
    branch_on_remote: bool = True,
    default_branch: str = "main",
    pr_url: str = PR_URL,
    push_calls: list | None = None,
    gh_create_calls: list | None = None,
):
    """Return a subprocess.run stub for the standard happy-path scenario."""

    def _dispatch(cmd, **kwargs):
        c = list(cmd)
        if c[:3] == ["gh", "auth", "status"]:
            return _cp()
        if c[:3] == ["git", "branch", "--list"]:
            return _cp(stdout=f"  {branch}\n")
        if c[:4] == ["git", "ls-remote", "--exit-code", "--heads"]:
            return _cp(returncode=0 if branch_on_remote else 2)
        if c[:4] == ["git", "push", "-u", "origin"]:
            if push_calls is not None:
                push_calls.append(c)
            return _cp()
        if c[:3] == ["gh", "repo", "view"]:
            return _cp(stdout=json.dumps({"defaultBranchRef": {"name": default_branch}}))
        if c[:3] == ["gh", "pr", "create"]:
            if gh_create_calls is not None:
                gh_create_calls.append(c)
            return _cp(stdout=pr_url + "\n")
        raise AssertionError(f"Unexpected subprocess.run call: {c!r}")

    return _dispatch


# ---------------------------------------------------------------------------
# (g) gh not installed → exit 2
# ---------------------------------------------------------------------------


def test_gh_not_installed(tmp_board, monkeypatch):
    """Exit 2 + message with https://cli.github.com/ when gh is absent from PATH."""
    _write_done_task(tmp_board)
    monkeypatch.setattr("shutil.which", lambda name: None)

    result = runner.invoke(app, ["pr", TASK_ID])

    assert result.exit_code == 2
    assert "https://cli.github.com/" in result.output


# ---------------------------------------------------------------------------
# (h) gh auth status fails → exit 2
# ---------------------------------------------------------------------------


def test_gh_auth_status_fails(tmp_board, monkeypatch):
    """Exit 2 + tells operator to run 'gh auth login' when auth check fails."""
    _write_done_task(tmp_board)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    def _dispatch(cmd, **kwargs):
        c = list(cmd)
        if c[:3] == ["gh", "auth", "status"]:
            return _cp(returncode=1, stderr="You are not logged into any GitHub hosts.")
        raise AssertionError(f"Unexpected call after auth failure: {c!r}")

    monkeypatch.setattr("subprocess.run", _dispatch)

    result = runner.invoke(app, ["pr", TASK_ID])

    assert result.exit_code == 2
    assert "gh auth login" in result.output


# ---------------------------------------------------------------------------
# (c) task missing or in wrong column
# ---------------------------------------------------------------------------


def test_task_not_found(tmp_board, monkeypatch):
    """Exit 1 + 'not found' message mentioning the task id; no subprocess calls made."""
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    def _no_gh(cmd, **kwargs):
        raise AssertionError(f"gh must not be called for missing task: {list(cmd)!r}")

    monkeypatch.setattr("subprocess.run", _no_gh)

    result = runner.invoke(app, ["pr", "20260101-missing-ffff"])

    assert result.exit_code == 1
    assert "not found" in result.output.lower()
    assert "20260101-missing-ffff" in result.output


def test_task_in_proposed(tmp_board, monkeypatch):
    """Exit 1 when task is in proposed/ instead of done/."""
    td = tmp_board / "tasks" / "proposed" / TASK_ID
    td.mkdir(parents=True, exist_ok=True)
    (td / "task.json").write_text(
        Task(id=TASK_ID, role="implementer", goal="do something").model_dump_json(indent=2)
    )
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    result = runner.invoke(app, ["pr", TASK_ID])

    assert result.exit_code == 1
    combined = result.output.lower()
    assert "done" in combined or "wrong" in combined or "proposed" in combined


def test_task_in_doing(tmp_board, monkeypatch):
    """Exit 1 when task is in doing/."""
    td = tmp_board / "tasks" / "doing" / TASK_ID
    td.mkdir(parents=True, exist_ok=True)
    (td / "task.json").write_text(
        Task(id=TASK_ID, role="implementer", goal="do something").model_dump_json(indent=2)
    )
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    result = runner.invoke(app, ["pr", TASK_ID])

    assert result.exit_code == 1
    combined = result.output.lower()
    assert "done" in combined or "wrong" in combined or "doing" in combined


def test_task_in_failed(tmp_board, monkeypatch):
    """Exit 1 when task is in failed/."""
    td = tmp_board / "tasks" / "failed" / TASK_ID
    td.mkdir(parents=True, exist_ok=True)
    (td / "task.json").write_text(
        Task(id=TASK_ID, role="implementer", goal="do something").model_dump_json(indent=2)
    )
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    result = runner.invoke(app, ["pr", TASK_ID])

    assert result.exit_code == 1
    combined = result.output.lower()
    assert "done" in combined or "wrong" in combined or "failed" in combined


# ---------------------------------------------------------------------------
# (d) result.json missing in done/<id>/
# ---------------------------------------------------------------------------


def test_result_json_missing(tmp_board, monkeypatch):
    """Exit 1 + mentions 'result' when done/<id>/result.json is absent."""
    td = tmp_board / "tasks" / "done" / TASK_ID
    td.mkdir(parents=True, exist_ok=True)
    (td / "task.json").write_text(
        Task(id=TASK_ID, role="implementer", goal="do something").model_dump_json(indent=2)
    )
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    result = runner.invoke(app, ["pr", TASK_ID])

    assert result.exit_code == 1
    assert "result" in result.output.lower()


# ---------------------------------------------------------------------------
# (e) local branch mas/<task-id> missing
# ---------------------------------------------------------------------------


def test_local_branch_missing(tmp_board, monkeypatch):
    """Exit 1 + message mentioning the branch when local branch is absent."""
    _write_done_task(tmp_board)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    def _dispatch(cmd, **kwargs):
        c = list(cmd)
        if c[:3] == ["gh", "auth", "status"]:
            return _cp()
        if c[:3] == ["git", "branch", "--list"]:
            return _cp(stdout="")  # branch absent
        raise AssertionError(f"Should not reach past missing-branch check: {c!r}")

    monkeypatch.setattr("subprocess.run", _dispatch)

    result = runner.invoke(app, ["pr", TASK_ID])

    assert result.exit_code == 1
    assert BRANCH in result.output or "branch" in result.output.lower()


# ---------------------------------------------------------------------------
# (f) branch not on origin → git push -u origin <branch>, never --force
# ---------------------------------------------------------------------------


def test_push_when_branch_not_on_origin(tmp_board, monkeypatch):
    """git push -u origin mas/<id> called exactly once; --force is never used."""
    _write_done_task(tmp_board)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    push_calls: list[list[str]] = []
    gh_calls: list[list[str]] = []
    monkeypatch.setattr(
        "subprocess.run",
        _make_dispatch(branch_on_remote=False, push_calls=push_calls, gh_create_calls=gh_calls),
    )

    result = runner.invoke(app, ["pr", TASK_ID])

    assert result.exit_code == 0
    assert len(push_calls) == 1
    assert push_calls[0] == ["git", "push", "-u", "origin", BRANCH]
    assert "--force" not in push_calls[0]
    assert len(gh_calls) == 1  # gh pr create still called


# ---------------------------------------------------------------------------
# (a) happy path
# ---------------------------------------------------------------------------


def test_happy_path(tmp_board, monkeypatch):
    """Exit 0, PR URL on stdout, gh pr create has correct --head/--base/--title/--body."""
    _write_done_task(tmp_board)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    gh_calls: list[list[str]] = []
    monkeypatch.setattr(
        "subprocess.run",
        _make_dispatch(pr_url=PR_URL, gh_create_calls=gh_calls),
    )

    result = runner.invoke(app, ["pr", TASK_ID])

    assert result.exit_code == 0
    assert PR_URL in result.output

    assert len(gh_calls) == 1
    cmd = gh_calls[0]
    assert "--head" in cmd
    assert cmd[cmd.index("--head") + 1] == BRANCH
    assert "--base" in cmd
    assert cmd[cmd.index("--base") + 1] == "main"  # resolved via gh repo view
    assert "--title" in cmd
    # title uses result.summary when non-empty (goal m)
    assert cmd[cmd.index("--title") + 1] == "Implementation complete with all tests passing"
    assert "--body" in cmd


# ---------------------------------------------------------------------------
# (b) optional flags forwarded to gh pr create
# ---------------------------------------------------------------------------


def test_draft_flag_forwarded(tmp_board, monkeypatch):
    """--draft appears in gh pr create invocation."""
    _write_done_task(tmp_board)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    gh_calls: list[list[str]] = []
    monkeypatch.setattr("subprocess.run", _make_dispatch(gh_create_calls=gh_calls))

    result = runner.invoke(app, ["pr", TASK_ID, "--draft"])

    assert result.exit_code == 0
    assert len(gh_calls) == 1
    assert "--draft" in gh_calls[0]


def test_custom_base_flag(tmp_board, monkeypatch):
    """--base develop overrides default branch lookup and is forwarded to gh."""
    _write_done_task(tmp_board)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    gh_calls: list[list[str]] = []

    def _dispatch(cmd, **kwargs):
        c = list(cmd)
        if c[:3] == ["gh", "auth", "status"]:
            return _cp()
        if c[:3] == ["git", "branch", "--list"]:
            return _cp(stdout=f"  {BRANCH}\n")
        if c[:4] == ["git", "ls-remote", "--exit-code", "--heads"]:
            return _cp()
        if c[:3] == ["gh", "repo", "view"]:
            # Allowed but should not be needed when --base is explicit
            return _cp(stdout=json.dumps({"defaultBranchRef": {"name": "main"}}))
        if c[:3] == ["gh", "pr", "create"]:
            gh_calls.append(c)
            return _cp(stdout="https://github.com/org/repo/pull/44\n")
        raise AssertionError(f"Unexpected: {c!r}")

    monkeypatch.setattr("subprocess.run", _dispatch)

    result = runner.invoke(app, ["pr", TASK_ID, "--base", "develop"])

    assert result.exit_code == 0
    assert len(gh_calls) == 1
    cmd = gh_calls[0]
    assert "--base" in cmd
    assert cmd[cmd.index("--base") + 1] == "develop"


def test_reviewer_flags_forwarded(tmp_board, monkeypatch):
    """--reviewer alice --reviewer bob each appear as --reviewer <value> in gh call."""
    _write_done_task(tmp_board)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    gh_calls: list[list[str]] = []
    monkeypatch.setattr("subprocess.run", _make_dispatch(gh_create_calls=gh_calls))

    result = runner.invoke(app, ["pr", TASK_ID, "--reviewer", "alice", "--reviewer", "bob"])

    assert result.exit_code == 0
    assert len(gh_calls) == 1
    cmd = gh_calls[0]
    reviewer_values = [cmd[i + 1] for i, v in enumerate(cmd) if v == "--reviewer"]
    assert "alice" in reviewer_values
    assert "bob" in reviewer_values


# ---------------------------------------------------------------------------
# (i) gh pr create reports existing PR in stderr
# ---------------------------------------------------------------------------


def test_existing_pr_detected(tmp_board, monkeypatch):
    """Exit 0 + existing URL on stdout when gh pr create exits non-zero with a PR URL in stderr."""
    _write_done_task(tmp_board)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    existing_url = "https://github.com/org/repo/pull/99"

    def _dispatch(cmd, **kwargs):
        c = list(cmd)
        if c[:3] == ["gh", "auth", "status"]:
            return _cp()
        if c[:3] == ["git", "branch", "--list"]:
            return _cp(stdout=f"  {BRANCH}\n")
        if c[:4] == ["git", "ls-remote", "--exit-code", "--heads"]:
            return _cp()
        if c[:3] == ["gh", "repo", "view"]:
            return _cp(stdout=json.dumps({"defaultBranchRef": {"name": "main"}}))
        if c[:3] == ["gh", "pr", "create"]:
            return _cp(
                returncode=1,
                stderr=(
                    f"a pull request for branch '{BRANCH}' into 'main' already exists:\n"
                    f"{existing_url}\n"
                ),
            )
        raise AssertionError(f"Unexpected: {c!r}")

    monkeypatch.setattr("subprocess.run", _dispatch)

    result = runner.invoke(app, ["pr", TASK_ID])

    assert result.exit_code == 0
    assert existing_url in result.output


# ---------------------------------------------------------------------------
# (j) PR title falls back to goal (truncated to 70 chars) when summary empty
# ---------------------------------------------------------------------------


def test_title_fallback_to_goal_when_summary_empty(tmp_board, monkeypatch):
    """Title = goal[:70] when result.summary is an empty string."""
    long_goal = "A" * 100
    td = tmp_board / "tasks" / "done" / TASK_ID
    td.mkdir(parents=True, exist_ok=True)
    (td / "task.json").write_text(
        Task(id=TASK_ID, role="implementer", goal=long_goal).model_dump_json(indent=2)
    )
    (td / "result.json").write_text(
        Result(task_id=TASK_ID, status="success", summary="", verdict="pass").model_dump_json(
            indent=2
        )
    )
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    gh_calls: list[list[str]] = []
    monkeypatch.setattr("subprocess.run", _make_dispatch(gh_create_calls=gh_calls))

    result = runner.invoke(app, ["pr", TASK_ID])

    assert result.exit_code == 0
    assert len(gh_calls) == 1
    cmd = gh_calls[0]
    title = cmd[cmd.index("--title") + 1]
    assert title == long_goal[:70]
    assert len(title) <= 70


# ---------------------------------------------------------------------------
# (k) PR body contains all required sections
# ---------------------------------------------------------------------------


def test_pr_body_contains_required_sections(tmp_board, monkeypatch):
    """PR body includes goal, evaluator summary, feedback, cost, tokens, footer."""
    goal = "Implement feature X for the dashboard"
    _write_done_task(
        tmp_board,
        goal=goal,
        summary="Implementation complete with all tests passing",
        feedback="Good implementation",
        cost_usd=0.05,
        tokens_in=1000,
        tokens_out=500,
    )
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    gh_calls: list[list[str]] = []
    monkeypatch.setattr("subprocess.run", _make_dispatch(gh_create_calls=gh_calls))

    result = runner.invoke(app, ["pr", TASK_ID])

    assert result.exit_code == 0
    assert len(gh_calls) == 1
    cmd = gh_calls[0]
    body = cmd[cmd.index("--body") + 1]

    assert goal in body
    assert "Implementation complete with all tests passing" in body
    assert "Good implementation" in body
    assert "0.05" in body   # cost_usd
    assert "1000" in body   # tokens_in
    assert "500" in body    # tokens_out
    assert "Generated by mas" in body
