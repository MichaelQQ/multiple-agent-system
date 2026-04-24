from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger("mas.notify")


def _event_matches(event: str, from_col: str, to_col: str) -> bool:
    if "->" in event:
        parts = event.split("->", 1)
        return parts[0] == from_col and parts[1] == to_col
    return event == to_col


def fire_webhooks(webhooks: list, payload: dict) -> None:
    if not webhooks:
        return

    from_col = payload.get("from", "")
    to_col = payload.get("to", "")

    body = {
        "task_id": payload.get("task_id"),
        "role": payload.get("role"),
        "goal": payload.get("goal"),
        "from": from_col,
        "to": to_col,
        "summary": payload.get("summary"),
        "status": payload.get("status"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "task_dir": payload.get("task_dir"),
    }
    data = json.dumps(body).encode()

    for webhook in webhooks:
        events = webhook.events if hasattr(webhook, "events") else []
        if not any(_event_matches(e, from_col, to_col) for e in events):
            continue

        url = str(webhook.url)
        timeout_s = getattr(webhook, "timeout_s", 10)
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(req, timeout=timeout_s)
        except Exception as exc:
            log.warning("webhook delivery failed %s -> %s %s: %s", from_col, to_col, url, exc)
