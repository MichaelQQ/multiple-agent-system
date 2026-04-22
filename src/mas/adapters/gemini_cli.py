from __future__ import annotations

from pathlib import Path

from .base import Adapter

_PERMISSION_MODE_MAP = {
    "bypassPermissions": "yolo",
    "acceptEdits": "auto_edit",
    "default": "default",
    "plan": "plan",
}


class GeminiCliAdapter(Adapter):
    name = "gemini"
    agentic = True

    def health_check(self) -> bool:
        cli = self.provider_cfg.cli or "gemini"
        return self._check_cli_responsive(cli, ["--version"])

    def build_command(self, prompt: str, task_dir: Path, cwd: Path) -> list[str]:
        cli = self.provider_cfg.cli or "gemini"
        args: list[str] = [cli, "-p", prompt]
        if self.role_cfg.model:
            args += ["-m", self.role_cfg.model]
        approval = _PERMISSION_MODE_MAP.get(
            self.role_cfg.permission_mode or "", "yolo"
        )
        args += ["--approval-mode", approval]
        if self.role_cfg.allowed_tools:
            for tool in self.role_cfg.allowed_tools:
                args += ["--allowed-tools", tool]
        args += list(self.provider_cfg.extra_args)
        return args
