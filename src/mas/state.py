"""Persistent per-parent state, accumulated across child completions.

Lives at `parent_dir/state.json`. Tracks worktree files touched, the canonical
test command, last-known-green commit SHA, accepted artifacts, and rejected
attempts so subsequent role prompts can see what's been tried.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .schemas import Result, SubtaskSpec

log = logging.getLogger("mas.state")


class RejectedAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subtask_id: str
    role: str
    status: str
    summary: str
    attempt: int = 1


class ParentState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worktree_files_touched: list[str] = Field(default_factory=list)
    test_command: str | None = None
    last_known_green_sha: str | None = None
    accepted_artifacts: list[str] = Field(default_factory=list)
    rejected_attempts: list[RejectedAttempt] = Field(default_factory=list)


def state_path(parent_dir: Path) -> Path:
    return parent_dir / "state.json"


def read_state(parent_dir: Path) -> ParentState:
    p = state_path(parent_dir)
    if not p.exists():
        return ParentState()
    try:
        return ParentState.model_validate_json(p.read_text())
    except Exception:
        log.warning("invalid state.json, starting fresh", extra={"path": str(p)})
        return ParentState()


def write_state(parent_dir: Path, state: ParentState) -> None:
    state_path(parent_dir).write_text(state.model_dump_json(indent=2))


def _extend_unique(seq: list[str], items: list[str]) -> None:
    existing = set(seq)
    for it in items:
        if it and it not in existing:
            seq.append(it)
            existing.add(it)


def _git_head_sha(worktree: Path) -> str | None:
    try:
        r = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    sha = r.stdout.strip()
    return sha or None


def update_state_from_result(
    parent_dir: Path,
    spec: SubtaskSpec,
    result: Result,
    *,
    worktree: Path | None = None,
    attempt: int = 1,
) -> ParentState:
    """Merge a child's result into parent state.json. Returns the updated state."""
    state = read_state(parent_dir)
    handoff: dict[str, Any] = result.handoff or {}

    cmd = handoff.get("test_command")
    if isinstance(cmd, str) and cmd:
        state.test_command = cmd

    for key in ("changed_files", "test_files", "stub_files"):
        v = handoff.get(key)
        if isinstance(v, list):
            _extend_unique(state.worktree_files_touched, [str(x) for x in v if x])
    if result.artifacts:
        _extend_unique(state.worktree_files_touched, [str(x) for x in result.artifacts])

    if spec.role == "implementer" and result.status == "success":
        cf = handoff.get("changed_files")
        if isinstance(cf, list):
            _extend_unique(state.accepted_artifacts, [str(x) for x in cf if x])

    if (
        spec.role == "evaluator"
        and result.status == "success"
        and result.verdict == "pass"
        and worktree is not None
    ):
        sha = _git_head_sha(worktree)
        if sha:
            state.last_known_green_sha = sha

    is_rejection = result.status != "success" or (
        spec.role == "evaluator" and result.verdict in ("fail", "needs_revision")
    )
    if is_rejection:
        rej_status = (
            result.verdict
            if (spec.role == "evaluator" and result.verdict in ("fail", "needs_revision"))
            else result.status
        )
        state.rejected_attempts.append(
            RejectedAttempt(
                subtask_id=spec.id,
                role=spec.role,
                status=rej_status,
                summary=result.summary or "",
                attempt=attempt,
            )
        )

    write_state(parent_dir, state)
    return state
