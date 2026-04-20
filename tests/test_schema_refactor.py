"""Tests for schema refactor: Pydantic models and updated return types."""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from mas.schemas import (
    BoardSummary,
    MasConfig,
    ProposerSignals,
    Transition,
)
from mas.tick import TickEnv


class TestTransition:
    """Tests for Transition model."""

    def test_construction_with_alias(self):
        """Test construction using alias from/to via dict."""
        t = Transition.model_validate({"timestamp": "2024-01-01T00:00:00Z", "from": "proposed", "to": "doing", "reason": "test"})
        assert t.from_state == "proposed"
        assert t.to_state == "doing"
        assert t.reason == "test"

    def test_construction_with_field_name(self):
        """Test construction using field name from_state/to_state."""
        t = Transition(timestamp="2024-01-01T00:00:00Z", from_state="proposed", to_state="doing", reason="test")
        assert t.from_state == "proposed"
        assert t.to_state == "doing"

    def test_validation_rejects_extra_fields(self):
        """Test that validation rejects extra fields."""
        with pytest.raises(ValidationError):
            Transition(
                timestamp="2024-01-01T00:00:00Z",
                from_state="proposed",
                to_state="doing",
                reason="test",
                extra_field="not allowed",
            )

    def test_serialization_round_trip(self):
        """Test serialization and deserialization round-trip."""
        original = Transition(
            timestamp="2024-01-01T00:00:00Z",
            from_state="proposed",
            to_state="doing",
            reason="test reason",
        )
        json_str = original.model_dump_json()
        restored = Transition.model_validate_json(json_str)
        assert restored.timestamp == original.timestamp
        assert restored.from_state == original.from_state
        assert restored.to_state == original.to_state
        assert restored.reason == original.reason


class TestProposerSignals:
    """Tests for ProposerSignals model."""

    def test_defaults(self):
        """Test default values."""
        ps = ProposerSignals()
        assert ps.repo_scan == ""
        assert ps.already_proposed == []
        assert ps.in_progress == []
        assert ps.recently_done == []
        assert ps.recently_failed == []
        assert ps.git_log == ""
        assert ps.recent_diffs == ""
        assert ps.ideas == ""
        assert ps.ci_output == ""

    def test_validation_rejects_extra_fields(self):
        """Test that validation rejects extra fields."""
        with pytest.raises(ValidationError):
            ProposerSignals(
                repo_scan="scan",
                extra_field="not allowed",
            )

    def test_model_dump_structure(self):
        """Test model_dump produces expected dict structure."""
        ps = ProposerSignals(
            repo_scan="scan result",
            already_proposed=["task-1", "task-2"],
            git_log="log output",
            recent_diffs="diff output",
            ideas="my ideas",
            ci_output="ci output",
        )
        d = ps.model_dump()
        assert d["repo_scan"] == "scan result"
        assert d["already_proposed"] == ["task-1", "task-2"]
        assert d["git_log"] == "log output"
        assert d["recent_diffs"] == "diff output"
        assert d["ideas"] == "my ideas"
        assert d["ci_output"] == "ci output"


class TestBoardSummary:
    """Tests for BoardSummary model."""

    def test_construction(self):
        """Test construction with all fields."""
        bs = BoardSummary(
            proposed=["task-1", "task-2"],
            doing=["task-3"],
            done=["task-4"],
            failed=["task-5"],
        )
        assert bs.proposed == ["task-1", "task-2"]
        assert bs.doing == ["task-3"]
        assert bs.done == ["task-4"]
        assert bs.failed == ["task-5"]

    def test_validation(self):
        """Test validation works with valid data."""
        bs = BoardSummary(proposed=[], doing=[], done=[], failed=[])
        assert bs.proposed == []
        assert bs.doing == []
        assert bs.done == []
        assert bs.failed == []


class TestTickEnv:
    """Tests for TickEnv model."""

    def test_is_pydantic_basemodel(self):
        """Test that TickEnv is a Pydantic BaseModel."""
        from pydantic import BaseModel
        assert issubclass(TickEnv, BaseModel)

    def test_construction(self):
        """Test construction with required fields."""
        cfg = MasConfig(
            providers={},
            roles={},
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            env = TickEnv(
                repo=Path(tmpdir),
                mas=Path(tmpdir) / ".mas",
                cfg=cfg,
            )
            assert env.repo == Path(tmpdir)
            assert env.mas == Path(tmpdir) / ".mas"

    def test_rejects_extra_fields(self):
        """Test that TickEnv rejects extra fields."""
        from pydantic import ValidationError
        cfg = MasConfig(
            providers={},
            roles={},
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(ValidationError):
                TickEnv(
                    repo=Path(tmpdir),
                    mas=Path(tmpdir) / ".mas",
                    cfg=cfg,
                    extra_field="not allowed",
                )


class TestTransitionsModule:
    """Tests for transitions module functions."""

    def test_read_transitions_returns_transition_objects(self):
        """Test read_transitions returns list of Transition objects with attribute access."""
        from mas import transitions

        with tempfile.TemporaryDirectory() as tmpdir:
            task_dir = Path(tmpdir)
            transitions.log_transition(task_dir, "proposed", "doing", "started")
            transitions.log_transition(task_dir, "doing", "done", "completed")

            result = transitions.read_transitions(task_dir)

            assert len(result) == 2
            assert isinstance(result[0], Transition)
            assert result[0].from_state == "proposed"
            assert result[0].to_state == "doing"
            assert result[0].reason == "started"
            assert result[1].from_state == "doing"
            assert result[1].to_state == "done"
            assert result[1].reason == "completed"

    def test_read_transitions_empty_dir(self):
        """Test read_transitions returns empty list for non-existent file."""
        from mas import transitions

        with tempfile.TemporaryDirectory() as tmpdir:
            task_dir = Path(tmpdir)
            result = transitions.read_transitions(task_dir)
            assert result == []
