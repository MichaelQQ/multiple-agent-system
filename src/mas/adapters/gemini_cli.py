from __future__ import annotations

from pathlib import Path

from .base import Adapter


class GeminiCliAdapter(Adapter):
    name = "gemini"
    agentic = True

    def build_command(self, prompt: str, task_dir: Path, cwd: Path) -> list[str]:
        cli = self.provider_cfg.cli or "gemini"
        args: list[str] = [cli, "-p", prompt, "--yolo"]
        if self.role_cfg.model:
            args += ["-m", self.role_cfg.model]
        args += list(self.provider_cfg.extra_args)
        return args
