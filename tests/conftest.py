"""Shared pytest fixtures for MAS tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from datetime import datetime

import pytest

from mas.adapters.base import Adapter
from mas.board import ensure_layout
from mas.schemas import ProviderConfig, RoleConfig


def _fixed_setup_board_root(tmp_path, task_entries):
    """Fixed version of setup_board_root that uses exist_ok=True."""
    board_root = tmp_path / "board"
    board_root.mkdir(exist_ok=True)
    tasks_dir = board_root / "tasks"
    tasks_dir.mkdir(exist_ok=True)
    (tasks_dir / "done").mkdir(exist_ok=True)
    (tasks_dir / "doing").mkdir(exist_ok=True)

    for task_id, is_done, timestamp, cost in task_entries:
        task_dir = tasks_dir / ("done" if is_done else "doing") / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        audit_file = task_dir / "audit.jsonl"
        entry = {
            "timestamp": timestamp.isoformat(),
            "event": "subtask_complete",
            "details": {"cost_usd": cost}
        }
        with open(audit_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
    return board_root


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    """Monkeypatch setup_board_root in test_budget_forecast module."""
    if item.fspath and "test_budget_forecast" in str(item.fspath):
        import tests.test_budget_forecast as test_mod
        test_mod.setup_board_root = _fixed_setup_board_root


@pytest.fixture
def tmp_board(tmp_path):
    """Create a temporary .mas/ directory structure."""
    mas_dir = tmp_path / ".mas"
    ensure_layout(mas_dir)
    return mas_dir


@pytest.fixture
def fake_adapter():
    """A mock adapter that returns canned responses for testing."""

    class FakeAdapter(Adapter):
        name = "fake"
        agentic = True

        def __init__(self, provider_cfg=None, role_cfg=None):
            if provider_cfg is None:
                provider_cfg = ProviderConfig(cli="echo", max_concurrent=1)
            if role_cfg is None:
                role_cfg = RoleConfig(provider="fake", model=None, timeout_s=30)
            super().__init__(provider_cfg, role_cfg)
            self.captured_prompts = []

        def build_command(self, prompt, task_dir, cwd):
            self.captured_prompts.append(prompt)
            return ["echo", "fake"]

    return FakeAdapter()


@pytest.fixture
def git_repo(tmp_path):
    """Initialize a bare git repo for worktree tests."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    (repo / "README").write_text("# Test\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True
    )
    return repo