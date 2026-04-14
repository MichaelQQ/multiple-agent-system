from __future__ import annotations

from pathlib import Path

from .base import Adapter


class OllamaAdapter(Adapter):
    """Text-only provider. Tick must pipe prompt via stdin and then parse the
    JSON that the model emits on stdout into `result.json`. This adapter only
    builds the command — the post-processing is handled by roles.py which wraps
    dispatch() and writes the parsed result.
    """

    name = "ollama"
    agentic = False

    def build_command(self, prompt: str, task_dir: Path, cwd: Path) -> list[str]:
        cli = self.provider_cfg.cli or "ollama"
        model = self.role_cfg.model or "llama3.2"
        args: list[str] = [cli, "run", model]
        args += list(self.provider_cfg.extra_args)
        return args
