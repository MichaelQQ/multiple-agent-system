from __future__ import annotations

from pathlib import Path

import pytest

from mas.adapters.claude_code import ClaudeCodeAdapter
from mas.adapters.base import DispatchHandle
from mas.schemas import ProviderConfig, RoleConfig


def _adapter(
    *,
    permission_mode: str | None = None,
    allowed_tools: list[str] | None = None,
    model: str | None = None,
    cli: str = "claude",
    extra_args: list[str] | None = None,
) -> ClaudeCodeAdapter:
    provider_cfg = ProviderConfig(cli=cli, extra_args=extra_args or [])
    role_cfg = RoleConfig(
        provider="claude",
        model=model,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
    )
    return ClaudeCodeAdapter(provider_cfg, role_cfg)


_DUMMY_PATH = Path("/tmp")


def test_default_cli_binary():
    cmd = _adapter().build_command("do it", _DUMMY_PATH, _DUMMY_PATH)
    assert cmd[0] == "claude"


def test_prompt_passed_with_p_flag():
    cmd = _adapter().build_command("my prompt", _DUMMY_PATH, _DUMMY_PATH)
    assert "-p" in cmd
    assert cmd[cmd.index("-p") + 1] == "my prompt"


def test_custom_cli_binary():
    cmd = _adapter(cli="/opt/bin/claude").build_command(
        "do it", _DUMMY_PATH, _DUMMY_PATH
    )
    assert cmd[0] == "/opt/bin/claude"


def test_model_flag_when_set():
    cmd = _adapter(model="claude-opus-4-5").build_command(
        "do it", _DUMMY_PATH, _DUMMY_PATH
    )
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-opus-4-5"


def test_model_flag_omitted_when_unset():
    cmd = _adapter().build_command("do it", _DUMMY_PATH, _DUMMY_PATH)
    assert "--model" not in cmd


def test_permission_mode_flag_when_set():
    cmd = _adapter(permission_mode="autoApprove").build_command(
        "do it", _DUMMY_PATH, _DUMMY_PATH
    )
    assert "--permission-mode" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "autoApprove"


def test_permission_mode_flag_omitted_when_unset():
    cmd = _adapter().build_command("do it", _DUMMY_PATH, _DUMMY_PATH)
    assert "--permission-mode" not in cmd


def test_allowed_tools_flag_when_set():
    cmd = _adapter(allowed_tools=["Bash", "Read", "Edit"]).build_command(
        "do it", _DUMMY_PATH, _DUMMY_PATH
    )
    assert "--allowedTools" in cmd
    tools_arg = cmd[cmd.index("--allowedTools") + 1]
    assert tools_arg == "Bash,Read,Edit"


def test_allowed_tools_flag_omitted_when_unset():
    cmd = _adapter().build_command("do it", _DUMMY_PATH, _DUMMY_PATH)
    assert "--allowedTools" not in cmd


def test_external_task_dir_adds_add_dir_flag(tmp_path):
    cwd = tmp_path / "worktree"
    task_dir = tmp_path / "tasks" / "doing" / "task-abc"
    cmd = _adapter().build_command("do it", task_dir, cwd)
    assert "--add-dir" in cmd
    assert cmd[cmd.index("--add-dir") + 1] == str(task_dir)


def test_task_dir_equals_cwd_omits_add_dir_flag(tmp_path):
    cwd = tmp_path / "worktree"
    task_dir = cwd
    cmd = _adapter().build_command("do it", task_dir, cwd)
    assert "--add-dir" not in cmd


def test_task_dir_subdirectory_of_cwd_omits_add_dir_flag(tmp_path):
    cwd = tmp_path / "worktree"
    task_dir = cwd / "subtask"
    cmd = _adapter().build_command("do it", task_dir, cwd)
    assert "--add-dir" not in cmd


def test_extra_args_appended():
    cmd = _adapter(extra_args=["--debug", "--verbose"]).build_command(
        "do it", _DUMMY_PATH, _DUMMY_PATH
    )
    assert "--debug" in cmd
    assert "--verbose" in cmd
    assert cmd.index("--verbose") == cmd.index("--debug") + 1


def test_adapter_name():
    adapter = _adapter()
    assert adapter.name == "claude-code"


def test_adapter_agentic_is_true():
    adapter = _adapter()
    assert adapter.agentic is True


def test_registry_contains_claude_code():
    from mas.adapters import REGISTRY
    assert "claude-code" in REGISTRY
    assert REGISTRY["claude-code"] is ClaudeCodeAdapter


def test_dispatch_sets_mas_role_env_var(tmp_path, monkeypatch):
    captured_env = {}

    class MockPopen:
        def __init__(self, *args, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            self.pid = 12345

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr("subprocess.Popen", MockPopen)

    adapter = _adapter()
    cwd = tmp_path / "worktree"
    task_dir = tmp_path / "task-abc"
    log_path = task_dir / "logs" / "test.log"

    adapter.dispatch("prompt", task_dir, cwd, log_path, role="implementer")

    assert "MAS_ROLE" in captured_env
    assert captured_env["MAS_ROLE"] == "implementer"


def test_dispatch_sets_mas_task_dir_env_var(tmp_path, monkeypatch):
    captured_env = {}

    class MockPopen:
        def __init__(self, *args, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            self.pid = 12345

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr("subprocess.Popen", MockPopen)

    adapter = _adapter()
    cwd = tmp_path / "worktree"
    task_dir = tmp_path / "task-abc"
    log_path = task_dir / "logs" / "test.log"

    adapter.dispatch("prompt", task_dir, cwd, log_path, role="implementer")

    assert "MAS_TASK_DIR" in captured_env
    assert captured_env["MAS_TASK_DIR"] == str(task_dir)


def test_dispatch_returns_dispatch_handle(tmp_path, monkeypatch):
    class MockPopen:
        def __init__(self, *args, **kwargs):
            self.pid = 12345

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr("subprocess.Popen", MockPopen)

    adapter = _adapter()
    cwd = tmp_path / "worktree"
    task_dir = tmp_path / "task-abc"
    log_path = task_dir / "logs" / "test.log"

    handle = adapter.dispatch("prompt", task_dir, cwd, log_path, role="implementer")

    assert isinstance(handle, DispatchHandle)
    assert handle.pid == 12345
    assert handle.provider == "claude-code"
    assert handle.role == "implementer"
    assert handle.task_dir == task_dir
    assert handle.log_path == log_path


def test_env_strips_vscode_prefixes(tmp_path, monkeypatch):
    monkeypatch.setenv("VSCODE_GIT_IPC_CHANNEL", "/fake/socket")
    monkeypatch.setenv("VSCODE_INJECTED_FEATURES", "features")
    monkeypatch.setenv("GIT_ASKPASS", "/fake/askpass")

    captured_env = {}

    class MockPopen:
        def __init__(self, *args, **kwargs):
            env = kwargs.get("env", {})
            captured_env.update(env)
            self.pid = 12345

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr("subprocess.Popen", MockPopen)

    adapter = _adapter()
    cwd = tmp_path / "worktree"
    task_dir = tmp_path / "task-abc"
    log_path = task_dir / "logs" / "test.log"

    adapter.dispatch("prompt", task_dir, cwd, log_path, role="implementer")

    assert "VSCODE_GIT_IPC_CHANNEL" not in captured_env
    assert "VSCODE_INJECTED_FEATURES" not in captured_env


def test_env_preserves_claude_code_prefixes(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_PORT", "12345")
    monkeypatch.setenv("CLAUDECODE_PROJECT_PATH", "/fake/path")

    captured_env = {}

    class MockPopen:
        def __init__(self, *args, **kwargs):
            env = kwargs.get("env", {})
            captured_env.update(env)
            self.pid = 12345

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr("subprocess.Popen", MockPopen)

    adapter = _adapter()
    cwd = tmp_path / "worktree"
    task_dir = tmp_path / "task-abc"
    log_path = task_dir / "logs" / "test.log"

    adapter.dispatch("prompt", task_dir, cwd, log_path, role="implementer")

    assert "CLAUDE_CODE_PORT" in captured_env
    assert "CLAUDECODE_PROJECT_PATH" in captured_env