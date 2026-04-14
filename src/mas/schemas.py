from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["proposer", "orchestrator", "implementer", "tester", "evaluator"]
Status = Literal["success", "failure", "needs_revision"]
Verdict = Literal["pass", "fail", "needs_revision"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    parent_id: str | None = None
    role: Role
    goal: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    previous_failure: str | None = None
    cycle: int = 0
    attempt: int = 1
    created_at: datetime = Field(default_factory=_now)


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    status: Status
    summary: str
    artifacts: list[str] = Field(default_factory=list)
    handoff: dict[str, Any] | None = None
    verdict: Verdict | None = None
    feedback: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    duration_s: float = 0.0
    cost_usd: float | None = None


class SubtaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    role: Role
    goal: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)


class Plan(BaseModel):
    """Orchestrator output: ordered list of children sharing the parent worktree."""

    model_config = ConfigDict(extra="forbid")

    parent_id: str
    summary: str
    subtasks: list[SubtaskSpec]
    max_revision_cycles: int = 2


class RoleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str | None = None
    timeout_s: int = 1800
    max_retries: int = 2
    allowed_tools: list[str] | None = None
    permission_mode: str | None = None


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cli: str
    max_concurrent: int = 1
    extra_args: list[str] = Field(default_factory=list)


class MasConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    providers: dict[str, ProviderConfig]
    roles: dict[Role, RoleConfig]
    proposer_signals: dict[str, Any] = Field(default_factory=dict)
    max_proposed: int = 10
