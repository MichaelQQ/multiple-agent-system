"""Tests for mas.worktree module - uses real git repos (no mocking)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mas import worktree as wt


class TestWorktreeError:
    """Test WorktreeError exception behavior."""

    def test_worktree_error_is_runtime_error_subclass(self):
        """WorktreeError should be a RuntimeError subclass."""
        assert issubclass(wt.WorktreeError, RuntimeError)

    def test_worktree_error_can_be_raised_and_caught(self):
        """WorktreeError can be raised and caught as RuntimeError."""
        with pytest.raises(wt.WorktreeError):
            raise wt.WorktreeError("test error")


class TestGit:
    """Test _git helper function."""

    def test_git_returns_completed_process(self, git_repo):
        """_git returns a CompletedProcess object."""
        result = wt._git(git_repo, "rev-parse", "--is-inside-work-tree")
        assert isinstance(result, subprocess.CompletedProcess)

    def test_git_check_true_raises_on_failure(self, git_repo):
        """_git raises CalledProcessError when check=True and command fails."""
        with pytest.raises(subprocess.CalledProcessError):
            wt._git(git_repo, "nonexistent-command", check=True)

    def test_git_check_false_returns_nonzero_on_failure(self, git_repo):
        """_git returns non-zero returncode without raising when check=False."""
        result = wt._git(git_repo, "nonexistent-command", check=False)
        assert result.returncode != 0


class TestBranchName:
    """Test branch_name function."""

    def test_branch_name_simple_task_id(self):
        """branch_name returns mas/{task_id} for simple task IDs."""
        assert wt.branch_name("test-1") == "mas/test-1"

    def test_branch_name_complex_task_id(self):
        """branch_name returns correct format for complex task IDs."""
        assert wt.branch_name("20260416-create-a-complete-test-suite-for-6594") == "mas/20260416-create-a-complete-test-suite-for-6594"

    def test_branch_name_with_dashes(self):
        """branch_name handles task IDs with dashes."""
        assert wt.branch_name("test-123-abc") == "mas/test-123-abc"


class TestBranchExists:
    """Test _branch_exists function."""

    def test_branch_exists_returns_true_for_existing_branch(self, git_repo):
        """_branch_exists returns True for existing branch."""
        subprocess.run(["git", "-C", str(git_repo), "checkout", "-b", "mas/test-1"], check=True, capture_output=True)
        assert wt._branch_exists(git_repo, "mas/test-1") is True

    def test_branch_exists_returns_false_for_missing_branch(self, git_repo):
        """_branch_exists returns False for missing branch."""
        assert wt._branch_exists(git_repo, "mas/nonexistent-branch") is False

    def test_branch_exists_returns_false_for_nonexistent(self, git_repo):
        """_branch_exists returns False for nonexistent branches."""
        assert wt._branch_exists(git_repo, "nonexistent-branch-foo-bar") is False
        assert wt._branch_exists(git_repo, "another-nonexistent") is False


class TestCreate:
    """Test create function."""

    def test_create_creates_worktree_at_specified_path(self, git_repo, tmp_path):
        """create creates worktree at specified path."""
        worktree_path = tmp_path / "worktrees" / "test-1"
        result = wt.create(git_repo, "test-1", worktree_path)
        assert result == worktree_path
        assert worktree_path.exists()

    def test_create_creates_branch_with_correct_name(self, git_repo, tmp_path):
        """create creates worktree with branch mas/{task_id}."""
        worktree_path = tmp_path / "worktrees" / "test-1"
        wt.create(git_repo, "test-1", worktree_path)
        result = subprocess.run(
            ["git", "-C", str(git_repo), "branch", "--list"],
            capture_output=True, text=True
        )
        assert "mas/test-1" in result.stdout

    def test_create_idempotent_returns_same_path(self, git_repo, tmp_path):
        """create is idempotent: calling twice returns same path without error."""
        worktree_path = tmp_path / "worktrees" / "test-1"
        result1 = wt.create(git_repo, "test-1", worktree_path)
        result2 = wt.create(git_repo, "test-1", worktree_path)
        assert result1 == result2
        assert result2 == worktree_path

    def test_create_recovery_removes_and_recreates(self, git_repo, tmp_path):
        """create removes and recreates if worktree_path exists but has no .git."""
        worktree_path = tmp_path / "worktrees" / "test-1"
        worktree_path.mkdir(parents=True)
        (worktree_path / "somefile").write_text("existing content")
        result = wt.create(git_repo, "test-1", worktree_path)
        assert result == worktree_path
        assert worktree_path.exists()
        assert (worktree_path / ".git").exists()

    def test_create_creates_parent_directories(self, git_repo, tmp_path):
        """create creates parent directories if missing."""
        worktree_path = tmp_path / "deep" / "nested" / "path" / "test-1"
        result = wt.create(git_repo, "test-1", worktree_path)
        assert worktree_path.parent.exists()
        assert result == worktree_path

    def test_create_returns_existing_path_early(self, git_repo, tmp_path):
        """create returns early if worktree_path already exists (even without proper .git)."""
        worktree_path = tmp_path / "worktrees" / "test-1"
        worktree_path.mkdir(parents=True)
        (worktree_path / "somefile").write_text("existing content")
        result = wt.create(git_repo, "test-1", worktree_path)
        assert result == worktree_path

    def test_create_new_branch_from_current_head(self, git_repo, tmp_path):
        """create creates new branch from current HEAD when branch doesn't exist."""
        (git_repo / "file.txt").write_text("content")
        subprocess.run(["git", "-C", str(git_repo), "add", "file.txt"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(git_repo), "commit", "-m", "add file"], check=True, capture_output=True)
        worktree_path = tmp_path / "worktrees" / "test-new-branch"
        wt.create(git_repo, "test-new-branch", worktree_path)
        result = subprocess.run(
            ["git", "-C", str(worktree_path), "log", "-1", "--format=%s"],
            capture_output=True, text=True
        )
        assert "add file" in result.stdout


class TestCommitChanges:
    """Test commit_changes function."""

    def test_commit_changes_returns_true_when_uncommitted_changes(self, git_repo, tmp_path):
        """commit_changes returns True when there are uncommitted changes."""
        worktree_path = tmp_path / "worktrees" / "test-1"
        wt.create(git_repo, "test-1", worktree_path)
        (worktree_path / "new_file.txt").write_text("new content")
        result = wt.commit_changes(worktree_path, "add new file")
        assert result is True

    def test_commit_changes_returns_false_when_clean(self, git_repo, tmp_path):
        """commit_changes returns False when worktree is clean."""
        worktree_path = tmp_path / "worktrees" / "test-1"
        wt.create(git_repo, "test-1", worktree_path)
        result = wt.commit_changes(worktree_path, "no changes")
        assert result is False

    def test_commit_changes_stages_and_commits_new_files(self, git_repo, tmp_path):
        """commit_changes stages and commits new files."""
        worktree_path = tmp_path / "worktrees" / "test-1"
        wt.create(git_repo, "test-1", worktree_path)
        (worktree_path / "new.txt").write_text("content")
        wt.commit_changes(worktree_path, "add new")
        result = subprocess.run(
            ["git", "-C", str(worktree_path), "show", "--name-only", "--format="],
            capture_output=True, text=True
        )
        assert "new.txt" in result.stdout

    def test_commit_changes_stages_and_commits_modified_files(self, git_repo, tmp_path):
        """commit_changes stages and commits modified files."""
        worktree_path = tmp_path / "worktrees" / "test-1"
        wt.create(git_repo, "test-1", worktree_path)
        (worktree_path / "README").write_text("modified")
        wt.commit_changes(worktree_path, "modify readme")
        result = subprocess.run(
            ["git", "-C", str(worktree_path), "show", "--name-only", "--format="],
            capture_output=True, text=True
        )
        assert "README" in result.stdout

    def test_commit_changes_stages_and_commits_deleted_files(self, git_repo, tmp_path):
        """commit_changes stages and commits deleted files."""
        worktree_path = tmp_path / "worktrees" / "test-1"
        wt.create(git_repo, "test-1", worktree_path)
        (worktree_path / "to_delete.txt").write_text("content to delete")
        subprocess.run(["git", "-C", str(worktree_path), "add", "to_delete.txt"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(worktree_path), "commit", "-m", "add file to delete"], check=True, capture_output=True)
        (worktree_path / "to_delete.txt").unlink()
        wt.commit_changes(worktree_path, "delete file")
        result = subprocess.run(
            ["git", "-C", str(worktree_path), "show", "--name-only", "--format="],
            capture_output=True, text=True
        )
        assert "to_delete.txt" in result.stdout


class TestPrune:
    """Test prune function."""

    def test_prune_removes_worktree_directory(self, git_repo, tmp_path):
        """prune removes worktree directory."""
        worktree_path = tmp_path / "worktrees" / "test-1"
        wt.create(git_repo, "test-1", worktree_path)
        wt.prune(git_repo, worktree_path)
        assert not worktree_path.exists()

    def test_prune_keeps_branch_by_default(self, git_repo, tmp_path):
        """prune keeps branch by default (keep_branch=True)."""
        worktree_path = tmp_path / "worktrees" / "test-1"
        wt.create(git_repo, "test-1", worktree_path)
        wt.prune(git_repo, worktree_path)
        assert wt._branch_exists(git_repo, "mas/test-1") is True

    def test_prune_deletes_branch_when_keep_branch_false(self, git_repo, tmp_path):
        """prune deletes branch when keep_branch=False."""
        worktree_path = tmp_path / "worktrees" / "test-1"
        wt.create(git_repo, "test-1", worktree_path)
        wt.prune(git_repo, worktree_path, keep_branch=False)
        assert wt._branch_exists(git_repo, "mas/test-1") is False

    def test_prune_handles_already_removed_worktree_path(self, git_repo, tmp_path):
        """prune handles already-removed worktree path gracefully."""
        worktree_path = tmp_path / "worktrees" / "nonexistent"
        wt.prune(git_repo, worktree_path)


class TestIntegration:
    """Integration tests combining multiple functions."""

    def test_full_worktree_lifecycle(self, git_repo, tmp_path):
        """Test full lifecycle: create -> make changes -> commit -> prune."""
        worktree_path = tmp_path / "worktrees" / "test-1"
        result = wt.create(git_repo, "test-1", worktree_path)
        assert result == worktree_path

        (worktree_path / "file.txt").write_text("content")
        committed = wt.commit_changes(worktree_path, "add file")
        assert committed is True

        assert wt._branch_exists(git_repo, "mas/test-1") is True
        wt.prune(git_repo, worktree_path)
        assert not worktree_path.exists()
        assert wt._branch_exists(git_repo, "mas/test-1") is True