from __future__ import annotations

from pathlib import Path

from .base import Adapter


class ScriptAdapter(Adapter):
    name = "script"
    agentic = False

    def build_command(self, prompt: str, task_dir: Path, cwd: Path) -> list[str]:
        cli = self.provider_cfg.cli or "/bin/bash"
        script_args = self.role_cfg.extra_args if self.role_cfg.extra_args else self.provider_cfg.extra_args
        script_path = script_args[0] if script_args else ""
        if not script_path:
            raise ValueError("script provider requires script path in extra_args")
        return [cli, script_path, str(task_dir)]
