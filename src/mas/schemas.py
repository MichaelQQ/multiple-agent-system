from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

Role = Literal["proposer", "orchestrator", "implementer", "tester", "evaluator"]
Status = Literal["success", "failure", "needs_revision"]
Verdict = Literal["pass", "fail", "needs_revision"]

_TASK_ID_PATTERN = re.compile(r"^\d{8}-[a-zA-Z0-9_-]+-[a-f0-9]{4}$")


@dataclass
class ValidationIssue:
    """Represents individual validation problem."""
    field: str
    message: str


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
    prior_results: list["Result"] = Field(default_factory=list)
    cycle: int = 0
    attempt: int = 1
    created_at: datetime = Field(default_factory=_now)

    @field_validator("id")
    @classmethod
    def _validate_task_id(cls, v: str) -> str:
        if not _TASK_ID_PATTERN.match(v):
            raise ValueError(f"Task.id must match pattern {{yyyymmdd}}-{{slug}}-{{hash4}}, got: {v}")
        return v


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
    duration_s: float | None = None
    cost_usd: float | None = None

    @field_validator("duration_s")
    @classmethod
    def _validate_duration_s(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            raise ValueError("duration_s must be non-negative")
        return v


class ProposalHandoff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str
    rationale: str | None = None
    acceptance: str | None = None
    suggested_changes: list[str] | None = None


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


class Transition(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    timestamp: str
    from_state: str = Field(alias="from")
    to_state: str = Field(alias="to")
    reason: str


class ProposerSignals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_scan: str = ""
    already_proposed: list[str] = Field(default_factory=list)
    in_progress: list[str] = Field(default_factory=list)
    recently_done: list[str] = Field(default_factory=list)
    recently_failed: list[str] = Field(default_factory=list)
    git_log: str = ""
    recent_diffs: str = ""
    ideas: str = ""
    ci_output: str = ""


class BoardSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposed: list[str]
    doing: list[str]
    done: list[str]
    failed: list[str]


class MasConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    providers: dict[str, ProviderConfig]
    roles: dict[Role, RoleConfig]
    proposer_signals: dict[str, Any] = Field(default_factory=dict)
    max_proposed: int = 10
    proposal_similarity_threshold: float = 0.7

    @field_validator("proposer_signals", mode="before")
    @classmethod
    def _none_to_empty_dict(cls, v: Any) -> Any:
        return {} if v is None else v


Task.model_rebuild()
