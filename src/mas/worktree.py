from __future__ import annotations

import subprocess
from pathlib import Path


class WorktreeError(RuntimeError):
    pass


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def branch_name(task_id: str) -> str:
    return f"mas/{task_id}"


def _branch_exists(repo: Path, branch: str) -> bool:
    r = _git(repo, "rev-parse", "--verify", f"refs/heads/{branch}", check=False)
    return r.returncode == 0


def create(repo: Path, task_id: str, worktree_path: Path) -> Path:
    """Create a git worktree for the task. Idempotent / recovers from half-creates."""
    branch = branch_name(task_id)
    worktree_path = worktree_path.resolve()

    # Recover from a half-created state: branch exists but worktree dir missing/partial.
    if worktree_path.exists() and not (worktree_path / ".git").exists():
        # Incomplete dir — remove it before retrying.
        import shutil
        shutil.rmtree(worktree_path)

    if worktree_path.exists():
        return worktree_path

    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    if _branch_exists(repo, branch):
        _git(repo, "worktree", "add", str(worktree_path), branch)
    else:
        _git(repo, "worktree", "add", "-b", branch, str(worktree_path))
    return worktree_path


def prune(repo: Path, worktree_path: Path, *, keep_branch: bool = True) -> None:
    if worktree_path.exists():
        _git(repo, "worktree", "remove", "--force", str(worktree_path), check=False)
    _git(repo, "worktree", "prune", check=False)
    if not keep_branch:
        _git(repo, "branch", "-D", branch_name(worktree_path.name), check=False)
