from __future__ import annotations

from pathlib import Path

from .base import Adapter


class ClaudeCodeAdapter(Adapter):
    name = "claude-code"
    agentic = True

    def build_command(self, prompt: str, task_dir: Path, cwd: Path) -> list[str]:
        cli = self.provider_cfg.cli or "claude"
        args: list[str] = [cli, "-p", prompt]
        if self.role_cfg.model:
            args += ["--model", self.role_cfg.model]
        if self.role_cfg.permission_mode:
            args += ["--permission-mode", self.role_cfg.permission_mode]
        if self.role_cfg.allowed_tools:
            args += ["--allowedTools", ",".join(self.role_cfg.allowed_tools)]
        args += list(self.provider_cfg.extra_args)
        return args
