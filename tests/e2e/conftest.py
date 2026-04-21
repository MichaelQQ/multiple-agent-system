"""E2E test fixtures for MAS lifecycle tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from mas import board
from mas.schemas import MasConfig, Plan, ProviderConfig, RoleConfig, SubtaskSpec, Task

SCRIPT_DIR = Path(__file__).parent / "scripts"


@pytest.fixture
def git_repo(tmp_path):
    """Initialize a real git repo for worktree tests."""
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


@pytest.fixture
def mas_dir(tmp_path, git_repo):
    """Create .mas/ directory structure alongside the git repo."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)

    script_proposer = str(SCRIPT_DIR / "proposer.sh")
    script_orchestrator = str(SCRIPT_DIR / "orchestrator.sh")
    script_implementer = str(SCRIPT_DIR / "implementer.sh")
    script_tester = str(SCRIPT_DIR / "tester.sh")
    script_evaluator = str(SCRIPT_DIR / "evaluator.sh")

    config_data = {
        "providers": {
            "script": {
                "cli": "/bin/bash",
                "max_concurrent": 5,
                "extra_args": [script_proposer],
            },
            "mock": {
                "cli": "sh",
                "max_concurrent": 2,
                "extra_args": [],
            },
        },
        "roles": {
            "proposer": {"provider": "script", "max_retries": 1, "extra_args": [script_proposer]},
            "orchestrator": {"provider": "script", "max_retries": 1, "extra_args": [script_orchestrator]},
            "implementer": {"provider": "script", "max_retries": 1, "extra_args": [script_implementer]},
            "tester": {"provider": "script", "max_retries": 1, "extra_args": [script_tester]},
            "evaluator": {"provider": "script", "max_retries": 1, "extra_args": [script_evaluator]},
        },
        "max_proposed": 10,
    }

    (mas / "config.yaml").write_text(yaml.dump(config_data))
    roles_data = {"roles": config_data["roles"]}
    (mas / "roles.yaml").write_text(yaml.dump(roles_data))

    return mas


@pytest.fixture
def script_provider():
    """Return the script adapter class that should exist for E2E tests.

    This fixture FAILS if ScriptAdapter doesn't exist in the registry.
    """
    from mas.adapters import get_adapter
    from mas.adapters.base import Adapter

    adapter_cls = get_adapter("script")
    assert adapter_cls is not None
    assert issubclass(adapter_cls, Adapter)
    return adapter_cls


@pytest.fixture
def simple_task():
    """A simple task that can be executed by the script adapter."""
    return Task(
        id="20260421-test-0001",
        role="orchestrator",
        goal="Test task for lifecycle",
        inputs={"test": True},
    )


@pytest.fixture
def simple_plan(tmp_path):
    """A simple plan with a single implementer subtask."""
    return Plan(
        parent_id="20260421-test-0001",
        summary="Simple test plan",
        subtasks=[
            SubtaskSpec(
                id="20260421-test-0001-sub",
                role="implementer",
                goal="Implement a simple hello world",
                inputs={},
                constraints={},
            )
        ],
    )
