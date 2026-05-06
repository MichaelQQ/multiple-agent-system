"""Pattern-block prompt injection for implementer and tester subtasks.

When a ``patterns.jsonl`` file exists under ``.mas/``, the implementer and
tester prompts are augmented with a ``$pattern_block`` that lists recurring
failure patterns relevant to the current task goal. Orchestrator prompts must
*not* receive this block.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from mas.adapters import get_adapter
from mas.adapters.base import DispatchHandle
from mas.patterns import FailurePattern, write_patterns
from mas.schemas import MasConfig, ProviderConfig, RoleConfig, Task
from mas.tick import TickEnv, _dispatch_role


def _minimal_cfg_kwargs():
    return dict(
        providers={"mock": ProviderConfig(cli="sh", max_concurrent=4)},
        roles={
            r: RoleConfig(provider="mock")
            for r in ("proposer", "orchestrator", "implementer", "tester", "evaluator")
        },
    )


def _setup_dispatch(tmp_path: Path, *, role: str, goal: str, patterns: list[FailurePattern] | None = None):
    from mas import board
    from mas.ids import task_id as make_task_id

    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    (mas / "prompts").mkdir(exist_ok=True)

    # Write role prompt templates with $pattern_block placeholder
    for r in ("implementer", "tester", "orchestrator"):
        (mas / "prompts" / f"{r}.md").write_text(
            f"goal=$goal\nPATTERN_BLOCK_START\n$pattern_block\nPATTERN_BLOCK_END\n"
        )

    if patterns is not None:
        write_patterns(mas, patterns)

    cfg = MasConfig(**_minimal_cfg_kwargs())
    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)

    task_id = make_task_id(goal, salt=role)
    task_dir_ = board.task_dir(mas, "doing", task_id)
    task_dir_.mkdir(parents=True)
    task = Task(id=task_id, role=role, goal=goal)

    return env, cfg, task_dir_, task


def _capture_dispatch(tmp_path: Path, *, role: str, goal: str,
                       patterns: list[FailurePattern] | None = None) -> str:
    """Dispatch a role and return the rendered prompt."""
    env, cfg, task_dir_, task = _setup_dispatch(tmp_path, role=role, goal=goal, patterns=patterns)

    captured: list[str] = []

    def fake_dispatch(self, prompt, task_dir, cwd, log_path, role,
                       stdin_text=None, extra_env=None, **_):
        captured.append(prompt)
        return DispatchHandle(pid=1, provider="mock", role=role,
                              task_dir=task_dir, log_path=log_path)

    adapter_cls = get_adapter("mock")
    with patch.object(adapter_cls, "dispatch", fake_dispatch):
        _dispatch_role(env, task, task_dir_, tmp_path, role=role)

    assert captured, "dispatch was not called"
    return captured[0]


def _make_pattern(*, signature, terminal_reason, goal_sample, count,
                   last_seen=None, task_ids=None, rejected_attempts_sample=None):
    return FailurePattern(
        signature=signature,
        terminal_reason=terminal_reason,
        goal_sample=goal_sample,
        count=count,
        last_seen=last_seen or datetime.now(timezone.utc).isoformat(),
        task_ids=task_ids or ["t1"],
        rejected_attempts_sample=rejected_attempts_sample or [],
    )


# --- Tests -------------------------------------------------------------------


class TestPatternBlockInjectedForImplementer:
    def test_pattern_block_present_with_top_patterns(self, tmp_path: Path):
        patterns = [
            _make_pattern(signature="timeout|login flow", terminal_reason="timeout",
                          goal_sample="fix login flow timeout", count=5),
            _make_pattern(signature="import error|auth module", terminal_reason="worker_crash",
                          goal_sample="implement auth module", count=4),
            _make_pattern(signature="null pointer|payment", terminal_reason="max_retries_exceeded",
                          goal_sample="add payment processing", count=3),
            _make_pattern(signature="race condition|cache", terminal_reason="timeout",
                          goal_sample="fix cache race condition", count=2),
            _make_pattern(signature="off-by-one|index", terminal_reason="convergence_detected",
                          goal_sample="fix array index", count=1),
        ]
        prompt = _capture_dispatch(tmp_path, role="implementer",
                                    goal="fix login flow timeout issues",
                                    patterns=patterns)

        assert "PATTERN_BLOCK_START" in prompt
        assert "PATTERN_BLOCK_END" in prompt
        # At least some pattern signatures should appear
        assert any(sig in prompt for sig in ["timeout|login flow", "import error|auth module",
                                              "null pointer|payment"])
        # Counts should be visible
        assert "5" in prompt
        # Terminal reasons should be visible
        assert "timeout" in prompt


class TestPatternBlockInjectedForTester:
    def test_pattern_block_present_for_tester(self, tmp_path: Path):
        patterns = [
            _make_pattern(signature="flaky|network", terminal_reason="timeout",
                          goal_sample="test network timeout handling", count=3),
        ]
        prompt = _capture_dispatch(tmp_path, role="tester",
                                    goal="test network timeout handling",
                                    patterns=patterns)

        assert "PATTERN_BLOCK_START" in prompt
        assert "PATTERN_BLOCK_END" in prompt
        assert "flaky|network" in prompt
        assert "timeout" in prompt


class TestPatternBlockNotInjectedForOrchestrator:
    def test_orchestrator_omits_pattern_block(self, tmp_path: Path):
        patterns = [
            _make_pattern(signature="orphan|zombie", terminal_reason="unknown",
                          goal_sample="decompose task", count=5),
        ]
        prompt = _capture_dispatch(tmp_path, role="orchestrator",
                                    goal="decompose task into subtasks",
                                    patterns=patterns)

        assert "PATTERN_BLOCK_START" in prompt
        # The block between START/END should be empty for orchestrator
        import re
        block_match = re.search(r"PATTERN_BLOCK_START\n(.*)\nPATTERN_BLOCK_END", prompt, re.DOTALL)
        assert block_match is not None
        assert block_match.group(1).strip() == ""


class TestPatternBlockOmittedWhenNoPatternsFile:
    def test_no_patterns_file_no_error(self, tmp_path: Path):
        patterns_file = tmp_path / ".mas" / "patterns.jsonl"
        if patterns_file.exists():
            patterns_file.unlink()

        prompt = _capture_dispatch(tmp_path, role="implementer",
                                    goal="implement something new",
                                    patterns=None)

        assert "PATTERN_BLOCK_START" in prompt
        import re
        block_match = re.search(r"PATTERN_BLOCK_START\n(.*)\nPATTERN_BLOCK_END", prompt, re.DOTALL)
        assert block_match is not None
        assert block_match.group(1).strip() == ""


class TestPatternBlockOmittedWhenEmptyFile:
    def test_empty_patterns_file_no_block(self, tmp_path: Path):
        from mas import board
        from mas.ids import task_id as make_task_id
        mas = tmp_path / ".mas"
        board.ensure_layout(mas)
        (mas / "prompts").mkdir(exist_ok=True)
        for r in ("implementer",):
            (mas / "prompts" / f"{r}.md").write_text(
                "goal=$goal\nPATTERN_BLOCK_START\n$pattern_block\nPATTERN_BLOCK_END\n"
            )
        # Write empty patterns file
        (mas / "patterns.jsonl").write_text("")

        cfg = MasConfig(**_minimal_cfg_kwargs())
        env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)
        tid = make_task_id("test empty patterns", salt="empty")
        task_dir_ = board.task_dir(mas, "doing", tid)
        task_dir_.mkdir(parents=True)
        task = Task(id=tid, role="implementer", goal="test empty patterns")

        captured: list[str] = []
        def fake_dispatch(self, prompt, task_dir, cwd, log_path, role, **_):
            captured.append(prompt)
            return DispatchHandle(pid=1, provider="mock", role=role,
                                  task_dir=task_dir, log_path=log_path)

        adapter_cls = get_adapter("mock")
        with patch.object(adapter_cls, "dispatch", fake_dispatch):
            _dispatch_role(env, task, task_dir_, tmp_path, role="implementer")

        assert captured
        import re
        block_match = re.search(r"PATTERN_BLOCK_START\n(.*)\nPATTERN_BLOCK_END", captured[0], re.DOTALL)
        assert block_match is not None
        assert block_match.group(1).strip() == ""


class TestPatternBlockOmittedWhenNoRelevantPatterns:
    def test_no_relevant_patterns_no_block(self, tmp_path: Path):
        # Patterns exist but none are relevant to the current goal
        patterns = [
            _make_pattern(signature="unrelated|goal", terminal_reason="timeout",
                          goal_sample="completely different feature", count=5),
        ]
        prompt = _capture_dispatch(tmp_path, role="implementer",
                                    goal="build a spaceship engine",
                                    patterns=patterns)

        import re
        block_match = re.search(r"PATTERN_BLOCK_START\n(.*)\nPATTERN_BLOCK_END", prompt, re.DOTALL)
        assert block_match is not None
        assert block_match.group(1).strip() == ""


class TestPatternBlockFiltersByRelevance:
    def test_only_relevant_patterns_in_block(self, tmp_path: Path):
        patterns = [
            # Relevant to "login flow" goal
            _make_pattern(signature="timeout|login", terminal_reason="timeout",
                          goal_sample="fix login flow timeout", count=5),
            # NOT relevant
            _make_pattern(signature="crash|payment", terminal_reason="worker_crash",
                          goal_sample="process credit card payments", count=4),
            # Relevant
            _make_pattern(signature="error|auth", terminal_reason="max_retries_exceeded",
                          goal_sample="implement authentication flow", count=3),
            # NOT relevant
            _make_pattern(signature="leak|database", terminal_reason="convergence_detected",
                          goal_sample="fix database connection leak", count=2),
        ]
        prompt = _capture_dispatch(tmp_path, role="implementer",
                                    goal="fix login flow timeout issues",
                                    patterns=patterns)

        assert "timeout|login" in prompt
        assert "error|auth" in prompt
        # Irrelevant patterns should NOT appear
        assert "crash|payment" not in prompt
        assert "leak|database" not in prompt


class TestPatternBlockRendersMarkdownFormat:
    def test_markdown_structure_present(self, tmp_path: Path):
        patterns = [
            _make_pattern(signature="timeout|login", terminal_reason="timeout",
                          goal_sample="fix login flow timeout that occurs when users submit",
                          count=5),
        ]
        prompt = _capture_dispatch(tmp_path, role="implementer",
                                    goal="fix login flow timeout issues",
                                    patterns=patterns)

        import re
        block_match = re.search(r"PATTERN_BLOCK_START\n(.*)\nPATTERN_BLOCK_END", prompt, re.DOTALL)
        assert block_match is not None
        block = block_match.group(1)

        # Each pattern should render with signature
        assert "timeout|login" in block
        # Count should be visible
        assert "5" in block
        # Terminal reason should be visible
        assert "timeout" in block
        # Goal sample should be visible
        assert "login flow timeout" in block
