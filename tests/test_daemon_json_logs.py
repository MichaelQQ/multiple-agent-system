"""
Failing tests encoding JSON-logging behavior for the MAS daemon.

These tests must fail against the current code and pass only after the
implementation:
  - Fixes JsonFormatter to emit `ts` (ISO-8601 UTC, Z-suffix), lowercase
    `level` (info/warn/error), and preserves an `event` key.
  - Adds _log_daemon_start / _log_daemon_stop / _log_config_reloaded to
    mas.daemon and wires them into start() and _run_loop().
  - Updates _run_loop to emit tick_start, tick_done, tick_error events.
"""

import json
import logging
import logging.handlers
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mas.daemon import (
    _run_loop,
    _log_daemon_start,
    _log_daemon_stop,
    _log_config_reloaded,
)
from mas.logging import setup_daemon_logging, JsonFormatter


# ─── Shared helpers ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_handlers():
    """Remove stale RotatingFileHandlers from the mas logger after each test."""
    yield
    root = logging.getLogger("mas")
    for h in list(root.handlers):
        if isinstance(h, logging.handlers.RotatingFileHandler):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass


@pytest.fixture
def mas_dir(tmp_path: Path) -> Path:
    """Minimal .mas directory accepted by load_config and _run_loop."""
    mas = tmp_path / ".mas"
    mas.mkdir()
    (mas / "logs").mkdir()
    (mas / "config.yaml").write_text(
        "providers:\n"
        "  mock:\n"
        "    cli: sh\n"
        "    max_concurrent: 1\n"
        "    extra_args: []\n"
    )
    (mas / "roles.yaml").write_text(
        "roles:\n"
        "  proposer: {provider: mock}\n"
        "  orchestrator: {provider: mock}\n"
        "  implementer: {provider: mock}\n"
        "  tester: {provider: mock}\n"
        "  evaluator: {provider: mock}\n"
    )
    return mas


def _setup_json_log(mas_dir: Path) -> Path:
    log_dir = mas_dir / "logs"
    setup_daemon_logging(log_dir, 1024 * 1024, 1, json_logs=True)
    return log_dir


def _read_json_lines(log_dir: Path) -> list[dict]:
    log_file = log_dir / "daemon.log"
    assert log_file.exists(), "daemon.log was not created"
    return [json.loads(l) for l in log_file.read_text().splitlines() if l.strip()]


# ─── Group 1: JsonFormatter output format ────────────────────────────────────


class TestJsonFormatterKeys:
    def test_ts_key_not_timestamp(self, mas_dir):
        """JsonFormatter must emit 'ts', not 'timestamp'."""
        log_dir = _setup_json_log(mas_dir)
        logging.getLogger("mas.daemon").info("probe", extra={"event": "probe"})
        data = _read_json_lines(log_dir)[0]
        assert "ts" in data, f"expected 'ts' key but got keys: {sorted(data)}"

    def test_level_lowercase_info(self, mas_dir):
        """level must be 'info' for INFO records."""
        log_dir = _setup_json_log(mas_dir)
        logging.getLogger("mas.daemon").info("probe", extra={"event": "probe"})
        data = _read_json_lines(log_dir)[0]
        assert "level" in data
        assert data["level"] == "info", f"expected 'info', got {data['level']!r}"

    def test_level_lowercase_error(self, mas_dir):
        """level must be 'error' for ERROR records."""
        log_dir = _setup_json_log(mas_dir)
        logging.getLogger("mas.daemon").error("probe", extra={"event": "probe"})
        data = _read_json_lines(log_dir)[0]
        assert data["level"] == "error", f"expected 'error', got {data['level']!r}"

    def test_level_warn_for_warning(self, mas_dir):
        """level must be 'warn' for WARNING records (not 'warning' or 'WARNING')."""
        log_dir = _setup_json_log(mas_dir)
        logging.getLogger("mas.daemon").warning("probe", extra={"event": "probe"})
        data = _read_json_lines(log_dir)[0]
        assert data["level"] == "warn", f"expected 'warn', got {data['level']!r}"

    def test_ts_ends_with_z(self, mas_dir):
        """ts must be ISO-8601 UTC ending in 'Z', e.g. 2026-04-27T12:00:00Z."""
        log_dir = _setup_json_log(mas_dir)
        logging.getLogger("mas.daemon").info("probe", extra={"event": "probe"})
        data = _read_json_lines(log_dir)[0]
        assert "ts" in data, "ts key missing"
        assert data["ts"].endswith("Z"), f"ts should end with Z, got: {data['ts']!r}"

    def test_every_line_has_ts_level_event(self, mas_dir):
        """All daemon log lines must have ts, level, and event keys."""
        log_dir = _setup_json_log(mas_dir)
        logger = logging.getLogger("mas.daemon")
        logger.info("first", extra={"event": "evt_a"})
        logger.error("second", extra={"event": "evt_b"})
        lines = _read_json_lines(log_dir)
        assert len(lines) == 2
        for line in lines:
            assert "ts" in line, f"missing 'ts' in: {line}"
            assert "level" in line, f"missing 'level' in: {line}"
            assert "event" in line, f"missing 'event' in: {line}"

    def test_extra_fields_passed_through(self, mas_dir):
        """Extra fields provided via extra= must appear in the JSON output."""
        log_dir = _setup_json_log(mas_dir)
        logging.getLogger("mas.daemon").info(
            "probe", extra={"event": "test_event", "pid": 99, "interval_s": 300}
        )
        data = _read_json_lines(log_dir)[0]
        assert data.get("pid") == 99
        assert data.get("interval_s") == 300


# ─── Group 2: daemon_start / daemon_stop lifecycle events ─────────────────────


class TestLifecycleEvents:
    def test_daemon_start_emits_event(self, mas_dir):
        """_log_daemon_start must write event=daemon_start with pid and interval_s."""
        log_dir = _setup_json_log(mas_dir)
        _log_daemon_start(pid=42, interval_s=120)
        lines = _read_json_lines(log_dir)
        events = {l["event"]: l for l in lines if "event" in l}
        assert "daemon_start" in events, (
            f"daemon_start not emitted; events found: {list(events)}"
        )
        ev = events["daemon_start"]
        assert ev.get("pid") == 42, f"pid mismatch: {ev}"
        assert ev.get("interval_s") == 120, f"interval_s mismatch: {ev}"
        assert ev.get("level") == "info", f"expected level=info: {ev}"

    def test_daemon_stop_emits_event(self, mas_dir):
        """_log_daemon_stop must write event=daemon_stop with a reason key."""
        log_dir = _setup_json_log(mas_dir)
        _log_daemon_stop(reason="signal")
        lines = _read_json_lines(log_dir)
        events = {l["event"]: l for l in lines if "event" in l}
        assert "daemon_stop" in events, (
            f"daemon_stop not emitted; events found: {list(events)}"
        )
        ev = events["daemon_stop"]
        assert "reason" in ev, f"'reason' missing from daemon_stop: {ev}"
        assert ev["reason"] == "signal"
        assert ev.get("level") == "info"


# ─── Group 3: tick_start / tick_done / tick_error events ─────────────────────


@pytest.fixture
def one_successful_tick(mas_dir):
    """Run exactly one successful tick through _run_loop; return log_dir."""
    log_dir = _setup_json_log(mas_dir)
    stop_flag = {"stop": False}

    def fake_sleep(_secs):
        stop_flag["stop"] = True

    times = iter([1000.0, 1001.5] + [1001.5] * 20)

    with (
        patch("mas.daemon.time.time", side_effect=lambda: next(times)),
        patch("mas.daemon.time.sleep", side_effect=fake_sleep),
        patch("mas.tick.run_tick"),
        patch("mas.daemon.ConfigWatcher") as mock_cw,
    ):
        mock_cw.return_value.has_changed.return_value = False
        _run_loop(mas_dir.parent, 300, stop_flag)

    return log_dir


@pytest.fixture
def one_error_tick(mas_dir):
    """Run exactly one failing tick through _run_loop; return log_dir."""
    log_dir = _setup_json_log(mas_dir)
    stop_flag = {"stop": False}

    def fake_sleep(_secs):
        stop_flag["stop"] = True

    times = iter([1000.0, 1002.0] + [1002.0] * 20)

    with (
        patch("mas.daemon.time.time", side_effect=lambda: next(times)),
        patch("mas.daemon.time.sleep", side_effect=fake_sleep),
        patch("mas.tick.run_tick", side_effect=RuntimeError("disk full")),
        patch("mas.daemon.ConfigWatcher") as mock_cw,
    ):
        mock_cw.return_value.has_changed.return_value = False
        _run_loop(mas_dir.parent, 300, stop_flag)

    return log_dir


class TestTickEvents:
    def test_tick_start_event(self, one_successful_tick):
        lines = _read_json_lines(one_successful_tick)
        events = {l.get("event"): l for l in lines if "event" in l}
        assert "tick_start" in events, (
            f"tick_start not emitted; events found: {list(events)}"
        )
        ev = events["tick_start"]
        assert "tick_num" in ev, f"tick_num missing: {ev}"
        assert ev["tick_num"] == 1
        assert ev.get("level") == "info"

    def test_tick_done_event(self, one_successful_tick):
        lines = _read_json_lines(one_successful_tick)
        events = {l.get("event"): l for l in lines if "event" in l}
        assert "tick_done" in events, (
            f"tick_done not emitted; events found: {list(events)}"
        )
        ev = events["tick_done"]
        assert ev.get("tick_num") == 1
        assert "duration_s" in ev, f"duration_s missing: {ev}"
        assert isinstance(ev["duration_s"], (int, float))
        assert ev.get("level") == "info"

    def test_tick_error_event(self, one_error_tick):
        lines = _read_json_lines(one_error_tick)
        events = {l.get("event"): l for l in lines if "event" in l}
        assert "tick_error" in events, (
            f"tick_error not emitted; events found: {list(events)}"
        )
        ev = events["tick_error"]
        assert ev.get("tick_num") == 1
        assert "error" in ev, f"error field missing: {ev}"
        assert "disk full" in ev["error"], f"unexpected error value: {ev['error']!r}"
        assert ev.get("level") == "error", f"tick_error must have level=error: {ev}"

    def test_tick_error_not_on_success(self, one_successful_tick):
        """tick_error must not appear when tick succeeds."""
        lines = _read_json_lines(one_successful_tick)
        events = {l.get("event"): l for l in lines if "event" in l}
        assert "tick_error" not in events


# ─── Group 4: config_reloaded event ──────────────────────────────────────────


class TestConfigReloadedEvent:
    def test_changes_are_dicts_not_tuples(self, mas_dir):
        """_log_config_reloaded must emit changes as list of {field,old,new} dicts."""
        log_dir = _setup_json_log(mas_dir)
        changes = [
            ("daemon.interval", "300", "60"),
            ("max_proposed", "10", "5"),
        ]
        _log_config_reloaded(changes)
        lines = _read_json_lines(log_dir)
        events = {l.get("event"): l for l in lines if "event" in l}
        assert "config_reloaded" in events, (
            f"config_reloaded not emitted; events found: {list(events)}"
        )
        ev = events["config_reloaded"]
        assert "changes" in ev, f"'changes' key missing: {ev}"
        assert isinstance(ev["changes"], list), (
            f"changes must be a list, got {type(ev['changes'])}"
        )
        for change in ev["changes"]:
            assert isinstance(change, dict), (
                f"each change must be a dict, got {type(change)}: {change!r}"
            )
            assert "field" in change
            assert "old" in change
            assert "new" in change

    def test_changes_not_python_tuple_repr(self, mas_dir):
        """The JSON must not contain Python tuple repr strings."""
        log_dir = _setup_json_log(mas_dir)
        _log_config_reloaded([("daemon.interval", "300", "60")])
        lines = _read_json_lines(log_dir)
        events = {l.get("event"): l for l in lines if "event" in l}
        assert "config_reloaded" in events
        raw = json.dumps(events["config_reloaded"].get("changes", []))
        assert "('daemon.interval'" not in raw, (
            f"found Python tuple repr in changes: {raw}"
        )

    def test_config_reloaded_level_is_info(self, mas_dir):
        """config_reloaded must have level=info."""
        log_dir = _setup_json_log(mas_dir)
        _log_config_reloaded([("daemon.interval", "300", "60")])
        lines = _read_json_lines(log_dir)
        events = {l.get("event"): l for l in lines if "event" in l}
        assert "config_reloaded" in events
        assert events["config_reloaded"].get("level") == "info"


# ─── Group 5: text format preserved when json_logs=False ─────────────────────


class TestTextFormatPreservation:
    def test_text_mode_not_json(self, mas_dir):
        """Without --json-logs, daemon.log must contain human-readable text."""
        log_dir = mas_dir / "logs"
        setup_daemon_logging(log_dir, 1024 * 1024, 1, json_logs=False)
        logging.getLogger("mas.daemon").info("daemon started")
        line = (log_dir / "daemon.log").read_text().strip()
        assert not line.startswith("{"), f"unexpected JSON in text mode: {line!r}"
        assert "INFO" in line
        assert "mas.daemon" in line
        assert "daemon started" in line

    def test_text_format_pattern(self, mas_dir):
        """Text-mode lines must match: YYYY-MM-DD HH:MM:SS,mmm LEVEL name: msg."""
        log_dir = mas_dir / "logs"
        setup_daemon_logging(log_dir, 1024 * 1024, 1, json_logs=False)
        logging.getLogger("mas.daemon").info("tick #1 start")
        line = (log_dir / "daemon.log").read_text().strip()
        pattern = (
            r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} "
            r"INFO mas\.daemon: tick #1 start$"
        )
        assert re.match(pattern, line), f"text format mismatch: {line!r}"


# ─── Group 6: per-task worker logs unaffected ─────────────────────────────────


class TestWorkerLogsUnaffected:
    def test_worker_log_stays_plain_text(self, mas_dir, tmp_path):
        """Per-task worker logs must remain plain text even in daemon JSON mode."""
        _setup_json_log(mas_dir)

        task_log_file = tmp_path / "worker.log"
        worker_logger = logging.getLogger("mas.worker.task-xyz-9999")
        worker_logger.propagate = False
        worker_logger.setLevel(logging.INFO)
        handler = logging.FileHandler(str(task_log_file))
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        worker_logger.addHandler(handler)

        try:
            worker_logger.info("implementing feature")
        finally:
            worker_logger.removeHandler(handler)
            handler.close()

        content = task_log_file.read_text().strip()
        assert content == "INFO: implementing feature"
        with pytest.raises(json.JSONDecodeError):
            json.loads(content)
