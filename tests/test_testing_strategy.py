"""Tests for TESTING_STRATEGY.md documentation."""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
TESTING_STRATEGY_PATH = REPO_ROOT / "TESTING_STRATEGY.md"


def get_strategy_content_or_fail():
    """Get content or fail with clear error."""
    if not TESTING_STRATEGY_PATH.exists():
        pytest.fail(f"TESTING_STRATEGY.md not found at {TESTING_STRATEGY_PATH}")
    return TESTING_STRATEGY_PATH.read_text()


class TestTestingStrategyDocument:
    """Tests validating the existence and content of TESTING_STRATEGY.md."""

    def test_testing_strategy_md_exists(self):
        """TESTING_STRATEGY.md must exist at the repo root."""
        assert TESTING_STRATEGY_PATH.exists(), (
            f"TESTING_STRATEGY.md not found at {TESTING_STRATEGY_PATH}"
        )

    def test_has_unit_tests_section(self):
        """Document must contain a 'Unit Tests' section."""
        content = get_strategy_content_or_fail()
        assert "Unit Tests" in content, (
            "TESTING_STRATEGY.md must contain a 'Unit Tests' section"
        )

    def test_has_integration_tests_section(self):
        """Document must contain an 'Integration Tests' section."""
        content = get_strategy_content_or_fail()
        assert "Integration Tests" in content, (
            "TESTING_STRATEGY.md must contain an 'Integration Tests' section"
        )

    def test_has_e2e_tests_section(self):
        """Document must contain an 'E2E Tests' or 'End-to-End' section."""
        content = get_strategy_content_or_fail()
        has_e2e = "E2E Tests" in content or "End-to-End" in content
        assert has_e2e, (
            "TESTING_STRATEGY.md must contain an 'E2E Tests' or 'End-to-End' section"
        )

    def test_mentions_board_py(self):
        """Document must mention board.py with layer assignment."""
        content = get_strategy_content_or_fail()
        assert "board.py" in content, (
            "TESTING_STRATEGY.md must mention board.py with layer assignment"
        )

    def test_mentions_tick_py(self):
        """Document must mention tick.py with layer assignment."""
        content = get_strategy_content_or_fail()
        assert "tick.py" in content, (
            "TESTING_STRATEGY.md must mention tick.py with layer assignment"
        )

    def test_mentions_adapters(self):
        """Document must mention adapters/ with layer assignment."""
        content = get_strategy_content_or_fail()
        assert "adapters/" in content, (
            "TESTING_STRATEGY.md must mention adapters/ with layer assignment"
        )

    def test_has_mocking_section(self):
        """Document must include a section on mocking external services."""
        content = get_strategy_content_or_fail()
        content_lower = content.lower()
        has_mock_section = (
            "mock" in content_lower
            or "monkeypatch" in content_lower
            or "fixture" in content_lower
        )
        assert has_mock_section, (
            "TESTING_STRATEGY.md must include a section on mocking external services "
            "(mock, monkeypatch, or fixture patterns)"
        )

    def test_mentions_ollama_or_external_api(self):
        """Document should mention Ollama or external APIs in mocking context."""
        content = get_strategy_content_or_fail()
        content_lower = content.lower()
        has_ollama = "ollama" in content_lower
        has_external = "external" in content_lower and "api" in content_lower
        assert has_ollama or has_external, (
            "TESTING_STRATEGY.md should mention Ollama/external APIs in mocking context"
        )

    def test_references_existing_test_files(self):
        """Document must reference existing test files to show awareness."""
        content = get_strategy_content_or_fail()
        content_lower = content.lower()
        referenced_files = [
            "test_board.py",
            "test_ids.py",
            "test_ollama.py",
            "test_orphan.py",
            "test_retry_marker.py",
            "test_schemas.py",
        ]
        has_any_reference = any(f in content_lower for f in referenced_files)
        assert has_any_reference, (
            "TESTING_STRATEGY.md must reference existing test files "
            "(e.g., test_board.py, test_ids.py, etc.)"
        )