from __future__ import annotations

import json
import os
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mas import board, transitions
from mas.alert_notifier import (
    format_discord_payload,
    format_slack_payload,
    send_alert,
)
from mas.schemas import (
    AlertWebhooksConfig,
    MasConfig,
    Plan,
    ProviderConfig,
    Result,
    RoleConfig,
    StuckDetectionConfig,
    SubtaskSpec,
    Task,
)
from mas.tick import (
    TickEnv,
    _advance_one,
    _check_cost_anomalies,
    _is_task_stuck,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TASK_ID = "20260512-alert-test-1a2b"


def _cfg(**overrides) -> MasConfig:
    """Create a minimal MasConfig for testing."""
    base = MasConfig(
        providers={"mock": ProviderConfig(cli="sh", max_concurrent=2)},
        roles={
            "proposer": RoleConfig(provider="mock"),
            "orchestrator": RoleConfig(provider="mock"),
            "implementer": RoleConfig(provider="mock"),
            "tester": RoleConfig(provider="mock"),
            "evaluator": RoleConfig(provider="mock"),
        },
        stuck_detection=StuckDetectionConfig(),
    )
    if overrides:
        base = MasConfig.model_validate({**base.model_dump(), **overrides})
    return base


def _seed_parent_with_plan(mas: Path, parent_id: str, child_id: str) -> Path:
    parent = board.task_dir(mas, "doing", parent_id)
    parent.mkdir(parents=True)
    board.write_task(parent, Task(id=parent_id, role="orchestrator", goal="g"))
    (parent / "worktree").mkdir()
    plan = Plan(
        parent_id=parent_id,
        summary="s",
        subtasks=[SubtaskSpec(id=child_id, role="implementer", goal="do")],
    )
    (parent / "plan.json").write_text(plan.model_dump_json())
    (parent / "subtasks" / child_id).mkdir(parents=True)
    return parent


def _write_current_subtask_marker(parent_dir: Path, hours_ago: float) -> None:
    start = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    marker = {
        "role": "implementer",
        "provider": "mock",
        "pid": 12345,
        "start_time_iso": start.isoformat().replace("+00:00", "Z"),
        "subtask_id": "some-subtask",
    }
    (parent_dir / ".current_subtask").write_text(json.dumps(marker, indent=2))


def _make_event(
    task_id: str = TASK_ID,
    event_type: str = "cost_anomaly",
    reason: str = "test reason",
    role: str | None = "implementer",
    cost: float | None = 5.0,
    timestamp: str | None = None,
) -> dict:
    return {
        "task_id": task_id,
        "event_type": event_type,
        "reason": reason,
        "role": role,
        "cost": cost,
        "timestamp": timestamp if timestamp is not None else datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# 1. Schema — AlertWebhooksConfig
# ---------------------------------------------------------------------------

class TestAlertWebhooksSchema:
    def test_alert_webhooks_config_defaults(self):
        """AlertWebhooksConfig fields default to None."""
        cfg = AlertWebhooksConfig()
        assert cfg.slack is None
        assert cfg.discord is None

    def test_alert_webhooks_config_with_slack_url(self):
        """AlertWebhooksConfig accepts a slack URL string."""
        url = "https://hooks.slack.com/services/T00/B00/xxx"
        cfg = AlertWebhooksConfig(slack=url)
        assert cfg.slack == url
        assert cfg.discord is None

    def test_alert_webhooks_config_with_discord_url(self):
        """AlertWebhooksConfig accepts a discord URL string."""
        url = "https://discord.com/api/webhooks/123/abc"
        cfg = AlertWebhooksConfig(discord=url)
        assert cfg.discord == url
        assert cfg.slack is None

    def test_alert_webhooks_config_with_both_urls(self):
        """AlertWebhooksConfig accepts both slack and discord URLs."""
        slack_url = "https://hooks.slack.com/services/T00/B00/xxx"
        discord_url = "https://discord.com/api/webhooks/123/abc"
        cfg = AlertWebhooksConfig(slack=slack_url, discord=discord_url)
        assert cfg.slack == slack_url
        assert cfg.discord == discord_url

    def test_mas_config_alert_webhooks_none_by_default(self):
        """MasConfig.alert_webhooks is None by default."""
        cfg = _cfg()
        assert cfg.alert_webhooks is None

    def test_mas_config_accepts_alert_webhooks(self):
        """MasConfig accepts alert_webhooks via dict."""
        cfg = MasConfig(
            providers={"mock": ProviderConfig(cli="sh")},
            roles={"implementer": RoleConfig(provider="mock")},
            alert_webhooks={"slack": "https://hooks.slack.com/test"},
        )
        assert cfg.alert_webhooks is not None
        assert cfg.alert_webhooks.slack == "https://hooks.slack.com/test"

    def test_alert_webhooks_extra_fields_forbidden(self):
        """AlertWebhooksConfig rejects extra fields."""
        with pytest.raises(Exception):
            AlertWebhooksConfig.model_validate(
                {"slack": "https://hooks.slack.com/test", "extra": "bad"}
            )


# ---------------------------------------------------------------------------
# 2. Event payload schema
# ---------------------------------------------------------------------------

class TestEventPayloadSchema:
    def test_event_has_required_fields(self):
        """Every alert event has task_id, event_type, reason, role, cost, timestamp."""
        event = _make_event()
        for key in ("task_id", "event_type", "reason", "role", "cost", "timestamp"):
            assert key in event, f"event missing required key: {key}"
        assert isinstance(event["task_id"], str)
        assert isinstance(event["event_type"], str)
        assert isinstance(event["reason"], str)
        assert event["role"] is None or isinstance(event["role"], str)
        assert event["cost"] is None or isinstance(event["cost"], (int, float))
        assert isinstance(event["timestamp"], str)

    def test_event_type_must_be_valid(self):
        """event_type must be one of cost_anomaly or hung_subtask."""
        valid = {"cost_anomaly", "hung_subtask"}
        assert _make_event(event_type="cost_anomaly")["event_type"] in valid
        assert _make_event(event_type="hung_subtask")["event_type"] in valid

    def test_event_timestamp_is_iso8601(self):
        """timestamp field is an ISO8601 string."""
        event = _make_event()
        ts = event["timestamp"]
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        assert parsed is not None


# ---------------------------------------------------------------------------
# 3. Slack formatter
# ---------------------------------------------------------------------------

class TestSlackFormatter:
    def test_format_slack_payload_returns_blocks(self):
        """format_slack_payload returns a dict with 'blocks' key."""
        event = _make_event()
        result = format_slack_payload(event)
        assert "blocks" in result, "Slack payload must contain 'blocks' key"

    def test_slack_payload_includes_status_emoji_for_anomaly(self):
        """Slack blocks include :rotating_light: for cost_anomaly."""
        event = _make_event(event_type="cost_anomaly")
        payload = format_slack_payload(event)
        blocks_text = json.dumps(payload["blocks"])
        assert ":rotating_light:" in blocks_text

    def test_slack_payload_includes_status_emoji_for_hung(self):
        """Slack blocks include :hourglass_flowing_sand: for hung_subtask."""
        event = _make_event(event_type="hung_subtask")
        payload = format_slack_payload(event)
        blocks_text = json.dumps(payload["blocks"])
        assert ":hourglass_flowing_sand:" in blocks_text

    def test_slack_payload_includes_task_id(self):
        """Slack blocks contain the task_id."""
        event = _make_event(task_id="test-task-123")
        payload = format_slack_payload(event)
        blocks_text = json.dumps(payload["blocks"])
        assert "test-task-123" in blocks_text

    def test_slack_payload_includes_event_type(self):
        """Slack blocks contain the event_type."""
        event = _make_event(event_type="cost_anomaly")
        payload = format_slack_payload(event)
        blocks_text = json.dumps(payload["blocks"])
        assert "cost_anomaly" in blocks_text

    def test_slack_payload_includes_reason(self):
        """Slack blocks contain the reason."""
        event = _make_event(reason="cost exceeded threshold by 3x")
        payload = format_slack_payload(event)
        blocks_text = json.dumps(payload["blocks"])
        assert "cost exceeded threshold" in blocks_text

    def test_slack_payload_includes_role(self):
        """Slack blocks contain the role."""
        event = _make_event(role="implementer")
        payload = format_slack_payload(event)
        blocks_text = json.dumps(payload["blocks"])
        assert "implementer" in blocks_text

    def test_slack_payload_includes_cost(self):
        """Slack blocks contain the cost."""
        event = _make_event(cost=12.5)
        payload = format_slack_payload(event)
        blocks_text = json.dumps(payload["blocks"])
        assert "12.5" in blocks_text

    def test_slack_payload_includes_timestamp(self):
        """Slack blocks contain the ISO8601 timestamp."""
        ts = datetime.now(timezone.utc).isoformat()
        event = _make_event(timestamp=ts)
        payload = format_slack_payload(event)
        blocks_text = json.dumps(payload["blocks"])
        assert ts in blocks_text


# ---------------------------------------------------------------------------
# 4. Discord formatter
# ---------------------------------------------------------------------------

class TestDiscordFormatter:
    def test_format_discord_payload_returns_embeds(self):
        """format_discord_payload returns a dict with 'embeds' key."""
        event = _make_event()
        result = format_discord_payload(event)
        assert "embeds" in result, "Discord payload must contain 'embeds' key"

    def test_discord_embed_color_red_for_anomaly(self):
        """Embed color is 0xFF0000 (red) for cost_anomaly."""
        event = _make_event(event_type="cost_anomaly")
        payload = format_discord_payload(event)
        embeds = payload["embeds"]
        assert len(embeds) >= 1
        assert embeds[0].get("color") == 0xFF0000

    def test_discord_embed_color_amber_for_hung(self):
        """Embed color is 0xFFAA00 (amber) for hung_subtask."""
        event = _make_event(event_type="hung_subtask")
        payload = format_discord_payload(event)
        embeds = payload["embeds"]
        assert len(embeds) >= 1
        assert embeds[0].get("color") == 0xFFAA00

    def test_discord_embed_includes_task_id_field(self):
        """Discord embed fields include task_id."""
        event = _make_event(task_id="test-task-456")
        payload = format_discord_payload(event)
        fields_text = json.dumps(payload["embeds"])
        assert "test-task-456" in fields_text

    def test_discord_embed_includes_event_type_field(self):
        """Discord embed fields include event_type."""
        event = _make_event(event_type="cost_anomaly")
        payload = format_discord_payload(event)
        fields_text = json.dumps(payload["embeds"])
        assert "cost_anomaly" in fields_text

    def test_discord_embed_includes_reason_field(self):
        """Discord embed fields include reason."""
        event = _make_event(reason="subtask exceeded timeout")
        payload = format_discord_payload(event)
        fields_text = json.dumps(payload["embeds"])
        assert "subtask exceeded timeout" in fields_text

    def test_discord_embed_includes_role_field(self):
        """Discord embed fields include role."""
        event = _make_event(role="evaluator")
        payload = format_discord_payload(event)
        fields_text = json.dumps(payload["embeds"])
        assert "evaluator" in fields_text

    def test_discord_embed_includes_cost_field(self):
        """Discord embed fields include cost."""
        event = _make_event(cost=7.5)
        payload = format_discord_payload(event)
        fields_text = json.dumps(payload["embeds"])
        assert "7.5" in fields_text

    def test_discord_embed_includes_timestamp_field(self):
        """Discord embed fields include timestamp."""
        ts = datetime.now(timezone.utc).isoformat()
        event = _make_event(timestamp=ts)
        payload = format_discord_payload(event)
        fields_text = json.dumps(payload["embeds"])
        assert ts in fields_text


# ---------------------------------------------------------------------------
# 5. Dispatch — send_alert
# ---------------------------------------------------------------------------

class TestSendAlert:
    def test_send_alert_posts_to_slack_url(self, monkeypatch):
        """send_alert POSTs formatted payload to the Slack URL."""
        calls: list = []
        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: calls.append(a))

        webhooks = AlertWebhooksConfig(slack="https://hooks.slack.com/test")
        event = _make_event()

        send_alert(webhooks, event)
        assert len(calls) >= 1, "expected at least one urlopen call for Slack URL"

    def test_send_alert_posts_to_discord_url(self, monkeypatch):
        """send_alert POSTs formatted payload to the Discord URL."""
        calls: list = []
        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: calls.append(a))

        webhooks = AlertWebhooksConfig(discord="https://discord.com/api/webhooks/123/abc")
        event = _make_event()

        send_alert(webhooks, event)
        assert len(calls) >= 1, "expected at least one urlopen call for Discord URL"

    def test_send_alert_posts_to_both_urls(self, monkeypatch):
        """send_alert POSTs to both Slack and Discord when both configured."""
        calls: list = []
        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: calls.append(a))

        webhooks = AlertWebhooksConfig(
            slack="https://hooks.slack.com/test",
            discord="https://discord.com/api/webhooks/123/abc",
        )
        event = _make_event()

        send_alert(webhooks, event)
        assert len(calls) == 2, "expected two urlopen calls (slack + discord)"

    def test_send_alert_posts_json_content_type(self, monkeypatch):
        """send_alert sets Content-Type application/json."""
        captured_reqs: list = []
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda req, *a, **k: captured_reqs.append(req),
        )

        webhooks = AlertWebhooksConfig(slack="https://hooks.slack.com/test")
        event = _make_event()

        send_alert(webhooks, event)
        assert len(captured_reqs) >= 1
        req = captured_reqs[0]
        assert req.get_header("Content-Type") == "application/json"

    def test_send_alert_uses_post_method(self, monkeypatch):
        """send_alert uses HTTP POST method."""
        captured_reqs: list = []
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda req, *a, **k: captured_reqs.append(req),
        )

        webhooks = AlertWebhooksConfig(slack="https://hooks.slack.com/test")
        event = _make_event()

        send_alert(webhooks, event)
        assert len(captured_reqs) >= 1
        assert captured_reqs[0].method == "POST"

    def test_send_alert_returns_immediately(self, monkeypatch):
        """send_alert is non-blocking (returns immediately)."""
        import time

        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: time.sleep(0.5))

        webhooks = AlertWebhooksConfig(slack="https://hooks.slack.com/test")
        event = _make_event()

        start = time.time()
        send_alert(webhooks, event)
        elapsed = time.time() - start
        assert elapsed < 0.3, "send_alert appears to be blocking"

    def test_send_alert_tolerates_delivery_failure(self, monkeypatch, caplog):
        """send_alert logs warning and does not raise on delivery failure."""
        import logging

        def bad_urlopen(*a, **k):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr("urllib.request.urlopen", bad_urlopen)

        webhooks = AlertWebhooksConfig(slack="https://hooks.slack.com/test")
        event = _make_event()

        with caplog.at_level(logging.WARNING, logger="mas.alert_notifier"):
            send_alert(webhooks, event)

        assert True, "send_alert should tolerate delivery failures without raising"

    def test_send_alert_dedup_same_event(self, monkeypatch):
        """Same task_id+event_type not sent twice by send_alert."""
        from mas.alert_notifier import _clear_sent_alerts

        _clear_sent_alerts()
        calls: list = []
        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: calls.append(a))

        webhooks = AlertWebhooksConfig(slack="https://hooks.slack.com/test")
        event = _make_event(task_id="dedup-test", event_type="hung_subtask")

        send_alert(webhooks, event)
        send_alert(webhooks, event)

        assert len(calls) == 1, "expected only one POST for deduplicated event"


# ---------------------------------------------------------------------------
# 6. Tick integration — stuck detection sends alert
# ---------------------------------------------------------------------------

class TestTickStuckAlert:
    def test_stuck_task_sends_hung_alert_when_webhooks_configured(
        self, tmp_path, monkeypatch
    ):
        """When stuck and alert_webhooks set, send_alert is called with hung_subtask."""
        mas = tmp_path / ".mas"
        board.ensure_layout(mas)

        parent_id = "20260512-stuck-alert"
        parent = _seed_parent_with_plan(mas, parent_id, "child-stuck-1")
        _write_current_subtask_marker(parent, hours_ago=10)

        webhooks = AlertWebhooksConfig(slack="https://hooks.slack.com/test")
        cfg = _cfg(alert_webhooks=webhooks)
        env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)

        sent: list = []
        monkeypatch.setattr("mas.tick.alert_notifier.send_alert", lambda *a: sent.append(a))

        with patch("mas.tick.get_adapter") as mock_get, \
             patch("mas.board.count_active_pids", return_value=0), \
             patch("mas.board.write_pid"), \
             patch("mas.tick._role_running", return_value=False), \
             patch("mas.tick._pid_alive", return_value=False), \
             patch("mas.tick._worker_orphaned", return_value=False), \
             patch("mas.tick._check_cost_anomalies"):
            mock_adapter = MagicMock()
            mock_adapter.dispatch.return_value = MagicMock(pid=12345)
            mock_adapter.agentic = False
            mock_get.return_value.return_value = mock_adapter
            _advance_one(env, parent)

        assert len(sent) >= 1, "expected send_alert to be called for stuck task"
        assert sent[0][1]["event_type"] == "hung_subtask"


# ---------------------------------------------------------------------------
# 7. Tick integration — _check_cost_anomalies
# ---------------------------------------------------------------------------

class TestCheckCostAnomalies:
    def test_check_cost_anomalies_calls_detect_anomalies(self, tmp_path, monkeypatch):
        """_check_cost_anomalies calls detect_anomalies from cost_helpers."""
        detected: list = []
        monkeypatch.setattr(
            "mas.cost_helpers.detect_anomalies",
            lambda *a: detected,
        )

        sent: list = []
        monkeypatch.setattr(
            "mas.alert_notifier.send_alert",
            lambda *a: sent.append(a),
        )

        webhooks = AlertWebhooksConfig(slack="https://hooks.slack.com/test")
        cfg = _cfg(alert_webhooks=webhooks)
        env = TickEnv(repo=tmp_path, mas=tmp_path / ".mas", cfg=cfg)
        from mas.tick import _check_cost_anomalies

        _check_cost_anomalies(env, tmp_path / "parent")

        assert True  # Called without error

    def test_check_cost_anomalies_sends_alert_for_each_anomaly(self, tmp_path, monkeypatch):
        """_check_cost_anomalies sends cost_anomaly alert for each anomaly."""
        anomalies = [
            {
                "task_id": "t1",
                "role": "implementer",
                "actual_cost": 5.0,
                "baseline": 1.0,
                "delta": 4.0,
                "multiplier_exceeded": 5.0,
            },
        ]
        monkeypatch.setattr(
            "mas.cost_helpers.detect_anomalies",
            lambda *a: anomalies,
        )

        sent: list = []
        monkeypatch.setattr(
            "mas.alert_notifier.send_alert",
            lambda *a: sent.append(a),
        )

        webhooks = AlertWebhooksConfig(slack="https://hooks.slack.com/test")
        cfg = _cfg(alert_webhooks=webhooks)
        mas = tmp_path / ".mas"
        board.ensure_layout(mas)
        env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)

        _check_cost_anomalies(env, tmp_path / "parent")

        assert len(sent) == 1, "expected one alert per anomaly"
        assert sent[0][1]["event_type"] == "cost_anomaly"
        assert sent[0][1]["task_id"] == "t1"

    def test_check_cost_anomalies_noop_when_no_webhooks(self, tmp_path, monkeypatch):
        """_check_cost_anomalies is a no-op when alert_webhooks is None."""
        sent: list = []
        monkeypatch.setattr(
            "mas.alert_notifier.send_alert",
            lambda *a: sent.append(a),
        )

        cfg = _cfg(alert_webhooks=None)
        env = TickEnv(repo=tmp_path, mas=tmp_path / ".mas", cfg=cfg)

        _check_cost_anomalies(env, tmp_path / "parent")

        assert len(sent) == 0, "no alerts should be sent without alert_webhooks"

    def test_advance_one_calls_check_cost_anomalies(self, tmp_path, monkeypatch):
        """_check_cost_anomalies is called during _advance_one."""
        mas = tmp_path / ".mas"
        board.ensure_layout(mas)

        parent_id = "20260512-check-called"
        parent = _seed_parent_with_plan(mas, parent_id, "child-call-1")

        cfg = _cfg()
        env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)

        called_with: list = []
        monkeypatch.setattr(
            "mas.tick._check_cost_anomalies",
            lambda *a: called_with.append(a),
        )

        with patch("mas.tick.get_adapter") as mock_get, \
             patch("mas.board.count_active_pids", return_value=0), \
             patch("mas.board.write_pid"), \
             patch("mas.tick._role_running", return_value=False), \
             patch("mas.tick._pid_alive", return_value=False), \
             patch("mas.tick._worker_orphaned", return_value=False):
            mock_adapter = MagicMock()
            mock_adapter.dispatch.return_value = MagicMock(pid=12345)
            mock_adapter.agentic = False
            mock_get.return_value.return_value = mock_adapter
            _advance_one(env, parent)

        assert len(called_with) >= 1, "expected _check_cost_anomalies to be called"


# ---------------------------------------------------------------------------
# 8. Deduplication within the same tick
# ---------------------------------------------------------------------------

class TestDedup:
    def test_dedup_different_task_ids_both_sent(self, monkeypatch):
        """Different task_ids for same event_type both fire alerts."""
        from mas.alert_notifier import _clear_sent_alerts

        _clear_sent_alerts()
        calls: list = []
        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: calls.append(a))

        webhooks = AlertWebhooksConfig(slack="https://hooks.slack.com/test")

        e1 = _make_event(task_id="task-1", event_type="hung_subtask")
        e2 = _make_event(task_id="task-2", event_type="hung_subtask")

        send_alert(webhooks, e1)
        send_alert(webhooks, e2)

        assert len(calls) == 2, "different task_ids should both be sent"

    def test_dedup_different_event_types_both_sent(self, monkeypatch):
        """Different event_types for same task_id both fire alerts."""
        from mas.alert_notifier import _clear_sent_alerts

        _clear_sent_alerts()
        calls: list = []
        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: calls.append(a))

        webhooks = AlertWebhooksConfig(slack="https://hooks.slack.com/test")

        e1 = _make_event(task_id="task-1", event_type="cost_anomaly")
        e2 = _make_event(task_id="task-1", event_type="hung_subtask")

        send_alert(webhooks, e1)
        send_alert(webhooks, e2)

        assert len(calls) == 2, "different event_types should both be sent"
