from __future__ import annotations

from pathlib import Path

import pytest

from mas.adapters.gemini_cli import GeminiCliAdapter
from mas.schemas import ProviderConfig, RoleConfig


def _adapter(
    *,
    permission_mode: str | None = None,
    allowed_tools: list[str] | None = None,
    model: str | None = None,
    cli: str = "gemini",
    extra_args: list[str] | None = None,
) -> GeminiCliAdapter:
    provider_cfg = ProviderConfig(cli=cli, extra_args=extra_args or [])
    role_cfg = RoleConfig(
        provider="gemini",
        model=model,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
    )
    return GeminiCliAdapter(provider_cfg, role_cfg)


_DUMMY_PATH = Path("/tmp")


def test_default_approval_mode_is_yolo():
    cmd = _adapter().build_command("do it", _DUMMY_PATH, _DUMMY_PATH)
    assert "--approval-mode" in cmd
    idx = cmd.index("--approval-mode")
    assert cmd[idx + 1] == "yolo"


def test_explicit_permission_mode_bypass():
    cmd = _adapter(permission_mode="bypassPermissions").build_command(
        "do it", _DUMMY_PATH, _DUMMY_PATH
    )
    idx = cmd.index("--approval-mode")
    assert cmd[idx + 1] == "yolo"


def test_permission_mode_accept_edits():
    cmd = _adapter(permission_mode="acceptEdits").build_command(
        "do it", _DUMMY_PATH, _DUMMY_PATH
    )
    idx = cmd.index("--approval-mode")
    assert cmd[idx + 1] == "auto_edit"


def test_permission_mode_default():
    cmd = _adapter(permission_mode="default").build_command(
        "do it", _DUMMY_PATH, _DUMMY_PATH
    )
    idx = cmd.index("--approval-mode")
    assert cmd[idx + 1] == "default"


def test_permission_mode_plan():
    cmd = _adapter(permission_mode="plan").build_command(
        "do it", _DUMMY_PATH, _DUMMY_PATH
    )
    idx = cmd.index("--approval-mode")
    assert cmd[idx + 1] == "plan"


def test_no_yolo_flag_in_command():
    cmd = _adapter().build_command("do it", _DUMMY_PATH, _DUMMY_PATH)
    assert "--yolo" not in cmd


def test_model_flag():
    cmd = _adapter(model="gemini-2.5-pro").build_command(
        "do it", _DUMMY_PATH, _DUMMY_PATH
    )
    assert "-m" in cmd
    assert cmd[cmd.index("-m") + 1] == "gemini-2.5-pro"


def test_no_model_flag_when_unset():
    cmd = _adapter().build_command("do it", _DUMMY_PATH, _DUMMY_PATH)
    assert "-m" not in cmd


def test_allowed_tools_emitted_as_repeated_flags():
    cmd = _adapter(allowed_tools=["Bash", "Read"]).build_command(
        "do it", _DUMMY_PATH, _DUMMY_PATH
    )
    pairs = [(cmd[i], cmd[i + 1]) for i in range(len(cmd) - 1) if cmd[i] == "--allowed-tools"]
    tools = [v for _, v in pairs]
    assert tools == ["Bash", "Read"]


def test_no_allowed_tools_flag_when_unset():
    cmd = _adapter().build_command("do it", _DUMMY_PATH, _DUMMY_PATH)
    assert "--allowed-tools" not in cmd


def test_extra_args_appended():
    cmd = _adapter(extra_args=["--debug"]).build_command(
        "do it", _DUMMY_PATH, _DUMMY_PATH
    )
    assert "--debug" in cmd


def test_prompt_passed_with_p_flag():
    cmd = _adapter().build_command("my prompt", _DUMMY_PATH, _DUMMY_PATH)
    assert cmd[0] == "gemini"
    assert "-p" in cmd
    assert cmd[cmd.index("-p") + 1] == "my prompt"


def test_custom_cli_binary():
    cmd = _adapter(cli="/opt/bin/gemini").build_command(
        "do it", _DUMMY_PATH, _DUMMY_PATH
    )
    assert cmd[0] == "/opt/bin/gemini"
