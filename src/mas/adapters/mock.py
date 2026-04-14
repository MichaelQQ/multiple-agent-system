from __future__ import annotations

from pathlib import Path

from .base import Adapter


class MockAdapter(Adapter):
    """Deterministic adapter for tests/E2E smoke.

    Behaviour:
    - If provider_cfg.extra_args[0] points at an executable script, run it with
      `<task_dir>` as its single argument. The script is expected to read
      task.json from task_dir and write result.json (plus plan.json for
      orchestrator) into task_dir.
    - Otherwise, copy provider_cfg.extra_args[0] into task_dir/result.json.
    """

    name = "mock"
    agentic = True

    def build_command(self, prompt: str, task_dir: Path, cwd: Path) -> list[str]:
        if not self.provider_cfg.extra_args:
            target = task_dir / "result.json"
            return ["/bin/sh", "-c", f"echo '{{}}' > {str(target)!r}"]

        first = Path(self.provider_cfg.extra_args[0])
        if first.is_file() and first.stat().st_mode & 0o111:
            return [str(first), str(task_dir)]
        target = task_dir / "result.json"
        return ["/bin/sh", "-c", f"cp {str(first)!r} {str(target)!r}"]
