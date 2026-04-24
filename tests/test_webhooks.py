"""Failing tests encoding the webhooks feature.

These tests import from their intended future locations:
  - WebhookConfig  from mas.schemas
  - fire_webhooks  from mas.notify
  - board.move()   from mas.board

All tests exercise board.move() with a `webhooks` argument and use
monkeypatch on urllib.request.urlopen; no real network calls are made.

Tests fail semantically against the stubs because:
  1. Stub always calls urlopen (even on empty list / mismatched events).
  2. Stub sends {"stub": True} instead of the required payload fields.
  3. Stub swallows webhook errors without logging WARNING.
  4. validate_config stub does not check webhook URL schemas.
"""
from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from mas import board
from mas.config import validate_config
from mas.notify import fire_webhooks  # noqa: F401 — confirms import resolves
from mas.schemas import MasConfig, ProviderConfig, RoleConfig, Task, WebhookConfig

TASK_ID = "20260424-webhook-1a2b"


def _setup(
    mas_dir: Path,
    *,
    task_id: str = TASK_ID,
    role: str = "implementer",
    goal: str = "build the thing",
    result: dict | None = None,
) -> tuple[Path, Path]:
    src = board.task_dir(mas_dir, "doing", task_id)
    board.write_task(src, Task(id=task_id, role=role, goal=goal))
    if result is not None:
        (src / "result.json").write_text(json.dumps(result))
    dst = board.task_dir(mas_dir, "done", task_id)
    return src, dst


# ---------------------------------------------------------------------------
# Case 1: empty webhooks list — urlopen must never be called
# ---------------------------------------------------------------------------

class TestEmptyWebhooks:
    def test_no_urlopen_on_empty_list(self, tmp_board: Path, monkeypatch):
        calls: list = []
        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: calls.append(a))

        src, dst = _setup(tmp_board)
        board.move(src, dst, reason="done", webhooks=[])

        assert len(calls) == 0, (
            f"expected urlopen never called with empty webhooks, got {len(calls)} call(s)"
        )


# ---------------------------------------------------------------------------
# Case 2: single matching webhook — one POST with the correct body fields
# ---------------------------------------------------------------------------

class TestMatchingWebhook:
    def test_single_post_correct_body(self, tmp_board: Path, monkeypatch):
        captured: list[urllib.request.Request] = []

        def fake_urlopen(req, *args, **kwargs):
            captured.append(req)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        role = "implementer"
        goal = "build the thing"
        result_payload = {
            "task_id": TASK_ID,
            "status": "success",
            "summary": "all done",
            "duration_s": 1.0,
        }
        src, dst = _setup(tmp_board, role=role, goal=goal, result=result_payload)
        webhook = WebhookConfig(url="http://example.com/hook", events=["doing->done"])

        board.move(src, dst, reason="done", webhooks=[webhook])

        assert len(captured) == 1, f"expected exactly 1 POST, got {len(captured)}"

        body = json.loads(captured[0].data)

        # All required fields must be present with correct values.
        assert body.get("task_id") == TASK_ID
        assert body.get("role") == role
        assert body.get("goal") == goal
        assert body.get("from") == "doing"
        assert body.get("to") == "done"
        assert body.get("summary") == "all done"
        assert body.get("status") == "success"
        assert "timestamp" in body, "payload must include a timestamp field"
        assert "task_dir" in body, "payload must include a task_dir field"


# ---------------------------------------------------------------------------
# Case 3: webhook with events=["failed"] must NOT fire on doing->done
# ---------------------------------------------------------------------------

class TestWebhookEventFilter:
    def test_failed_only_webhook_not_fired_on_done(self, tmp_board: Path, monkeypatch):
        calls: list = []
        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: calls.append(a))

        src, dst = _setup(tmp_board)
        webhook = WebhookConfig(url="http://example.com/hook", events=["failed"])

        board.move(src, dst, reason="done", webhooks=[webhook])

        assert len(calls) == 0, (
            "webhook with events=['failed'] must not fire on a doing->done transition"
        )


# ---------------------------------------------------------------------------
# Case 4: webhook delivery errors must not prevent board.move() from completing;
#         a WARNING must be logged for each failure type.
# ---------------------------------------------------------------------------

class TestWebhookErrorHandling:
    @pytest.mark.parametrize("exc", [
        urllib.error.URLError("connection refused"),
        urllib.error.HTTPError("http://x", 500, "Server Error", {}, None),
        socket.timeout("timed out"),
    ])
    def test_move_survives_webhook_error_and_logs_warning(
        self, exc, tmp_board: Path, monkeypatch, caplog
    ):
        def bad_urlopen(*a, **k):
            raise exc

        monkeypatch.setattr(urllib.request, "urlopen", bad_urlopen)

        src, dst = _setup(tmp_board)
        webhook = WebhookConfig(url="http://example.com/hook", events=["doing->done"])

        with caplog.at_level(logging.WARNING, logger="mas"):
            result = board.move(src, dst, reason="done", webhooks=[webhook])

        assert result == dst, "board.move() must return dst even when webhook delivery fails"
        assert dst.exists(), "task directory must exist at dst after move"
        assert any(r.levelname == "WARNING" for r in caplog.records), (
            f"expected a WARNING log entry when webhook delivery raises {type(exc).__name__}"
        )


# ---------------------------------------------------------------------------
# Case 5: invalid URL schema in webhooks config must produce a ValidationIssue
# ---------------------------------------------------------------------------

class TestWebhookInvalidUrlSchema:
    def test_invalid_url_schema_produces_validation_issue(
        self, tmp_board: Path, monkeypatch
    ):
        import shutil as _shutil

        # Prevent unrelated CLI-not-found issues from interfering.
        monkeypatch.setattr(_shutil, "which", lambda _: "/usr/bin/fake")

        (tmp_board / "prompts").mkdir(exist_ok=True)
        (tmp_board / "prompts" / "implementer.md").write_text("# prompt")

        cfg = MasConfig(
            providers={"fake": ProviderConfig(cli="fake")},
            roles={"implementer": RoleConfig(provider="fake")},
            webhooks=[WebhookConfig(url="ftp://invalid-schema.example.com/hook")],
        )

        issues = validate_config(cfg, tmp_board)

        issue_text = " ".join(
            f"{i.field} {i.message}".lower() for i in issues
        )
        assert "webhook" in issue_text or "url" in issue_text, (
            f"expected a ValidationIssue about invalid webhook URL schema, "
            f"got: {issues!r}"
        )
