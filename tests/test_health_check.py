from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from mas import board
from mas.adapters import AdapterUnavailableError
from mas.adapters.base import Adapter
from mas.adapters.claude_code import ClaudeCodeAdapter
from mas.adapters.codex import CodexAdapter
from mas.adapters.gemini_cli import GeminiCliAdapter
from mas.adapters.ollama import OllamaAdapter
from mas.adapters.opencode import OpenCodeAdapter
from mas.schemas import MasConfig, ProviderConfig, RoleConfig, Task
from mas.tick import TickEnv, _dispatch_role


def _provider_cfg(cli: str) -> ProviderConfig:
    return ProviderConfig(cli=cli, max_concurrent=1, extra_args=[])


def _role_cfg(provider: str) -> RoleConfig:
    return RoleConfig(provider=provider)


@pytest.mark.parametrize(
    ("adapter_cls", "provider_name", "cli"),
    [
        (ClaudeCodeAdapter, "claude-code", "claude"),
        (CodexAdapter, "codex", "codex"),
        (GeminiCliAdapter, "gemini", "gemini"),
        (OllamaAdapter, "ollama", "ollama"),
        (OpenCodeAdapter, "opencode", "opencode"),
    ],
)
def test_health_check_returns_false_when_cli_missing(adapter_cls, provider_name, cli):
    adapter = adapter_cls(_provider_cfg(cli), _role_cfg(provider_name))
    with patch("shutil.which", return_value=None):
        assert adapter.health_check() is False


@pytest.mark.parametrize(
    ("adapter_cls", "provider_name", "cli"),
    [
        (ClaudeCodeAdapter, "claude-code", "claude"),
        (CodexAdapter, "codex", "codex"),
        (GeminiCliAdapter, "gemini", "gemini"),
        (OllamaAdapter, "ollama", "ollama"),
        (OpenCodeAdapter, "opencode", "opencode"),
    ],
)
def test_health_check_returns_true_when_cli_is_found(adapter_cls, provider_name, cli):
    adapter = adapter_cls(_provider_cfg(cli), _role_cfg(provider_name))
    with patch("shutil.which", return_value=f"/usr/bin/{cli}"), patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess([cli, "--version"], 0),
    ) as mock_run:
        assert adapter.health_check() is True
    mock_run.assert_called_once()


def test_dispatch_raises_adapter_unavailable_error_when_cli_missing(tmp_path: Path):
    adapter = ClaudeCodeAdapter(_provider_cfg("claude"), _role_cfg("claude-code"))
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    log_path = task_dir / "logs" / "dispatch.log"

    with patch("shutil.which", return_value=None):
        with pytest.raises(AdapterUnavailableError) as exc_info:
            adapter.dispatch("prompt", task_dir, tmp_path, log_path, role="implementer")

    assert "claude" in str(exc_info.value).lower()


def test_adapter_unavailable_error_exported_from_package():
    from mas.adapters import AdapterUnavailableError as exported

    assert exported is AdapterUnavailableError


def test_dispatch_role_handles_adapter_unavailable(tmp_path: Path):
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    task = Task(
        id="20260422-health-check-abcd",
        role="proposer",
        goal="verify unavailable adapter handling",
    )
    task_dir = board.task_dir(mas, "doing", task.id)
    cfg = MasConfig(
        providers={"mock": ProviderConfig(cli="mock", max_concurrent=1, extra_args=[])},
        roles={
            "proposer": RoleConfig(provider="mock"),
            "orchestrator": RoleConfig(provider="mock"),
            "implementer": RoleConfig(provider="mock"),
            "tester": RoleConfig(provider="mock"),
            "evaluator": RoleConfig(provider="mock"),
        },
    )
    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)

    class FailingAdapter(Adapter):
        name = "mock"
        agentic = True

        def build_command(self, prompt: str, task_dir: Path, cwd: Path) -> list[str]:
            return ["true"]

        def health_check(self) -> bool:
            return True

        def dispatch(self, prompt, task_dir, cwd, log_path, role, stdin_text=None):
            raise AdapterUnavailableError("mock provider unavailable")

    with patch("mas.tick.get_adapter", return_value=FailingAdapter):
        _dispatch_role(env, task, task_dir, tmp_path, role="proposer")

    failed_dir = board.task_dir(mas, "failed", task.id)
    assert failed_dir.exists()
    result = json.loads((failed_dir / "result.json").read_text())
    assert result["status"] == "failure"
    assert "mock" in result["summary"].lower()
