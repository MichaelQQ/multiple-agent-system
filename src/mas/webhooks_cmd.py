from __future__ import annotations

import json
import os
import secrets
import socket
import time
import urllib.error
from datetime import datetime, timezone
from io import StringIO
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .config import load_config  # exported so tests can monkeypatch mas.webhooks_cmd.load_config
from .notify import _post_webhook as _http_post

webhooks_app = typer.Typer(name="webhooks", no_args_is_help=True, add_completion=False)


@webhooks_app.callback()
def _root() -> None:
    """`mas webhooks` subcommand group."""


def _filter_matches(event: str, webhook_events: list[str]) -> bool:
    return event in webhook_events


def _post_webhook(
    url: str, payload: dict, timeout_s: float
) -> tuple[str, float | None, str]:
    """POST payload to url. Returns (result_str, latency_ms_or_None, detail)."""
    data = json.dumps(payload).encode()
    t0 = time.monotonic()
    try:
        with _http_post(url, data, timeout_s) as resp:
            latency_ms = (time.monotonic() - t0) * 1000
            status = resp.status
            if 200 <= status < 300:
                return "2xx", latency_ms, ""
            body = resp.read(80).decode(errors="replace")
            return str(status), latency_ms, body
    except urllib.error.HTTPError as exc:
        latency_ms = (time.monotonic() - t0) * 1000
        body = b""
        try:
            body = exc.read(80)
        except Exception:
            pass
        return str(exc.code), latency_ms, body.decode(errors="replace")
    except socket.timeout:
        latency_ms = (time.monotonic() - t0) * 1000
        return "timeout", latency_ms, ""
    except urllib.error.URLError as exc:
        latency_ms = (time.monotonic() - t0) * 1000
        if isinstance(exc.reason, socket.timeout):
            return "timeout", latency_ms, ""
        return "error", None, str(exc)
    except Exception as exc:
        return "error", None, str(exc)


def _synthetic_payload(task_dir_path: str) -> dict:
    return {
        "task_id": f"webhook-test-{secrets.token_hex(4)}",
        "role": "proposer",
        "goal": "mas webhooks test synthetic event",
        "from": "proposed",
        "to": "doing",
        "summary": "synthetic test event from `mas webhooks test`",
        "status": "success",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "task_dir": task_dir_path,
        "_synthetic": True,
    }


@webhooks_app.command()
def test(
    url: Optional[str] = typer.Option(None, "--url", help="Only test this webhook URL"),
    event: str = typer.Option("test", "--event", help="Event name to forward in the filter check"),
    timeout_s: Optional[float] = typer.Option(None, "--timeout-s", help="Override per-webhook timeout (seconds)"),
) -> None:
    """Send a synthetic test payload to configured webhooks."""
    cfg = load_config()
    webhooks = list(cfg.webhooks) if cfg.webhooks else []

    if url is not None:
        matched = [w for w in webhooks if str(w.url) == url]
        if not matched:
            typer.echo(f"error: no configured webhook with url {url}", err=True)
            raise typer.Exit(2)
        webhooks = matched

    if not webhooks:
        typer.echo("No webhooks configured.")
        raise typer.Exit(0)

    try:
        from .config import project_dir
        mas_dir = project_dir()
        task_dir_path = str(mas_dir / "tasks" / "proposed")
    except Exception:
        task_dir_path = os.path.abspath(".mas/tasks/proposed")

    payload = _synthetic_payload(task_dir_path)

    table = Table()
    table.add_column("URL")
    table.add_column("Event filter")
    table.add_column("Result")
    table.add_column("Latency")
    table.add_column("Detail")

    has_failure = False

    for webhook in webhooks:
        wh_url = str(webhook.url)
        wh_events = list(webhook.events) if hasattr(webhook, "events") else []
        event_filter_str = ", ".join(wh_events)

        if not _filter_matches(event, wh_events):
            table.add_row(wh_url, event_filter_str, "skipped", "", "")
            continue

        wh_timeout = float(timeout_s) if timeout_s is not None else float(getattr(webhook, "timeout_s", 10))
        result_str, latency_ms, detail = _post_webhook(wh_url, payload, wh_timeout)

        latency_str = f"{latency_ms:.0f}ms" if latency_ms is not None else ""
        if result_str != "2xx":
            has_failure = True

        table.add_row(wh_url, event_filter_str, result_str, latency_str, detail[:80])

    sio = StringIO()
    console = Console(file=sio, highlight=False, no_color=True, width=200)
    console.print(table)
    typer.echo(sio.getvalue(), nl=False)

    if has_failure:
        raise typer.Exit(1)
