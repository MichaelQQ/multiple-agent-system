"""Tests for daemon log rotation (DaemonConfig + setup_daemon_logging)."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

import pytest

from mas.config import validate_config
from mas.logging import setup_daemon_logging
from mas.schemas import DaemonConfig, MasConfig, ProviderConfig, RoleConfig


def _fresh_mas_logger():
    root = logging.getLogger("mas")
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    root.setLevel(logging.INFO)
    return root


def test_daemon_config_defaults():
    cfg = DaemonConfig()
    assert cfg.log_max_bytes == 10_485_760
    assert cfg.log_backup_count == 5


def test_mas_config_has_daemon_default():
    cfg = MasConfig(
        providers={"mock": ProviderConfig(cli="true")},
        roles={"proposer": RoleConfig(provider="mock")},
    )
    assert cfg.daemon.log_max_bytes == 10_485_760
    assert cfg.daemon.log_backup_count == 5


def test_setup_daemon_logging_attaches_rotating_handler(tmp_path):
    _fresh_mas_logger()
    setup_daemon_logging(tmp_path, max_bytes=100, backup_count=3)
    root = logging.getLogger("mas")
    rot = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(rot) == 1
    assert Path(rot[0].baseFilename).resolve() == (tmp_path / "daemon.log").resolve()
    assert rot[0].maxBytes == 100
    assert rot[0].backupCount == 3


def test_setup_daemon_logging_is_idempotent(tmp_path):
    _fresh_mas_logger()
    setup_daemon_logging(tmp_path, max_bytes=100, backup_count=3)
    setup_daemon_logging(tmp_path, max_bytes=200, backup_count=4)
    root = logging.getLogger("mas")
    rot = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(rot) == 1
    assert rot[0].maxBytes == 200
    assert rot[0].backupCount == 4


def test_rotation_triggered_when_exceeding_max_bytes(tmp_path):
    _fresh_mas_logger()
    setup_daemon_logging(tmp_path, max_bytes=200, backup_count=2)
    logger = logging.getLogger("mas.rotation_test")
    for i in range(40):
        logger.info("X" * 50)

    log_file = tmp_path / "daemon.log"
    assert log_file.exists()
    assert (tmp_path / "daemon.log.1").exists()


def test_rotation_caps_backup_count(tmp_path):
    _fresh_mas_logger()
    setup_daemon_logging(tmp_path, max_bytes=100, backup_count=2)
    logger = logging.getLogger("mas.rotation_cap_test")
    for i in range(100):
        logger.info("Y" * 50)

    backups = sorted(tmp_path.glob("daemon.log.*"))
    assert len(backups) <= 2


def test_exception_traceback_routed_through_handler(tmp_path):
    _fresh_mas_logger()
    setup_daemon_logging(tmp_path, max_bytes=100_000, backup_count=1)
    logger = logging.getLogger("mas.daemon")
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("wrapped")

    content = (tmp_path / "daemon.log").read_text()
    assert "wrapped" in content
    assert "ValueError: boom" in content


def test_say_routed_through_logger(tmp_path, caplog):
    _fresh_mas_logger()
    setup_daemon_logging(tmp_path, max_bytes=1000, backup_count=1)
    from mas.daemon import _say

    with caplog.at_level(logging.INFO, logger="mas.daemon"):
        _say("hello daemon")
    assert "hello daemon" in caplog.text


def test_validate_rejects_non_positive_log_max_bytes(tmp_path):
    cfg = MasConfig(
        providers={"mock": ProviderConfig(cli="true")},
        roles={"proposer": RoleConfig(provider="mock")},
        daemon=DaemonConfig(log_max_bytes=0),
    )
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "proposer.md").write_text("p")
    issues = validate_config(cfg, tmp_path)
    assert any("log_max_bytes" in i.field for i in issues)


def test_validate_rejects_negative_log_backup_count(tmp_path):
    cfg = MasConfig(
        providers={"mock": ProviderConfig(cli="true")},
        roles={"proposer": RoleConfig(provider="mock")},
        daemon=DaemonConfig(log_backup_count=-1),
    )
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "proposer.md").write_text("p")
    issues = validate_config(cfg, tmp_path)
    assert any("log_backup_count" in i.field for i in issues)


def test_validate_accepts_defaults(tmp_path):
    cfg = MasConfig(
        providers={"mock": ProviderConfig(cli="true")},
        roles={"proposer": RoleConfig(provider="mock")},
    )
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "proposer.md").write_text("p")
    issues = validate_config(cfg, tmp_path)
    assert not any(i.field.startswith("daemon.") for i in issues)
