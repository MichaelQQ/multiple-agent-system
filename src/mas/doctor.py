from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TypedDict


class CheckRecord(TypedDict):
    group: str
    name: str
    status: str  # "OK" | "WARN" | "FAIL"
    detail: str


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _parse_worktree_list(output: str) -> list[dict[str, str]]:
    worktrees: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            if current:
                worktrees.append(current)
                current = {}
        else:
            parts = stripped.split(" ", 1)
            key = parts[0].lower()
            value = parts[1] if len(parts) > 1 else "true"
            current[key] = value
    if current:
        worktrees.append(current)
    return worktrees


def _check_config(mas_dir: Path) -> list[CheckRecord]:
    from .config import load_config, validate_config
    from .errors import ConfigValidationError

    try:
        cfg = load_config(mas_dir)
    except ConfigValidationError as exc:
        if exc.errors:
            return [
                {
                    "group": "Config",
                    "name": f"config:{e['field']}",
                    "status": "FAIL",
                    "detail": e["message"],
                }
                for e in exc.errors
            ]
        return [{"group": "Config", "name": "config", "status": "FAIL", "detail": str(exc)}]
    except Exception as exc:
        return [{"group": "Config", "name": "config", "status": "FAIL", "detail": str(exc)}]

    issues = validate_config(cfg, mas_dir)
    if issues:
        return [
            {
                "group": "Config",
                "name": f"config:{issue.field}",
                "status": "FAIL",
                "detail": issue.message,
            }
            for issue in issues
        ]
    return [{"group": "Config", "name": "config", "status": "OK", "detail": "Config and roles are valid"}]


def _check_providers(mas_dir: Path) -> list[CheckRecord]:
    import shutil as _shutil

    from .config import load_config
    from .errors import ConfigValidationError

    try:
        cfg = load_config(mas_dir)
    except (ConfigValidationError, Exception):
        return [
            {
                "group": "Provider",
                "name": "providers",
                "status": "WARN",
                "detail": "Config failed to load; cannot check providers",
            }
        ]

    if not cfg.providers:
        return [
            {"group": "Provider", "name": "providers", "status": "OK", "detail": "No providers configured"}
        ]

    used_providers = {role_cfg.provider for role_cfg in cfg.roles.values()}
    checks: list[CheckRecord] = []

    for name, prov_cfg in cfg.providers.items():
        if name not in used_providers:
            continue
        cli = prov_cfg.cli
        if _shutil.which(cli) is None:
            checks.append(
                {
                    "group": "Provider",
                    "name": f"provider:{name}",
                    "status": "FAIL",
                    "detail": f"CLI '{cli}' not found in PATH",
                }
            )
        else:
            checks.append(
                {
                    "group": "Provider",
                    "name": f"provider:{name}",
                    "status": "OK",
                    "detail": f"CLI '{cli}' found",
                }
            )

    if not checks:
        return [
            {
                "group": "Provider",
                "name": "providers",
                "status": "OK",
                "detail": "No used providers to check",
            }
        ]
    return checks


def _check_board_worktree(mas_dir: Path) -> list[CheckRecord]:
    checks: list[CheckRecord] = []
    proj_root = mas_dir.parent

    orphan_checks: list[CheckRecord] = []
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=str(proj_root),
            check=False,
            timeout=10,
        )
        if result.returncode == 0:
            for wt in _parse_worktree_list(result.stdout):
                branch = wt.get("branch", "")
                if not branch.startswith("refs/heads/mas/"):
                    continue
                task_id = branch[len("refs/heads/mas/"):]
                task_exists = any(
                    (mas_dir / "tasks" / col / task_id).exists()
                    for col in ("proposed", "doing", "done", "failed")
                )
                if not task_exists:
                    orphan_checks.append(
                        {
                            "group": "Board",
                            "name": f"worktree:{task_id}",
                            "status": "FAIL",
                            "detail": f"Orphan worktree: branch mas/{task_id} has no task dir",
                        }
                    )
    except Exception:
        pass

    if orphan_checks:
        checks.extend(orphan_checks)
    else:
        checks.append(
            {"group": "Board", "name": "worktrees", "status": "OK", "detail": "No orphan worktrees"}
        )

    pids_dir = mas_dir / "pids"
    stale_checks: list[CheckRecord] = []
    if pids_dir.exists():
        for pid_file in sorted(pids_dir.glob("*.pid")):
            try:
                pid = int(pid_file.read_text().strip())
                if not _pid_alive(pid):
                    stale_checks.append(
                        {
                            "group": "Board",
                            "name": f"pid:{pid_file.name}",
                            "status": "WARN",
                            "detail": f"Stale worker PID {pid} ({pid_file.name})",
                        }
                    )
            except (ValueError, IOError):
                pass

    if stale_checks:
        checks.extend(stale_checks)
    else:
        checks.append(
            {
                "group": "Board",
                "name": "pids",
                "status": "OK",
                "detail": "No stale worker PID files",
            }
        )

    return checks


def _check_daemon(mas_dir: Path) -> list[CheckRecord]:
    pid_path = mas_dir / "daemon.pid"
    if not pid_path.exists():
        return [{"group": "Daemon", "name": "daemon", "status": "OK", "detail": "No daemon.pid file"}]
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, IOError) as exc:
        return [
            {
                "group": "Daemon",
                "name": "daemon",
                "status": "FAIL",
                "detail": f"daemon.pid unreadable: {exc}",
            }
        ]
    if _pid_alive(pid):
        return [
            {"group": "Daemon", "name": "daemon", "status": "OK", "detail": f"Daemon running (pid {pid})"}
        ]
    return [
        {
            "group": "Daemon",
            "name": "daemon",
            "status": "FAIL",
            "detail": f"Stale daemon.pid: PID {pid} is not alive",
        }
    ]


def run_checks(mas_dir: Path) -> list[CheckRecord]:
    checks: list[CheckRecord] = []
    checks.extend(_check_config(mas_dir))
    checks.extend(_check_providers(mas_dir))
    checks.extend(_check_board_worktree(mas_dir))
    checks.extend(_check_daemon(mas_dir))
    return checks
