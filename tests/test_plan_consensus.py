"""Plan-time consensus (gap 3, item #20).

When a parent task's `cost_budget_usd` is ≥ `MasConfig.plan_consensus_threshold_usd`,
the orchestrator prompt is augmented with a block instructing the agent to draft
two distinct plan variants and pick one with rationale.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from mas.schemas import MasConfig, ProviderConfig, RoleConfig, Task


def _minimal_cfg_kwargs():
    return dict(
        providers={"mock": ProviderConfig(cli="sh", max_concurrent=4)},
        roles={
            r: RoleConfig(provider="mock")
            for r in ("proposer", "orchestrator", "implementer", "tester", "evaluator")
        },
    )


# --- Schema -----------------------------------------------------------------


class TestPlanConsensusThresholdField:
    def test_field_exists(self):
        assert "plan_consensus_threshold_usd" in MasConfig.model_fields

    def test_default_is_none(self):
        cfg = MasConfig(**_minimal_cfg_kwargs())
        assert cfg.plan_consensus_threshold_usd is None

    def test_accepts_float(self):
        cfg = MasConfig(**_minimal_cfg_kwargs(), plan_consensus_threshold_usd=5.0)
        assert cfg.plan_consensus_threshold_usd == 5.0

    def test_extra_forbid_preserved(self):
        with pytest.raises(ValidationError):
            MasConfig(**_minimal_cfg_kwargs(), bogus_field=True)


# --- Gate logic -------------------------------------------------------------


class TestConsensusEnabled:
    def _task(self, *, budget):
        return Task(
            id="20260505-tc-aaaa",
            role="orchestrator",
            goal="g",
            cost_budget_usd=budget,
        )

    def test_disabled_when_threshold_unset(self):
        from mas.tick import _consensus_enabled

        cfg = MasConfig(**_minimal_cfg_kwargs())
        assert _consensus_enabled(cfg, self._task(budget=100.0)) is False

    def test_disabled_when_no_budget_anywhere(self):
        from mas.tick import _consensus_enabled

        cfg = MasConfig(**_minimal_cfg_kwargs(), plan_consensus_threshold_usd=5.0)
        assert _consensus_enabled(cfg, self._task(budget=None)) is False

    def test_enabled_when_task_budget_meets_threshold(self):
        from mas.tick import _consensus_enabled

        cfg = MasConfig(**_minimal_cfg_kwargs(), plan_consensus_threshold_usd=5.0)
        assert _consensus_enabled(cfg, self._task(budget=5.0)) is True
        assert _consensus_enabled(cfg, self._task(budget=10.0)) is True

    def test_disabled_when_task_budget_below_threshold(self):
        from mas.tick import _consensus_enabled

        cfg = MasConfig(**_minimal_cfg_kwargs(), plan_consensus_threshold_usd=5.0)
        assert _consensus_enabled(cfg, self._task(budget=4.99)) is False

    def test_falls_back_to_default_budget(self):
        from mas.tick import _consensus_enabled

        cfg = MasConfig(
            **_minimal_cfg_kwargs(),
            plan_consensus_threshold_usd=5.0,
            default_cost_budget_usd=10.0,
        )
        assert _consensus_enabled(cfg, self._task(budget=None)) is True


# --- Prompt plumbing --------------------------------------------------------


def _setup_orchestrator_dispatch(tmp_path: Path, *, threshold, budget):
    from mas import board

    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    (mas / "prompts").mkdir(exist_ok=True)
    (mas / "prompts" / "orchestrator.md").write_text(
        "goal=$goal\nCONSENSUS_BLOCK_START\n$consensus_block\nCONSENSUS_BLOCK_END\n"
    )
    kwargs = _minimal_cfg_kwargs()
    if threshold is not None:
        kwargs["plan_consensus_threshold_usd"] = threshold
    cfg = MasConfig(**kwargs)

    task_dir_ = board.task_dir(mas, "doing", "20260505-tc-bbbb")
    task_dir_.mkdir(parents=True)
    task = Task(
        id="20260505-tc-bbbb",
        role="orchestrator",
        goal="decompose",
        cost_budget_usd=budget,
    )
    return mas, cfg, task_dir_, task


def test_orchestrator_prompt_includes_consensus_block_when_gated(tmp_path: Path):
    from mas.adapters import get_adapter
    from mas.adapters.base import DispatchHandle
    from mas.tick import TickEnv, _dispatch_role

    mas, cfg, task_dir_, task = _setup_orchestrator_dispatch(
        tmp_path, threshold=5.0, budget=10.0
    )
    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)

    captured: list[str] = []

    def fake_dispatch(self, prompt, task_dir, cwd, log_path, role,
                      stdin_text=None, extra_env=None, **_):
        captured.append(prompt)
        return DispatchHandle(pid=1, provider="mock", role=role,
                              task_dir=task_dir, log_path=log_path)

    adapter_cls = get_adapter("mock")
    with patch.object(adapter_cls, "dispatch", fake_dispatch):
        _dispatch_role(env, task, task_dir_, tmp_path, role="orchestrator")

    assert captured, "dispatch was not called"
    prompt = captured[0]
    assert "Plan-time consensus mode" in prompt
    assert "plan_variant_a.json" in prompt
    assert "plan_variant_b.json" in prompt
    assert "plan_pick.json" in prompt


def test_orchestrator_prompt_omits_consensus_block_when_threshold_unset(tmp_path: Path):
    from mas.adapters import get_adapter
    from mas.adapters.base import DispatchHandle
    from mas.tick import TickEnv, _dispatch_role

    mas, cfg, task_dir_, task = _setup_orchestrator_dispatch(
        tmp_path, threshold=None, budget=10.0
    )
    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)

    captured: list[str] = []

    def fake_dispatch(self, prompt, task_dir, cwd, log_path, role,
                      stdin_text=None, extra_env=None, **_):
        captured.append(prompt)
        return DispatchHandle(pid=1, provider="mock", role=role,
                              task_dir=task_dir, log_path=log_path)

    adapter_cls = get_adapter("mock")
    with patch.object(adapter_cls, "dispatch", fake_dispatch):
        _dispatch_role(env, task, task_dir_, tmp_path, role="orchestrator")

    assert captured, "dispatch was not called"
    prompt = captured[0]
    assert "Plan-time consensus mode" not in prompt
    assert "plan_variant_a.json" not in prompt


def test_orchestrator_prompt_omits_consensus_block_when_budget_below_threshold(tmp_path: Path):
    from mas.adapters import get_adapter
    from mas.adapters.base import DispatchHandle
    from mas.tick import TickEnv, _dispatch_role

    mas, cfg, task_dir_, task = _setup_orchestrator_dispatch(
        tmp_path, threshold=20.0, budget=5.0
    )
    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)

    captured: list[str] = []

    def fake_dispatch(self, prompt, task_dir, cwd, log_path, role,
                      stdin_text=None, extra_env=None, **_):
        captured.append(prompt)
        return DispatchHandle(pid=1, provider="mock", role=role,
                              task_dir=task_dir, log_path=log_path)

    adapter_cls = get_adapter("mock")
    with patch.object(adapter_cls, "dispatch", fake_dispatch):
        _dispatch_role(env, task, task_dir_, tmp_path, role="orchestrator")

    assert captured, "dispatch was not called"
    assert "Plan-time consensus mode" not in captured[0]


def test_consensus_block_only_for_orchestrator_role(tmp_path: Path):
    """Even when the gate fires, only the orchestrator prompt receives the
    block — implementer/tester/evaluator stay untouched."""
    from mas import board
    from mas.adapters import get_adapter
    from mas.adapters.base import DispatchHandle
    from mas.tick import TickEnv, _dispatch_role

    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    (mas / "prompts").mkdir(exist_ok=True)
    (mas / "prompts" / "implementer.md").write_text(
        "goal=$goal\nCONSENSUS_BLOCK_START\n$consensus_block\nCONSENSUS_BLOCK_END\n"
    )
    cfg = MasConfig(**_minimal_cfg_kwargs(), plan_consensus_threshold_usd=5.0)
    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)

    task_dir_ = board.task_dir(mas, "doing", "20260505-tc-cccc")
    task_dir_.mkdir(parents=True)
    task = Task(
        id="20260505-tc-cccc",
        role="implementer",
        goal="impl",
        cost_budget_usd=100.0,
    )

    captured: list[str] = []

    def fake_dispatch(self, prompt, task_dir, cwd, log_path, role,
                      stdin_text=None, extra_env=None, **_):
        captured.append(prompt)
        return DispatchHandle(pid=1, provider="mock", role=role,
                              task_dir=task_dir, log_path=log_path)

    adapter_cls = get_adapter("mock")
    with patch.object(adapter_cls, "dispatch", fake_dispatch):
        _dispatch_role(env, task, task_dir_, tmp_path, role="implementer")

    assert captured, "dispatch was not called"
    assert "Plan-time consensus mode" not in captured[0]
