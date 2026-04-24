"""Tests for per-role wall-clock timeout enforcement in the tick loop."""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path
from unittest.mock import patch

from mas import board, transitions
from mas.schemas import (
    MasConfig,
    ProviderConfig,
    Result,
    RoleConfig,
    Task,
)
from mas.tick import TickEnv, _reap_workers


def _cfg(timeout_s: int = 1800) -> MasConfig:
    return MasConfig(
        providers={"mock": ProviderConfig(cli="sh", max_concurrent=2, extra_args=[])},
        roles={
            "proposer": RoleConfig(provider="mock", timeout_s=timeout_s),
            "orchestrator": RoleConfig(provider="mock", timeout_s=timeout_s),
            "implementer": RoleConfig(provider="mock", timeout_s=timeout_s),
            "tester": RoleConfig(provider="mock", timeout_s=timeout_s),
            "evaluator": RoleConfig(provider="mock", timeout_s=timeout_s),
        },
    )


def _seed_task(mas: Path, task_id: str) -> Path:
    tdir = board.task_dir(mas, "doing", task_id)
    tdir.mkdir(parents=True)
    board.write_task(tdir, Task(id=task_id, role="implementer", goal="g"))
    (tdir / "logs").mkdir()
    (tdir / "logs" / "implementer-1.log").write_text("some output\nlast line of log\n")
    return tdir


def test_write_pid_includes_timestamp(tmp_path):
    pid_dir = tmp_path / "pids"
    with patch("time.time", return_value=1234567.5):
        board.write_pid(pid_dir, "implementer", "mock", 54321)

    contents = (pid_dir / "implementer.mock.pid").read_text()
    lines = contents.splitlines()
    assert lines[0] == "54321"
    assert lines[1] == "1234567.5"


def test_read_pid_entry_handles_new_format(tmp_path):
    p = tmp_path / "x.pid"
    p.write_text("123\n456.7\n")
    assert board.read_pid_entry(p) == (123, 456.7)


def test_read_pid_entry_handles_legacy_format(tmp_path):
    p = tmp_path / "x.pid"
    p.write_text("999\n")
    assert board.read_pid_entry(p) == (999, None)


def test_reap_skips_young_pid(tmp_path):
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(timeout_s=1800))

    tdir = _seed_task(mas, "20260424-young-aaaa")
    pid_dir = tdir / "pids"
    pid_dir.mkdir()
    live_pid = os.getpid()
    with patch("time.time", return_value=10000.0):
        board.write_pid(pid_dir, "implementer", "mock", live_pid)

    with patch("time.time", return_value=10100.0), \
         patch("os.kill") as mock_kill:
        _reap_workers(env)

    for call in mock_kill.call_args_list:
        assert call.args[1] not in (signal.SIGTERM, signal.SIGKILL)
    assert (pid_dir / "implementer.mock.pid").exists()
    assert not (tdir / "result.json").exists()


def test_reap_kills_overdue_pid_and_synthesizes_failure(tmp_path):
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(timeout_s=1800))

    task_id = "20260424-overdue-aaaa"
    tdir = _seed_task(mas, task_id)
    pid_dir = tdir / "pids"
    pid_dir.mkdir()
    pidfile = pid_dir / "implementer.mock.pid"
    pidfile.write_text("12345\n100.0\n")

    alive_calls = iter([True, False])
    with patch("mas.tick._pid_alive", side_effect=lambda pid: next(alive_calls, False)), \
         patch("mas.board._pid_alive", return_value=True), \
         patch("time.time", return_value=5000.0), \
         patch("time.sleep"), \
         patch("os.kill") as mock_kill:
        _reap_workers(env)

    signals_sent = [c.args[1] for c in mock_kill.call_args_list if c.args[0] == 12345]
    assert signal.SIGTERM in signals_sent

    result = board.read_result(tdir)
    assert result is not None
    assert result.status == "failure"
    assert "timeout exceeded after 1800s" in result.summary
    assert "last line of log" in (result.feedback or "")

    txns = transitions.read_transitions(tdir)
    assert any(t.to_state == "timeout" and "role.timeout_s" in t.reason for t in txns)

    assert not pidfile.exists()


def test_reap_escalates_to_sigkill_if_sigterm_ignored(tmp_path):
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(timeout_s=10))

    task_id = "20260424-stuck-aaaa"
    tdir = _seed_task(mas, task_id)
    pid_dir = tdir / "pids"
    pid_dir.mkdir()
    (pid_dir / "implementer.mock.pid").write_text("12345\n0.0\n")

    with patch("mas.tick._pid_alive", return_value=True), \
         patch("mas.board._pid_alive", return_value=True), \
         patch("time.time", side_effect=[5000.0, 5000.0, 5001.0, 5010.0]), \
         patch("time.sleep"), \
         patch("os.kill") as mock_kill:
        _reap_workers(env)

    signals_sent = [c.args[1] for c in mock_kill.call_args_list if c.args[0] == 12345]
    assert signal.SIGTERM in signals_sent
    assert signal.SIGKILL in signals_sent


def test_reap_skips_legacy_pidfile(tmp_path):
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(timeout_s=1))

    tdir = _seed_task(mas, "20260424-legacy-aaaa")
    pid_dir = tdir / "pids"
    pid_dir.mkdir()
    (pid_dir / "implementer.mock.pid").write_text("12345\n")

    with patch("mas.tick._pid_alive", return_value=True), \
         patch("mas.board._pid_alive", return_value=True), \
         patch("os.kill") as mock_kill:
        _reap_workers(env)

    assert not mock_kill.called
    assert not (tdir / "result.json").exists()


def test_dispatch_role_writes_pid_with_timestamp(tmp_path):
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    (mas / "prompts" / "implementer.md").write_text("prompt template")

    tdir = board.task_dir(mas, "doing", "20260424-disp-aaaa")
    tdir.mkdir(parents=True)
    task = Task(id="20260424-disp-aaaa", role="implementer", goal="g")

    env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg())

    from unittest.mock import MagicMock
    from mas.tick import _dispatch_role

    mock_adapter = MagicMock()
    mock_adapter.dispatch.return_value = MagicMock(pid=54321)
    mock_adapter.agentic = False

    with patch("mas.tick.get_adapter", return_value=MagicMock(return_value=mock_adapter)), \
         patch("mas.board.count_active_pids", return_value=0), \
         patch("time.time", return_value=7777777.0):
        _dispatch_role(env, task, tdir, tdir, role="implementer")

    pid_file = tdir / "pids" / "implementer.mock.pid"
    assert pid_file.exists()
    entry = board.read_pid_entry(pid_file)
    assert entry == (54321, 7777777.0)
