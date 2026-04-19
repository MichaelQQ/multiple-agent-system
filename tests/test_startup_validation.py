"""Tests for the startup validation layer.

These tests verify:
1. validate_config() checks provider CLIs and role prompt templates
2. mas validate CLI command handles all validation cases
3. tick and daemon integrations call validation at startup
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from mas import cli as mas_cli
from mas.config import load_config
from mas.schemas import MasConfig, Role


@dataclass
class ValidationError:
    """Represents a validation error from validate_config."""
    field: str
    message: str


VALID_CONFIG = {
    "providers": {
        "claude-code": {
            "cli": "claude",
            "max_concurrent": 2,
            "extra_args": [],
        },
        "opencode": {
            "cli": "opencode",
            "max_concurrent": 1,
            "extra_args": [],
        },
    },
    "roles": {
        "proposer": {
            "provider": "claude-code",
            "model": "claude-haiku-4-5-20251001",
            "timeout_s": 600,
            "max_retries": 2,
        },
        "orchestrator": {
            "provider": "claude-code",
            "timeout_s": 1800,
            "max_retries": 2,
        },
        "implementer": {
            "provider": "opencode",
            "timeout_s": 3600,
            "max_retries": 2,
        },
        "tester": {
            "provider": "claude-code",
            "timeout_s": 1800,
            "max_retries": 2,
        },
        "evaluator": {
            "provider": "claude-code",
            "timeout_s": 1800,
            "max_retries": 2,
        },
    },
    "max_proposed": 10,
}


@pytest.fixture
def mas_dir(tmp_path):
    """Create a temporary .mas/ directory with config."""
    mas = tmp_path / ".mas"
    mas.mkdir()
    (mas / "prompts").mkdir()
    mas.mkdir(parents=True, exist_ok=True)
    (mas / "logs").mkdir(parents=True, exist_ok=True)
    (mas / "tasks").mkdir(parents=True, exist_ok=True)
    for col in ("proposed", "doing", "done", "failed"):
        (mas / "tasks" / col).mkdir(parents=True, exist_ok=True)
    (mas / "config.yaml").write_text(yaml.dump(VALID_CONFIG))
    (mas / "roles.yaml").write_text(yaml.dump({"roles": VALID_CONFIG["roles"]}))
    return mas


def write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data))


class TestValidateConfig:
    """Tests for the validate_config() function."""

    def test_import_validate_config(self):
        """validate_config should be importable from mas.config."""
        from mas.config import validate_config
        assert callable(validate_config)

    def test_returns_empty_list_when_valid(self, mas_dir):
        """validate_config returns empty list when all is well."""
        from mas.config import validate_config

        prompts = {
            "proposer": "proposer prompt",
            "orchestrator": "orchestrator prompt",
            "implementer": "implementer prompt",
            "tester": "tester prompt",
            "evaluator": "evaluator prompt",
        }
        for role, content in prompts.items():
            (mas_dir / "prompts" / f"{role}.md").write_text(content)

        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/mock"
            cfg = load_config(mas_dir)
            result = validate_config(cfg, mas_dir)
            assert result == []

    def test_missing_provider_cli_returns_error(self, mas_dir):
        """validate_config returns error when provider CLI is not found."""
        from mas.config import validate_config

        for role in ("proposer", "orchestrator", "implementer", "tester", "evaluator"):
            (mas_dir / "prompts" / f"{role}.md").write_text(f"{role} prompt")

        with patch("shutil.which") as mock_which:
            mock_which.return_value = None
            cfg = load_config(mas_dir)
            result = validate_config(cfg, mas_dir)
            assert len(result) > 0
            assert any("cli" in e.field.lower() or "provider" in e.message.lower() for e in result)

    def test_missing_prompt_template_returns_error(self, mas_dir):
        """validate_config returns error when prompt template is missing."""
        from mas.config import validate_config

        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/mock"
            cfg = load_config(mas_dir)
            result = validate_config(cfg, mas_dir)
            assert len(result) > 0
            assert any("template" in e.field.lower() or "prompt" in e.message.lower() for e in result)

    def test_invalid_yaml_returns_error(self, mas_dir):
        """validate_config wraps YAML errors from load_config."""
        from mas.config import validate_config

        (mas_dir / "config.yaml").write_text("invalid: yaml: content: -")

        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/mock"
            cfg = load_config(mas_dir)
            result = validate_config(cfg, mas_dir)
            assert len(result) > 0


class TestMasValidateCLI:
    """Tests for the mas validate CLI command."""

    @pytest.fixture
    def runner(self):
        return CliRunner()

    @pytest.fixture
    def fully_configured_mas_dir(self, mas_dir):
        """Create a fully configured mas_dir with prompts and CLIs."""
        prompts = {
            "proposer": "proposer prompt",
            "orchestrator": "orchestrator prompt",
            "implementer": "implementer prompt",
            "tester": "tester prompt",
            "evaluator": "evaluator prompt",
        }
        for role, content in prompts.items():
            (mas_dir / "prompts" / f"{role}.md").write_text(content)
        return mas_dir

    def test_validate_command_exists(self, runner, fully_configured_mas_dir):
        """mas validate command should exist."""
        result = runner.invoke(mas_cli.app, ["validate"])
        assert result.exit_code != 2

    def test_validate_success_exits_zero(self, runner, fully_configured_mas_dir):
        """mas validate exits 0 when all is well."""
        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/mock"
            result = runner.invoke(mas_cli.app, ["validate"], obj={"mas": fully_configured_mas_dir.parent})
            assert result.exit_code == 0
            assert "success" in result.output.lower() or "valid" in result.output.lower()

    def test_validate_missing_cli_exits_one(self, runner, fully_configured_mas_dir):
        """mas validate exits 1 when a provider CLI is missing."""
        with patch("shutil.which") as mock_which:
            mock_which.return_value = None
            result = runner.invoke(mas_cli.app, ["validate"], obj={"mas": fully_configured_mas_dir.parent})
            assert result.exit_code == 1

    def test_validate_missing_template_exits_one(self, runner, mas_dir):
        """mas validate exits 1 when a prompt template is missing."""
        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/mock"
            result = runner.invoke(mas_cli.app, ["validate"], obj={"mas": mas_dir.parent})
            assert result.exit_code == 1

    def test_validate_invalid_yaml_exits_one(self, runner, tmp_path):
        """mas validate exits 1 when config YAML is invalid."""
        bad_mas = tmp_path / ".mas"
        bad_mas.mkdir(parents=True, exist_ok=True)
        (bad_mas / "prompts").mkdir(parents=True, exist_ok=True)
        (bad_mas / "logs").mkdir(parents=True, exist_ok=True)
        (bad_mas / "tasks").mkdir(parents=True, exist_ok=True)
        for col in ("proposed", "doing", "done", "failed"):
            (bad_mas / "tasks" / col).mkdir(parents=True, exist_ok=True)
        (bad_mas / "config.yaml").write_text("invalid: yaml:")
        (bad_mas / "roles.yaml").write_text(yaml.dump({"roles": VALID_CONFIG["roles"]}))

        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/mock"
            result = runner.invoke(mas_cli.app, ["validate"], obj={"mas": tmp_path})
            assert result.exit_code == 1


class TestTickValidationIntegration:
    """Tests for validate_config integration with tick."""

    def test_run_tick_raises_on_missing_cli(self, mas_dir):
        """run_tick should raise or return early when a provider CLI is missing."""
        from mas.tick import run_tick

        prompts = {
            "proposer": "proposer prompt",
            "orchestrator": "orchestrator prompt",
            "implementer": "implementer prompt",
            "tester": "tester prompt",
            "evaluator": "evaluator prompt",
        }
        for role, content in prompts.items():
            (mas_dir / "prompts" / f"{role}.md").write_text(content)

        with patch("shutil.which") as mock_which:
            mock_which.return_value = None
            with pytest.raises(Exception):
                run_tick(start=mas_dir.parent)

    def test_run_tick_does_not_dispatch_when_validation_fails(self, mas_dir):
        """run_tick should not dispatch any work when validation fails."""
        from mas.tick import run_tick

        prompts = {
            "proposer": "proposer prompt",
            "orchestrator": "orchestrator prompt",
            "implementer": "implementer prompt",
            "tester": "tester prompt",
            "evaluator": "evaluator prompt",
        }
        for role, content in prompts.items():
            (mas_dir / "prompts" / f"{role}.md").write_text(content)

        with patch("shutil.which") as mock_which:
            mock_which.return_value = None
            try:
                run_tick(start=mas_dir.parent)
            except Exception:
                pass
            doing_tasks = list((mas_dir / "tasks" / "doing").iterdir())
            assert len(doing_tasks) == 0


class TestDaemonValidationIntegration:
    """Tests for validate_config integration with daemon."""

    def test_daemon_start_refuses_without_validation(self, mas_dir):
        """daemon.start should refuse to start when validation fails."""
        from mas.daemon import DaemonError

        prompts = {
            "proposer": "proposer prompt",
            "orchestrator": "orchestrator prompt",
            "implementer": "implementer prompt",
            "tester": "tester prompt",
            "evaluator": "evaluator prompt",
        }
        for role, content in prompts.items():
            (mas_dir / "prompts" / f"{role}.md").write_text(content)

        with patch("shutil.which") as mock_which:
            mock_which.return_value = None
            with pytest.raises(DaemonError):
                mas_cli.daemon_start(interval=1)