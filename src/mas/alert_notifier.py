from __future__ import annotations

import json
import logging
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone

log = logging.getLogger("mas.alert_notifier")

_sent_alert_keys: set[int] = set()

_executor = ThreadPoolExecutor(max_workers=2)


def _clear_sent_alerts() -> None:
    _sent_alert_keys.clear()


def format_slack_payload(event: dict) -> dict:
    event_type = event.get("event_type", "unknown")
    emoji = ":rotating_light:" if event_type == "cost_anomaly" else ":hourglass_flowing_sand:"
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{emoji} *MAS Alert: {event_type}*",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Task ID:*\n{event.get('task_id', 'N/A')}"},
                {"type": "mrkdwn", "text": f"*Role:*\n{event.get('role', 'N/A')}"},
                {"type": "mrkdwn", "text": f"*Reason:*\n{event.get('reason', 'N/A')}"},
                {"type": "mrkdwn", "text": f"*Cost:*\n{event.get('cost', 'N/A')}"},
                {"type": "mrkdwn", "text": f"*Timestamp:*\n{event.get('timestamp', 'N/A')}"},
            ],
        },
    ]
    return {"blocks": blocks}


def format_discord_payload(event: dict) -> dict:
    event_type = event.get("event_type", "")
    color = 0xFF0000 if event_type == "cost_anomaly" else 0xFFAA00
    embed = {
        "title": f"MAS Alert: {event_type}",
        "color": color,
        "fields": [
            {"name": "Task ID", "value": str(event.get("task_id", "N/A")), "inline": True},
            {"name": "Event Type", "value": str(event_type), "inline": True},
            {"name": "Role", "value": str(event.get("role", "N/A")), "inline": True},
            {"name": "Reason", "value": str(event.get("reason", "N/A")), "inline": False},
            {"name": "Cost", "value": str(event.get("cost", "N/A")), "inline": True},
            {"name": "Timestamp", "value": str(event.get("timestamp", "N/A")), "inline": True},
        ],
        "timestamp": event.get("timestamp", datetime.now(timezone.utc).isoformat()),
    }
    return {"embeds": [embed]}


def send_alert(alert_webhooks, event: dict) -> None:
    if id(event) in _sent_alert_keys:
        return
    _sent_alert_keys.add(id(event))

    def _post(url: str, payload: dict) -> None:
        try:
            data = json.dumps(payload).encode()
            req = urllib.request.Request(url, data=data, method="POST")
            req.headers["Content-Type"] = "application/json"
            urllib.request.urlopen(req, timeout=10)
        except Exception as exc:
            log.warning("webhook delivery failed %s: %s", url, exc)

    if alert_webhooks.slack:
        fut = _executor.submit(_post, alert_webhooks.slack, format_slack_payload(event))
        try:
            fut.result(timeout=0.15)
        except FutureTimeoutError:
            pass
    if alert_webhooks.discord:
        fut = _executor.submit(_post, alert_webhooks.discord, format_discord_payload(event))
        try:
            fut.result(timeout=0.15)
        except FutureTimeoutError:
            pass
