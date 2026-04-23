from __future__ import annotations

from pathlib import Path

import pytest

from mas.adapters.opencode import OpenCodeAdapter
from mas.schemas import ProviderConfig, RoleConfig


def _adapter(
    *,
    permission_mode: str | None = None,
    allowed_tools: list[str] | None = None,
    model: str | None = None,
    cli: str = "opencode",
    extra_args: list[str] | None = None,
) -> OpenCodeAdapter:
    provider_cfg = ProviderConfig(cli=cli, extra_args=extra_args or [])
    role_cfg = RoleConfig(
        provider="opencode",
        model=model,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
    )
    return OpenCodeAdapter(provider_cfg, role_cfg)


_DUMMY_PATH = Path("/tmp")


def test_prompt_passed_as_positional():
    cmd = _adapter().build_command("my prompt", _DUMMY_PATH, _DUMMY_PATH)
    assert cmd[0] == "opencode"
    assert cmd[1] == "run"
    assert cmd[2] == "my prompt"


def test_model_flag():
    cmd = _adapter(model="anthropic/claude-sonnet-4-5").build_command(
        "do it", _DUMMY_PATH, _DUMMY_PATH
    )
    assert "-m" in cmd
    assert cmd[cmd.index("-m") + 1] == "anthropic/claude-sonnet-4-5"


def test_no_model_flag_when_unset():
    cmd = _adapter().build_command("do it", _DUMMY_PATH, _DUMMY_PATH)
    assert "-m" not in cmd


def test_bypass_permissions_maps_to_dangerously_skip():
    cmd = _adapter(permission_mode="bypassPermissions").build_command(
        "do it", _DUMMY_PATH, _DUMMY_PATH
    )
    assert "--dangerously-skip-permissions" in cmd


def test_other_permission_modes_omit_flag():
    for mode in ("acceptEdits", "default", "plan", None):
        cmd = _adapter(permission_mode=mode).build_command(
            "do it", _DUMMY_PATH, _DUMMY_PATH
        )
        assert "--dangerously-skip-permissions" not in cmd


def test_external_task_dir_adds_dangerously_skip(tmp_path):
    cwd = tmp_path / "worktree"
    task_dir = tmp_path / "tasks" / "doing" / "task-abc"
    cmd = _adapter().build_command("do it", task_dir, cwd)
    assert "--dangerously-skip-permissions" in cmd


def test_internal_task_dir_omits_dangerously_skip(tmp_path):
    cwd = tmp_path / "worktree"
    task_dir = cwd / "subtask"
    cmd = _adapter().build_command("do it", task_dir, cwd)
    assert "--dangerously-skip-permissions" not in cmd


def test_extra_args_appended():
    cmd = _adapter(extra_args=["--format", "json"]).build_command(
        "do it", _DUMMY_PATH, _DUMMY_PATH
    )
    assert "--format" in cmd
    assert cmd[cmd.index("--format") + 1] == "json"


def test_custom_cli_binary():
    cmd = _adapter(cli="/opt/bin/opencode").build_command(
        "do it", _DUMMY_PATH, _DUMMY_PATH
    )
    assert cmd[0] == "/opt/bin/opencode"


def test_registry_contains_opencode():
    from mas.adapters import REGISTRY
    assert "opencode" in REGISTRY
    assert REGISTRY["opencode"] is OpenCodeAdapter
