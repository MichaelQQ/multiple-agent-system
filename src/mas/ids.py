from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 32) -> str:
    s = _SLUG_RE.sub("-", text.lower()).strip("-")
    return s[:max_len].rstrip("-") or "task"


def task_id(goal: str, *, now: datetime | None = None, salt: str = "") -> str:
    now = now or datetime.now(timezone.utc)
    date = now.strftime("%Y%m%d")
    slug = slugify(goal)
    h = hashlib.sha256(f"{now.isoformat()}-{goal}-{salt}".encode()).hexdigest()[:4]
    return f"{date}-{slug}-{h}"
