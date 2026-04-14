from __future__ import annotations

import abc
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..schemas import ProviderConfig, RoleConfig


@dataclass
class DispatchHandle:
    pid: int
    provider: str
    role: str
    task_dir: Path
    log_path: Path


class Adapter(abc.ABC):
    """Given a task workspace, launch the provider CLI as a detached subprocess.

    The process is expected to write `result.json` into `task_dir` before exiting.
    Stdout + stderr are captured to `log_path`. Agentic adapters pass the
    rendered prompt as a one-shot input; text adapters pipe the prompt via stdin.
    """

    name: str = "base"
    agentic: bool = True

    def __init__(self, provider_cfg: ProviderConfig, role_cfg: RoleConfig) -> None:
        self.provider_cfg = provider_cfg
        self.role_cfg = role_cfg

    @abc.abstractmethod
    def build_command(self, prompt: str, task_dir: Path, cwd: Path) -> list[str]: ...

    def dispatch(
        self,
        prompt: str,
        task_dir: Path,
        cwd: Path,
        log_path: Path,
        role: str,
        stdin_text: str | None = None,
    ) -> DispatchHandle:
        cmd = self.build_command(prompt, task_dir, cwd)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = log_path.open("ab")
        env = self._env()
        env["MAS_ROLE"] = role
        env["MAS_TASK_DIR"] = str(task_dir)
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
                start_new_session=True,  # detach: survives parent exit
                env=env,
            )
            if stdin_text is not None and proc.stdin is not None:
                try:
                    proc.stdin.write(stdin_text.encode())
                finally:
                    proc.stdin.close()
        finally:
            log_fh.close()
        return DispatchHandle(
            pid=proc.pid,
            provider=self.name,
            role=role,
            task_dir=task_dir,
            log_path=log_path,
        )

    def _env(self) -> dict[str, str]:
        return dict(os.environ)
