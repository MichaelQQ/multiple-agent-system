from __future__ import annotations

from pathlib import Path

from .base import Adapter


class CodexAdapter(Adapter):
    name = "codex"
    agentic = True

    def health_check(self) -> bool:
        cli = self.provider_cfg.cli or "codex"
        return self._check_cli_responsive(cli, ["--version"])

    def build_command(self, prompt: str, task_dir: Path, cwd: Path) -> list[str]:
        cli = self.provider_cfg.cli or "codex"
        args: list[str] = [cli, "exec", prompt]
        if self.role_cfg.model:
            args += ["--model", self.role_cfg.model]
        args += list(self.provider_cfg.extra_args)
        return args
