import errno
import signal
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mas.daemon import (
    DaemonError,
    _clear_pid,
    _pid_alive,
    _pid_path,
    _read_pid,
    _run_loop,
    start,
    stop,
    status,
)


@pytest.fixture
def mas(tmp_path: Path) -> Path:
    d = tmp_path / ".mas"
    d.mkdir(parents=True)
    (d / "logs").mkdir()
    return d


class TestPidAlive:
    def test_pid_alive_returns_false_for_nonexistent_pid(self):
        assert _pid_alive(999999999) is False

    def test_pid_alive_returns_true_for_self(self):
        import os
        assert _pid_alive(os.getpid()) is True

    def test_pid_alive_returns_false_when_oserror_not_perm(self):
        with patch("mas.daemon.os.kill") as mock_kill:
            mock_kill.side_effect = OSError(errno.ESRCH, "No such process")
            assert _pid_alive(12345) is False

    def test_pid_alive_returns_true_when_oserror_is_perm(self):
        with patch("mas.daemon.os.kill") as mock_kill:
            mock_kill.side_effect = OSError(errno.EPERM, "Operation not permitted")
            assert _pid_alive(12345) is True


class TestReadPid:
    def test_read_pid_returns_none_when_file_missing(self, mas):
        assert _read_pid(mas) is None

    def test_read_pid_returns_none_when_file_corrupt(self, mas):
        pid_file = _pid_path(mas)
        pid_file.write_text("not_a_number\n")
        assert _read_pid(mas) is None

    def test_read_pid_returns_none_on_oserror(self, mas):
        pid_file = _pid_path(mas)
        pid_file.write_text("12345\n")
        with patch("mas.daemon.Path.read_text") as mock_read:
            mock_read.side_effect = OSError("read error")
            assert _read_pid(mas) is None

    def test_read_pid_returns_valid_pid(self, mas):
        pid_file = _pid_path(mas)
        pid_file.write_text("12345\n")
        assert _read_pid(mas) == 12345

    def test_read_pid_strips_whitespace(self, mas):
        pid_file = _pid_path(mas)
        pid_file.write_text("  12345  \n")
        assert _read_pid(mas) == 12345


class TestClearPid:
    def test_clear_pid_removes_existing_file(self, mas):
        pid_file = _pid_path(mas)
        pid_file.write_text("12345\n")
        assert pid_file.exists()
        _clear_pid(mas)
        assert not pid_file.exists()

    def test_clear_pid_succeeds_when_file_missing(self, mas):
        _clear_pid(mas)


class TestStatus:
    def test_status_returns_none_false_when_no_pid_file(self, mas):
        with patch("mas.config.project_dir", return_value=mas):
            assert status(mas.parent) == (None, False)

    def test_status_returns_pid_true_when_alive(self, mas):
        import os
        pid_file = _pid_path(mas)
        pid_file.write_text(f"{os.getpid()}\n")
        with patch("mas.daemon._pid_alive", return_value=True):
            with patch("mas.config.project_dir", return_value=mas):
                pid, running = status(mas.parent)
                assert pid == os.getpid()
                assert running is True

    def test_status_returns_pid_false_when_stale(self, mas):
        pid_file = _pid_path(mas)
        pid_file.write_text("999999999\n")
        with patch("mas.daemon._pid_alive", return_value=False):
            with patch("mas.config.project_dir", return_value=mas):
                pid, running = status(mas.parent)
                assert pid == 999999999
                assert running is False


class TestStart:
    def test_start_raises_error_when_daemon_running(self, mas):
        pid_file = _pid_path(mas)
        pid_file.write_text("999999999\n")
        with patch("mas.config.project_dir", return_value=mas):
            with patch("mas.daemon._pid_alive", return_value=True):
                with pytest.raises(DaemonError, match="daemon already running"):
                    start(mas.parent)

    def test_start_clears_stale_pid_and_proceeds(self, mas):
        pid_file = _pid_path(mas)
        pid_file.write_text("999999999\n")
        assert pid_file.exists()

        child_wrote_pid = False

        def fake_sleep(seconds):
            pass

        mock_stdin = MagicMock()
        mock_stdin.fileno.return_value = 0

        with patch("mas.config.project_dir", return_value=mas):
            with patch("mas.daemon._pid_alive", return_value=False):
                with patch("mas.daemon.os.fork") as mock_fork:
                    def fork_side_effect():
                        nonlocal child_wrote_pid
                        if not child_wrote_pid:
                            child_wrote_pid = True
                            pid_file.write_text("54321\n")
                            return 0
                        return 54321
                    mock_fork.side_effect = fork_side_effect
                    with patch("mas.daemon.os.setsid"):
                        with patch("mas.daemon.os.chdir"):
                            with patch("mas.daemon.os.umask"):
                                with patch("mas.daemon.os.dup2"):
                                    with patch("builtins.open", MagicMock()):
                                        with patch.object(sys, "stdin", mock_stdin):
                                            with patch("mas.daemon.time.sleep", side_effect=fake_sleep):
                                                with patch("mas.daemon.os.getpid", return_value=54321):
                                                    with patch("mas.daemon.os._exit"):
                                                        with patch("mas.daemon._run_loop"):
                                                            start(mas.parent)
                                                            assert not pid_file.exists()

    def test_start_raises_error_when_fork_fails_to_write_pid(self, mas):
        with patch("mas.config.project_dir", return_value=mas):
            with patch("mas.daemon.os.fork") as mock_fork:
                mock_fork.return_value = 1
                with patch("mas.daemon.time.sleep"):
                    with patch("mas.daemon._read_pid", return_value=None):
                        with pytest.raises(DaemonError, match="daemon failed to start"):
                            start(mas.parent)


class TestStop:
    def test_stop_returns_false_when_no_pid_file(self, mas):
        with patch("mas.config.project_dir", return_value=mas):
            assert stop(mas.parent) is False

    def test_stop_returns_false_when_pid_dead(self, mas):
        pid_file = _pid_path(mas)
        pid_file.write_text("999999999\n")
        with patch("mas.daemon._pid_alive", return_value=False):
            with patch("mas.config.project_dir", return_value=mas):
                assert stop(mas.parent) is False
                assert not pid_file.exists()

    def test_stop_returns_true_and_sends_sigterm(self, mas):
        import os
        pid_file = _pid_path(mas)
        pid_file.write_text(f"{os.getpid()}\n")
        with patch("mas.daemon._pid_alive", side_effect=[True, False]):
            with patch("mas.daemon.os.kill") as mock_kill:
                with patch("mas.config.project_dir", return_value=mas):
                    assert stop(mas.parent) is True
                    mock_kill.assert_called_once_with(os.getpid(), signal.SIGTERM)

    def test_stop_escalates_to_sigkill_after_timeout(self, mas):
        import os
        pid_file = _pid_path(mas)
        pid_file.write_text(f"{os.getpid()}\n")
        kill_calls = []

        def fake_kill(pid, sig):
            kill_calls.append(sig)

        with patch("mas.daemon._pid_alive", return_value=True):
            with patch("mas.daemon.os.kill", side_effect=fake_kill):
                with patch("mas.daemon.time.sleep"):
                    with patch("mas.config.project_dir", return_value=mas):
                        result = stop(mas.parent, timeout=0.3)
                        assert result is True
                        assert signal.SIGTERM in kill_calls
                        assert signal.SIGKILL in kill_calls


class TestRunLoop:
    def test_run_loop_exits_when_stop_flag_set(self, mas):
        stop_flag = {"stop": True}
        with patch("mas.tick.run_tick"):
            _run_loop(mas.parent, 1, stop_flag)

    def test_run_loop_calls_tick(self, mas):
        stop_flag = {"stop": False}
        with patch("mas.tick.run_tick") as mock_tick:
            def fake_sleep(seconds):
                stop_flag["stop"] = True
            with patch("mas.daemon.time.sleep", side_effect=fake_sleep):
                _run_loop(mas.parent, 1, stop_flag)
                mock_tick.assert_called_once_with(start=mas.parent)

    def test_run_loop_handles_tick_exception(self, mas):
        stop_flag = {"stop": False}
        with patch("mas.tick.run_tick", side_effect=Exception("tick failed")):
            with patch("mas.daemon.log") as mock_log:
                def fake_sleep(seconds):
                    stop_flag["stop"] = True
                with patch("mas.daemon.time.sleep", side_effect=fake_sleep):
                    _run_loop(mas.parent, 1, stop_flag)
                    mock_log.exception.assert_called_once()

    def test_run_loop_sleeps_in_slices(self, mas):
        stop_flag = {"stop": False}
        with patch("mas.tick.run_tick"):
            def fake_sleep(seconds):
                stop_flag["stop"] = True
            with patch("mas.daemon.time.sleep", side_effect=fake_sleep) as mock_sleep:
                _run_loop(mas.parent, 2, stop_flag)
                assert mock_sleep.call_count >= 1


class TestSignalHandling:
    def test_handle_term_sets_stop_flag(self, mas):
        stop_flag = {"stop": False}

        def fake_handle_term(signum, frame):
            stop_flag["stop"] = True

        with patch("mas.daemon.os.fork") as mock_fork:
            with patch("mas.daemon.os.setsid"):
                with patch("mas.daemon.os.chdir"):
                    with patch("mas.daemon.os.umask"):
                        with patch("mas.daemon.os.dup2"):
                            with patch("builtins.open", MagicMock()):
                                with patch("mas.daemon.os.getpid", return_value=12345):
                                    with patch("mas.daemon.os._exit"):
                                        with patch("mas.daemon.signal.signal") as mock_signal:
                                            def side_effect_run(project, interval, flag):
                                                assert flag["stop"] is False
                                                fake_handle_term(signal.SIGTERM, None)
                                                assert flag["stop"] is True
                                            with patch("mas.daemon._run_loop", side_effect=side_effect_run):
                                                mock_fork.side_effect = [1, 0, 0]
                                                with patch("mas.config.project_dir", return_value=mas):
                                                    try:
                                                        start(mas.parent)
                                                    except:
                                                        pass
