from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

MARK_BEGIN = "# >>> mas-cron {id} >>>"
MARK_END = "# <<< mas-cron {id} <<<"


def _get_crontab() -> str:
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    except subprocess.CalledProcessError:
        return ""
    if r.returncode != 0 and "no crontab" not in (r.stderr or "").lower():
        return ""
    return r.stdout or ""


def _set_crontab(content: str) -> None:
    subprocess.run(["crontab", "-"], input=content, text=True, check=True)


def _resolve_mas() -> str:
    # Prefer the currently-running mas entrypoint so cron uses the same
    # installation the user just invoked (avoids PATH lookups that may
    # resolve to an unrelated `mas` binary).
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0:
        p = Path(argv0)
        if p.is_absolute() and p.exists():
            return str(p)
        resolved = shutil.which(argv0)
        if resolved:
            return resolved
    return f"{sys.executable} -m mas.cli"


def _block(project: Path, interval_minutes: int) -> str:
    ident = _ident(project)
    schedule = f"*/{interval_minutes} * * * *"
    mas_exe = _resolve_mas()
    cmd = (
        f"cd {project} && "
        f"{{ date '+=== %Y-%m-%d %H:%M:%S ==='; {mas_exe} tick; }} "
        f">> .mas/logs/tick.log 2>&1"
    )
    return (
        f"{MARK_BEGIN.format(id=ident)}\n"
        f"{schedule} {cmd}\n"
        f"{MARK_END.format(id=ident)}\n"
    )


def _ident(project: Path) -> str:
    import hashlib
    return hashlib.sha256(str(project.resolve()).encode()).hexdigest()[:8]


def install(project: Path, interval_minutes: int = 5) -> None:
    project = project.resolve()
    current = _get_crontab()
    ident = _ident(project)
    if MARK_BEGIN.format(id=ident) in current:
        uninstall(project)
        current = _get_crontab()
    _set_crontab(current.rstrip() + "\n\n" + _block(project, interval_minutes))


def uninstall(project: Path) -> None:
    project = project.resolve()
    current = _get_crontab()
    ident = _ident(project)
    begin = MARK_BEGIN.format(id=ident)
    end = MARK_END.format(id=ident)
    lines = current.splitlines()
    out: list[str] = []
    skip = False
    for line in lines:
        if line.strip() == begin:
            skip = True
            continue
        if line.strip() == end:
            skip = False
            continue
        if not skip:
            out.append(line)
    _set_crontab("\n".join(out).rstrip() + "\n")


def status(project: Path) -> str:
    project = project.resolve()
    ident = _ident(project)
    current = _get_crontab()
    begin = MARK_BEGIN.format(id=ident)
    if begin not in current:
        return f"no cron entry for {project}"
    # Extract the block
    lines = current.splitlines()
    collecting = False
    block: list[str] = []
    for line in lines:
        if line.strip() == begin:
            collecting = True
        if collecting:
            block.append(line)
        if line.strip() == MARK_END.format(id=ident):
            break
    return "\n".join(block)
