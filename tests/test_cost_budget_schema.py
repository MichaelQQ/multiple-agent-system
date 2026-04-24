"""Schema tests: Task.cost_budget_usd and MasConfig.default_cost_budget_usd must exist."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from mas.schemas import MasConfig, ProviderConfig, RoleConfig, Task


def _minimal_cfg_kwargs():
    return dict(
        providers={"mock": ProviderConfig(cli="sh", max_concurrent=1, extra_args=[])},
        roles={
            "proposer": RoleConfig(provider="mock"),
            "orchestrator": RoleConfig(provider="mock"),
            "implementer": RoleConfig(provider="mock"),
            "tester": RoleConfig(provider="mock"),
            "evaluator": RoleConfig(provider="mock"),
        },
    )


class TestTaskCostBudgetField:
    def test_task_has_cost_budget_usd_field(self):
        """Task schema must declare cost_budget_usd as a model field."""
        assert "cost_budget_usd" in Task.model_fields

    def test_task_cost_budget_usd_defaults_to_none(self):
        """cost_budget_usd must default to None (backward compatible)."""
        task = Task(id="20260424-test-aaaa", role="implementer", goal="g")
        assert getattr(task, "cost_budget_usd", "FIELD_MISSING") is None

    def test_task_extra_forbid_preserved(self):
        """extra='forbid' must still reject unknown fields after adding cost_budget_usd."""
        with pytest.raises(ValidationError):
            Task(
                id="20260424-test-aaaa",
                role="implementer",
                goal="g",
                definitely_not_a_field=True,
            )


class TestMasConfigDefaultCostBudgetField:
    def test_masconfig_has_default_cost_budget_usd_field(self):
        """MasConfig must declare default_cost_budget_usd as a model field."""
        assert "default_cost_budget_usd" in MasConfig.model_fields

    def test_masconfig_default_cost_budget_usd_defaults_to_none(self):
        """default_cost_budget_usd must default to None."""
        cfg = MasConfig(**_minimal_cfg_kwargs())
        assert getattr(cfg, "default_cost_budget_usd", "FIELD_MISSING") is None

    def test_masconfig_extra_forbid_preserved(self):
        """extra='forbid' must still reject unknown fields on MasConfig."""
        with pytest.raises(ValidationError):
            MasConfig(**_minimal_cfg_kwargs(), definitely_not_a_field=True)
