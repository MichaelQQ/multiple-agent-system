"""Rejected proposal logging and querying."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator

log = logging.getLogger("mas")

_VALID_COLUMNS = frozenset({"proposed", "doing", "done", "failed"})


class RejectedProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: str
    summary: str
    goal: str
    similarity_score: float
    matched_task_id: str
    matched_column: str
    threshold: float

    @field_validator("timestamp", mode="before")
    @classmethod
    def normalise_timestamp(cls, v: str) -> str:
        if isinstance(v, str) and v.endswith("Z"):
            return v
        try:
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, TypeError):
            return v

    @field_validator("matched_column", mode="before")
    @classmethod
    def normalise_matched_column(cls, v: str) -> str:
        if v not in _VALID_COLUMNS:
            return "proposed"
        return v


def write_rejected_proposal(mas_dir: Path, record: "RejectedProposal") -> None:
    """Append a rejection record to proposals/rejected.jsonl."""
    proposals_dir = mas_dir / "proposals"
    try:
        proposals_dir.mkdir(parents=True, exist_ok=True)
        path = proposals_dir / "rejected.jsonl"
        with path.open("a") as f:
            f.write(record.model_dump_json() + "\n")
    except OSError as e:
        log.warning("failed to write rejected proposal: %s", e)


def read_rejected_proposals(
    path: Path,
    *,
    since: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Read and filter rejected proposals from a JSONL file."""
    if not path.exists():
        return []

    from .stats import parse_since

    cutoff: datetime | None = None
    if since is not None:
        delta = parse_since(since)
        cutoff = datetime.now(timezone.utc) - delta

    try:
        text = path.read_text()
    except OSError:
        return []

    records: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            rec = RejectedProposal.model_validate(data)
            records.append(rec.model_dump())
        except Exception as e:
            log.warning("skipping malformed rejected proposal line: %s", e)
            continue

    if cutoff is not None:
        filtered: list[dict] = []
        for rec in records:
            ts_str = rec.get("timestamp", "")
            try:
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if dt >= cutoff:
                    filtered.append(rec)
            except ValueError:
                filtered.append(rec)
        records = filtered

    records.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return records[:limit]
