from __future__ import annotations

import abc
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..schemas import ProviderConfig, RoleConfig

log = logging.getLogger("mas.adapters")


@dataclass
class DispatchHandle:
    pid: int
    provider: str
    role: str
    task_dir: Path
    log_path: Path


class AdapterUnavailableError(RuntimeError):
    pass


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
        self._last_health_error: str | None = None

    @abc.abstractmethod
    def build_command(self, prompt: str, task_dir: Path, cwd: Path) -> list[str]: ...

    def health_check(self) -> bool:
        cli = self.provider_cfg.cli or self.name
        return self._check_cli_responsive(cli, ["--version"])

    def dispatch(
        self,
        prompt: str,
        task_dir: Path,
        cwd: Path,
        log_path: Path,
        role: str,
        stdin_text: str | None = None,
    ) -> DispatchHandle:
        if not self.health_check():
            message = self._last_health_error or f"{self.provider_cfg.cli} is unavailable"
            raise AdapterUnavailableError(message)
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
                start_new_session=True,
                env=env,
            )
            if stdin_text is not None and proc.stdin is not None:
                try:
                    proc.stdin.write(stdin_text.encode())
                finally:
                    proc.stdin.close()
        finally:
            log_fh.close()
        log.info(
            "dispatched",
            extra={"task_id": task_dir.name, "role": role, "provider": self.name, "pid": proc.pid},
        )
        return DispatchHandle(
            pid=proc.pid,
            provider=self.name,
            role=role,
            task_dir=task_dir,
            log_path=log_path,
        )

    # Strip VS Code plumbing vars that are irrelevant (or harmful) to a detached
    # subprocess.  CLAUDE_CODE_* and CLAUDECODE are intentionally kept — they carry
    # the SSE port used for auth when running inside VS Code; stripping them causes
    # "Not logged in" for users whose credentials live in the IDE session rather than
    # the system keychain.
    _STRIP_ENV_PREFIXES = ("VSCODE_", "GIT_ASKPASS")

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        for key in list(env):
            if any(key == p or key.startswith(p) for p in self._STRIP_ENV_PREFIXES):
                del env[key]
        return env

    def _check_cli_responsive(
        self,
        cli: str,
        version_args: list[str],
        *,
        timeout_s: int = 5,
    ) -> bool:
        cli_path = shutil.which(cli)
        if cli_path is None:
            self._last_health_error = f"{cli} not found in PATH"
            return False
        try:
            proc = subprocess.run(
                [cli, *version_args],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout_s,
                check=False,
                env=self._env(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._last_health_error = f"{cli} is not responsive: {exc}"
            return False
        if proc.returncode != 0:
            self._last_health_error = f"{cli} is not responsive (exit code {proc.returncode})"
            return False
        self._last_health_error = None
        return True
