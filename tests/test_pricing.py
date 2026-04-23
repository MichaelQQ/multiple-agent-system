from __future__ import annotations

import pytest

from mas.pricing import compute_cost_usd


class TestComputeCostUsd:
    def test_opus_cost_is_positive(self):
        cost = compute_cost_usd("claude-code", "claude-opus-4-7", tokens_in=1000, tokens_out=500)
        assert isinstance(cost, float)
        assert cost > 0.0

    def test_haiku_cost_is_positive(self):
        cost = compute_cost_usd("claude-code", "claude-haiku-4-5", tokens_in=1000, tokens_out=500)
        assert isinstance(cost, float)
        assert cost > 0.0

    def test_both_tokens_none_returns_zero(self):
        cost = compute_cost_usd("claude-code", "claude-opus-4-7", tokens_in=None, tokens_out=None)
        assert cost == 0.0

    def test_tokens_in_none_returns_zero(self):
        cost = compute_cost_usd("claude-code", "claude-opus-4-7", tokens_in=None, tokens_out=500)
        assert cost == 0.0

    def test_tokens_out_none_returns_zero(self):
        cost = compute_cost_usd("claude-code", "claude-opus-4-7", tokens_in=1000, tokens_out=None)
        assert cost == 0.0

    def test_unknown_provider_returns_zero_not_raise(self):
        cost = compute_cost_usd("nonexistent-provider-xyz", "some-model", tokens_in=1000, tokens_out=500)
        assert cost == 0.0

    def test_unknown_model_returns_zero_not_raise(self):
        cost = compute_cost_usd("claude-code", "nonexistent-model-xyz-99", tokens_in=1000, tokens_out=500)
        assert cost == 0.0

    def test_haiku_cheaper_than_opus_same_tokens(self):
        haiku = compute_cost_usd("claude-code", "claude-haiku-4-5", tokens_in=1000, tokens_out=500)
        opus = compute_cost_usd("claude-code", "claude-opus-4-7", tokens_in=1000, tokens_out=500)
        assert haiku < opus

    def test_zero_tokens_returns_zero(self):
        cost = compute_cost_usd("claude-code", "claude-opus-4-7", tokens_in=0, tokens_out=0)
        assert cost == 0.0

    def test_cost_scales_with_token_count(self):
        small = compute_cost_usd("claude-code", "claude-opus-4-7", tokens_in=100, tokens_out=50)
        large = compute_cost_usd("claude-code", "claude-opus-4-7", tokens_in=1000, tokens_out=500)
        assert large > small
