from __future__ import annotations

from pathlib import Path

from .base import Adapter


class OpenCodeAdapter(Adapter):
    name = "opencode"
    agentic = True

    def build_command(self, prompt: str, task_dir: Path, cwd: Path) -> list[str]:
        cli = self.provider_cfg.cli or "opencode"
        args: list[str] = [cli, "run", prompt]
        if self.role_cfg.model:
            args += ["-m", self.role_cfg.model]
        if self.role_cfg.permission_mode == "bypassPermissions":
            args.append("--dangerously-skip-permissions")
        args += list(self.provider_cfg.extra_args)
        return args
