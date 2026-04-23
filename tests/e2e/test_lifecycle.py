"""E2E lifecycle tests for MAS board and tick loop.

These tests verify full lifecycle scenarios using a real .mas directory with
config.yaml and roles.yaml, real tick loop and board operations, but mock
all adapter dispatch calls to simulate agent output by writing result.json files.
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mas import board
from mas.schemas import (
    MasConfig,
    Plan,
    ProviderConfig,
    Result,
    RoleConfig,
    SubtaskSpec,
    Task,
)
from mas.tick import TickEnv, _advance_doing, run_tick


def _cfg(
    max_retries: int = 2,
    max_proposed: int = 10,
    proposal_similarity_threshold: float = 0.7,
) -> MasConfig:
    return MasConfig(
        providers={"mock": ProviderConfig(cli="echo", max_concurrent=2, extra_args=[])},
        roles={
            "proposer": RoleConfig(provider="mock", max_retries=max_retries),
            "orchestrator": RoleConfig(provider="mock", max_retries=max_retries),
            "implementer": RoleConfig(provider="mock", max_retries=max_retries),
            "tester": RoleConfig(provider="mock", max_retries=max_retries),
            "evaluator": RoleConfig(provider="mock", max_retries=max_retries),
        },
        max_proposed=max_proposed,
        proposal_similarity_threshold=proposal_similarity_threshold,
    )


def _init_git_repo(repo: Path) -> None:
    """Initialize a git repo for worktree tests."""
    repo.mkdir(parents=True, exist_ok=True)
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


def _write_config(mas: Path) -> None:
    """Write config.yaml and roles.yaml to .mas directory."""
    mas.mkdir(parents=True, exist_ok=True)
    config_yaml = """max_proposed: 10

providers:
  mock:
    cli: echo
    max_concurrent: 2
    extra_args: []

proposer_signals: {}
"""
    roles_yaml = """roles:
  proposer:
    provider: mock
    max_retries: 2
  orchestrator:
    provider: mock
    max_retries: 2
  implementer:
    provider: mock
    max_retries: 2
  tester:
    provider: mock
    max_retries: 2
  evaluator:
    provider: mock
    max_retries: 2
"""
    (mas / "config.yaml").write_text(config_yaml)
    (mas / "roles.yaml").write_text(roles_yaml)


@pytest.fixture
def e2e_env(tmp_path):
    """Create a complete E2E test environment."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    mas = tmp_path / ".mas"
    _write_config(mas)
    board.ensure_layout(mas)

    return {"repo": repo, "mas": mas, "tmp_path": tmp_path}


class TestHappyPathLifecycle:
    """Test the happy-path lifecycle: proposal -> orchestration -> implementation -> testing -> evaluation -> done."""

    def test_full_lifecycle_proposes_to_done(self, e2e_env):
        """Full lifecycle moves task from proposed/ through doing/ to done/."""
        mas = e2e_env["mas"]
        repo = e2e_env["repo"]

        parent_id = "20260420-happy-aaaa"
        parent = board.task_dir(mas, "doing", parent_id)
        parent.mkdir(parents=True)
        board.write_task(
            parent,
            Task(id=parent_id, role="orchestrator", goal="Implement a simple hello world function"),
        )
        (parent / "worktree").mkdir()
        plan = Plan(
            parent_id=parent_id,
            summary="Implement hello world",
            subtasks=[
                SubtaskSpec(
                    id="20260420-impl-1abc",
                    role="implementer",
                    goal="Write a hello_world() function that returns 'Hello, World!'",
                ),
                SubtaskSpec(
                    id="20260420-test-1abc",
                    role="tester",
                    goal="Write test for impl's implementation",
                ),
                SubtaskSpec(
                    id="20260420-eval-1abc",
                    role="evaluator",
                    goal="Evaluate whether tests pass and implementation is correct",
                ),
            ],
            max_revision_cycles=2,
        )
        (parent / "plan.json").write_text(plan.model_dump_json(indent=2))
        subtasks = parent / "subtasks"
        subtasks.mkdir()

        env = TickEnv(repo=repo, mas=mas, cfg=_cfg())

        with patch("mas.tick.get_adapter") as mock_get, \
             patch("mas.board.count_active_pids", return_value=0), \
             patch("mas.board.write_pid"):
            mock_adapter = MagicMock()
            mock_adapter.dispatch.return_value = MagicMock(pid=12345)
            mock_adapter.agentic = False
            mock_get.return_value.return_value = mock_adapter

            impl_dir = subtasks / "20260420-impl-1abc"
            impl_dir.mkdir()
            _advance_doing(env)
            assert (impl_dir / "task.json").exists()

            impl_task = board.read_task(impl_dir)
            impl_result = Result(
                task_id=impl_task.id,
                status="success",
                summary="Wrote hello_world() function",
            )
            (impl_dir / "result.json").write_text(impl_result.model_dump_json(indent=2))

            _advance_doing(env)

            test_dir = subtasks / "20260420-test-1abc"
            assert test_dir.exists()
            test_result = Result(
                task_id="20260420-test-1abc",
                status="success",
                summary="Tests written",
                handoff={"test_command": "pytest", "test_files": []},
            )
            (test_dir / "result.json").write_text(test_result.model_dump_json(indent=2))

            _advance_doing(env)

            eval_dir = subtasks / "20260420-eval-1abc"
            assert eval_dir.exists()
            eval_result = Result(
                task_id="20260420-eval-1abc",
                status="success",
                summary="Tests pass",
                verdict="pass",
            )
            (eval_dir / "result.json").write_text(eval_result.model_dump_json(indent=2))

            _advance_doing(env)

        assert not parent.exists()
        assert (mas / "tasks" / "done" / parent_id).exists()
        done_dir = mas / "tasks" / "done" / parent_id
        assert (done_dir / "worktree").exists()

    def test_board_transitions_verified(self, e2e_env):
        """A proposer task in doing/ completes and moves to done/."""
        mas = e2e_env["mas"]
        repo = e2e_env["repo"]

        parent_id = "20260420-prop-aaaa"
        parent = board.task_dir(mas, "doing", parent_id)
        parent.mkdir(parents=True)
        board.write_task(
            parent,
            Task(id=parent_id, role="proposer", goal="Propose a new task"),
        )
        (parent / "logs").mkdir()

        env = TickEnv(repo=repo, mas=mas, cfg=_cfg())

        with patch("mas.tick.get_adapter") as mock_get, \
             patch("mas.board.count_active_pids", return_value=0), \
             patch("mas.board.write_pid"):
            mock_adapter = MagicMock()
            mock_adapter.dispatch.return_value = MagicMock(pid=12345)
            mock_adapter.agentic = False
            mock_get.return_value.return_value = mock_adapter

            result = Result(
                task_id=parent_id,
                status="success",
                summary="Proposed a task",
                handoff={"goal": "New feature"},
            )
            (parent / "result.json").write_text(result.model_dump_json(indent=2))

            _advance_doing(env)

        assert not parent.exists()
        done_dir = mas / "tasks" / "done" / parent_id
        assert done_dir.exists()


class TestRevisionCycleLifecycle:
    """Test revision cycle: evaluator needs_revision -> impl -> test -> eval -> done."""

    def test_revision_cycle_enforced(self, e2e_env):
        """Evaluator needs_revision triggers new revision cycle, bounded by max_revision_cycles."""
        mas = e2e_env["mas"]
        repo = e2e_env["repo"]

        parent_id = "20260420-rev-aaaa"
        parent = board.task_dir(mas, "doing", parent_id)
        parent.mkdir(parents=True)
        board.write_task(
            parent,
            Task(id=parent_id, role="orchestrator", goal="Implement something"),
        )
        (parent / "worktree").mkdir()
        plan = Plan(
            parent_id=parent_id,
            summary="Initial implementation",
            subtasks=[
                SubtaskSpec(
                    id="20260420-ri1-0abc",
                    role="implementer",
                    goal="Initial implementation",
                ),
                SubtaskSpec(
                    id="20260420-rt1-0abc",
                    role="tester",
                    goal="Write tests for impl",
                ),
                SubtaskSpec(
                    id="20260420-re1-0abc",
                    role="evaluator",
                    goal="Evaluate initial implementation",
                ),
            ],
            max_revision_cycles=2,
        )
        (parent / "plan.json").write_text(plan.model_dump_json(indent=2))
        subtasks = parent / "subtasks"
        subtasks.mkdir()

        env = TickEnv(repo=repo, mas=mas, cfg=_cfg())

        with patch("mas.tick.get_adapter") as mock_get, \
             patch("mas.board.count_active_pids", return_value=0), \
             patch("mas.board.write_pid"):
            mock_adapter = MagicMock()
            mock_adapter.dispatch.return_value = MagicMock(pid=12345)
            mock_adapter.agentic = False
            mock_get.return_value.return_value = mock_adapter

            impl_dir = subtasks / "20260420-ri1-0abc"
            impl_dir.mkdir()
            _advance_doing(env)
            impl_result = Result(
                task_id="20260420-ri1-0abc",
                status="success",
                summary="Initial impl",
            )
            (impl_dir / "result.json").write_text(impl_result.model_dump_json(indent=2))

            _advance_doing(env)

            test_dir = subtasks / "20260420-rt1-0abc"
            test_dir.mkdir(exist_ok=True)
            test_result = Result(
                task_id="20260420-rt1-0abc",
                status="success",
                summary="Tests written",
            )
            (test_dir / "result.json").write_text(test_result.model_dump_json(indent=2))

            _advance_doing(env)

            eval_dir = subtasks / "20260420-re1-0abc"
            eval_dir.mkdir(exist_ok=True)
            eval_result = Result(
                task_id="20260420-re1-0abc",
                status="needs_revision",
                summary="Implementation needs fixes",
                verdict="needs_revision",
                feedback="Please fix the bug in the hello function",
            )
            (eval_dir / "result.json").write_text(eval_result.model_dump_json(indent=2))

            _advance_doing(env)

        from mas.roles import parse_plan
        updated_plan = parse_plan(parent / "plan.json", parent_id)
        assert len(updated_plan.subtasks) > 3

        revision_ids = {s.id for s in updated_plan.subtasks if s.id.startswith("rev-")}
        assert len(revision_ids) >= 1

    def test_revision_feedback_passed_forward(self, e2e_env):
        """Revision feedback from evaluator is passed to next implementer."""
        mas = e2e_env["mas"]
        repo = e2e_env["repo"]

        parent_id = "20260420-revfeed-aaaa"
        parent = board.task_dir(mas, "doing", parent_id)
        parent.mkdir(parents=True)
        board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="Implement X"))
        (parent / "worktree").mkdir()
        plan = Plan(
            parent_id=parent_id,
            summary="Initial",
            subtasks=[
                SubtaskSpec(
                    id="20260420-rfi1-0abc",
                    role="implementer",
                    goal="Initial impl",
                ),
                SubtaskSpec(
                    id="20260420-rft1-0abc",
                    role="tester",
                    goal="Test impl",
                ),
                SubtaskSpec(
                    id="20260420-rfe1-0abc",
                    role="evaluator",
                    goal="Eval",
                ),
            ],
            max_revision_cycles=1,
        )
        (parent / "plan.json").write_text(plan.model_dump_json(indent=2))
        subtasks = parent / "subtasks"
        subtasks.mkdir()

        env = TickEnv(repo=repo, mas=mas, cfg=_cfg())

        feedback_text = "Bug in function: returns wrong value"

        with patch("mas.tick.get_adapter") as mock_get, \
             patch("mas.board.count_active_pids", return_value=0), \
             patch("mas.board.write_pid"):
            mock_adapter = MagicMock()
            mock_adapter.dispatch.return_value = MagicMock(pid=12345)
            mock_adapter.agentic = False
            mock_get.return_value.return_value = mock_adapter

            impl_dir = subtasks / "20260420-rfi1-0abc"
            impl_dir.mkdir()
            _advance_doing(env)
            (impl_dir / "result.json").write_text(
                Result(task_id="20260420-rfi1-0abc", status="success", summary="impl").model_dump_json()
            )

            _advance_doing(env)

            test_dir = subtasks / "20260420-rft1-0abc"
            test_dir.mkdir(exist_ok=True)
            (test_dir / "result.json").write_text(
                Result(task_id="20260420-rft1-0abc", status="success", summary="test").model_dump_json()
            )

            _advance_doing(env)

            eval_dir = subtasks / "20260420-rfe1-0abc"
            eval_dir.mkdir(exist_ok=True)
            (eval_dir / "result.json").write_text(
                Result(
                    task_id="20260420-rfe1-0abc",
                    status="needs_revision",
                    summary="needs revision",
                    verdict="needs_revision",
                    feedback=feedback_text,
                ).model_dump_json()
            )

            _advance_doing(env)

        from mas.roles import parse_plan
        updated_plan = parse_plan(parent / "plan.json", parent_id)
        revision_ids = {s.id for s in updated_plan.subtasks if s.id.startswith("rev-")}
        assert len(revision_ids) >= 1

        # Feedback is stored once per cycle on the Plan; rev-* subtasks carry a
        # feedback_cycle reference instead of the full text (deduped).
        assert feedback_text in updated_plan.revision_feedback.get("rev-1", "")
        found_ref = False
        for spec in updated_plan.subtasks:
            if spec.id.startswith("rev-") and spec.role == "implementer":
                assert spec.inputs.get("feedback_cycle") == "rev-1"
                assert "feedback" not in spec.inputs
                found_ref = True
        assert found_ref, "Revision implementer should reference rev-1 feedback_cycle"


class TestFailureRecovery:
    """Test failure recovery: subtask exhausts max_retries -> parent to failed/."""

    def test_max_retries_moves_to_failed(self, e2e_env):
        """A subtask that exhausts max_retries moves parent to failed/."""
        mas = e2e_env["mas"]
        repo = e2e_env["repo"]

        parent_id = "20260420-fail-aaaa"
        parent = board.task_dir(mas, "doing", parent_id)
        parent.mkdir(parents=True)
        board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="Implement something"))
        (parent / "worktree").mkdir()
        plan = Plan(
            parent_id=parent_id,
            summary="Test",
            subtasks=[
                SubtaskSpec(
                    id="20260420-fi1-0abc",
                    role="implementer",
                    goal="Implement feature",
                ),
            ],
        )
        (parent / "plan.json").write_text(plan.model_dump_json(indent=2))
        subtasks = parent / "subtasks"
        subtasks.mkdir()

        env = TickEnv(repo=repo, mas=mas, cfg=_cfg(max_retries=2))

        impl_dir = subtasks / "20260420-fi1-0abc"
        impl_dir.mkdir()
        (impl_dir / ".attempt").write_text("3")

        impl_result = Result(
            task_id="20260420-fi1-0abc",
            status="failure",
            summary="Still failing after retries",
        )
        (impl_dir / "result.json").write_text(impl_result.model_dump_json(indent=2))

        _advance_doing(env)

        assert not parent.exists()
        assert (mas / "tasks" / "failed" / parent_id).exists()


class TestWorktreeLifecycle:
    """Test git worktree creation and cleanup."""

    def test_worktree_created_on_orchestration(self, e2e_env):
        """Worktree directory is created when orchestrator runs."""
        mas = e2e_env["mas"]
        repo = e2e_env["repo"]

        parent_id = "20260420-wt-aaaa"
        parent = board.task_dir(mas, "doing", parent_id)
        parent.mkdir(parents=True)
        board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="Test worktree"))
        assert not (parent / "worktree").exists()

        env = TickEnv(repo=repo, mas=mas, cfg=_cfg())

        with patch("mas.tick.get_adapter") as mock_get, \
             patch("mas.board.count_active_pids", return_value=0), \
             patch("mas.board.write_pid"):
            mock_adapter = MagicMock()
            mock_adapter.dispatch.return_value = MagicMock(pid=12345)
            mock_adapter.agentic = False
            mock_get.return_value.return_value = mock_adapter

            result = Result(
                task_id=parent_id,
                status="success",
                summary="Created plan",
                handoff={
                    "parent_id": parent_id,
                    "summary": "test",
                    "subtasks": [{"id": "20260420-s1-abcd", "role": "tester", "goal": "t"}],
                },
            )
            (parent / "result.json").write_text(result.model_dump_json(indent=2))

            _advance_doing(env)

        assert (parent / "worktree").exists()

    def test_worktree_pruned_on_done(self, e2e_env):
        """Worktree is pruned when task moves to done/."""
        mas = e2e_env["mas"]
        repo = e2e_env["repo"]

        parent_id = "20260420-wtdone-aaaa"
        parent = board.task_dir(mas, "doing", parent_id)
        parent.mkdir(parents=True)
        board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="Done task"))
        wt = parent / "worktree"
        wt.mkdir()
        (wt / "somefile.txt").write_text("content")

        plan = Plan(
            parent_id=parent_id,
            summary="Done",
            subtasks=[
                SubtaskSpec(id="20260420-wdi-0abc", role="implementer", goal="g"),
            ],
        )
        (parent / "plan.json").write_text(plan.model_dump_json(indent=2))
        subtasks = parent / "subtasks"
        subtasks.mkdir()
        impl_dir = subtasks / "20260420-wdi-0abc"
        impl_dir.mkdir()
        (impl_dir / "result.json").write_text(
            Result(task_id="20260420-wdi-0abc", status="success", summary="ok").model_dump_json()
        )
        eval_dir = subtasks / "20260420-wde-0abc"
        eval_dir.mkdir()
        (eval_dir / "result.json").write_text(
            Result(task_id="20260420-wde-0abc", status="success", summary="ok", verdict="pass").model_dump_json()
        )

        env = TickEnv(repo=repo, mas=mas, cfg=_cfg())

        with patch.object(subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _advance_doing(env)

        assert not parent.exists()
        assert (mas / "tasks" / "done" / parent_id).exists()


class TestPriorResultsPropagation:
    """Test that prior_results are propagated between subtasks."""

    def test_prior_results_injected_into_task_json(self, e2e_env):
        """Tester's result is injected into implementer's task.json."""
        mas = e2e_env["mas"]
        repo = e2e_env["repo"]

        parent_id = "20260420-prior-aaaa"
        parent = board.task_dir(mas, "doing", parent_id)
        parent.mkdir(parents=True)
        board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="Test propagation"))
        (parent / "worktree").mkdir()
        plan = Plan(
            parent_id=parent_id,
            summary="Test",
            subtasks=[
                SubtaskSpec(
                    id="20260420-pt-abcd",
                    role="tester",
                    goal="Write tests",
                ),
                SubtaskSpec(
                    id="20260420-pi-abcd",
                    role="implementer",
                    goal="Implement feature",
                ),
            ],
        )
        (parent / "plan.json").write_text(plan.model_dump_json(indent=2))
        subtasks = parent / "subtasks"
        subtasks.mkdir()

        test_dir = subtasks / "20260420-pt-abcd"
        test_dir.mkdir()
        test_result = Result(
            task_id="20260420-pt-abcd",
            status="success",
            summary="Tests written",
            handoff={"test_command": "pytest tests/test_x.py", "test_files": ["tests/test_x.py"]},
        )
        (test_dir / "result.json").write_text(test_result.model_dump_json(indent=2))

        impl_dir = subtasks / "20260420-pi-abcd"
        impl_dir.mkdir()

        env = TickEnv(repo=repo, mas=mas, cfg=_cfg())

        with patch("mas.tick.get_adapter") as mock_get, \
             patch("mas.board.count_active_pids", return_value=0), \
             patch("mas.board.write_pid"):
            mock_adapter = MagicMock()
            mock_adapter.dispatch.return_value = MagicMock(pid=12345)
            mock_adapter.agentic = False
            mock_get.return_value.return_value = mock_adapter

            _advance_doing(env)

        impl_task = board.read_task(impl_dir)
        assert len(impl_task.prior_results) == 1
        assert impl_task.prior_results[0].task_id == "20260420-pt-abcd"
        assert impl_task.prior_results[0].handoff["test_command"] == "pytest tests/test_x.py"