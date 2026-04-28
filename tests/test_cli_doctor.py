"""Failing tests that pin the `mas doctor` CLI command contract.

All tests must currently fail (command does not exist yet). Once the
`doctor` command is implemented they should all pass.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from mas import board
from mas.cli import app
from mas.schemas import Task

runner = CliRunner()


@pytest.fixture(autouse=True)
def _wide_console():
    """Prevent Rich from truncating table output in the test runner."""
    from mas import cli

    original = cli.console
    cli.console = Console(width=200)
    yield
    cli.console = original


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_task(
    mas_dir: Path,
    column: str,
    task_id: str,
    role: str = "orchestrator",
    goal: str = "test task",
) -> Path:
    task = Task(id=task_id, role=role, goal=goal)
    d = mas_dir / "tasks" / column / task_id
    board.write_task(d, task)
    return d


def _setup_clean_mas(mas_dir: Path) -> None:
    """Write a minimal valid config, roles, and prompt templates."""
    (mas_dir / "config.yaml").write_text(
        "providers:\n"
        "  claude-code:\n"
        "    cli: claude\n"
        "    max_concurrent: 1\n"
    )
    (mas_dir / "roles.yaml").write_text(
        "roles:\n"
        "  proposer:\n"
        "    provider: claude-code\n"
        "  orchestrator:\n"
        "    provider: claude-code\n"
        "  implementer:\n"
        "    provider: claude-code\n"
        "  tester:\n"
        "    provider: claude-code\n"
        "  evaluator:\n"
        "    provider: claude-code\n"
    )
    prompts_dir = mas_dir / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    for role in ("proposer", "orchestrator", "implementer", "tester", "evaluator"):
        (prompts_dir / f"{role}.md").write_text(f"# {role} prompt\n")


def _dead_pid() -> int:
    """Return a PID that is guaranteed not to be alive on this system."""
    candidate = 2**22  # 4194304 — beyond typical max PID on macOS/Linux
    try:
        os.kill(candidate, 0)
        # If we get here, the PID is somehow alive — fall back to a different value
        return 2**22 - 1
    except OSError:
        return candidate


# ---------------------------------------------------------------------------
# (a) Clean setup → exit 0 with OK rows for all four groups
# ---------------------------------------------------------------------------


class TestDoctorCleanSetup:
    def test_exit_0_with_ok_rows_for_all_groups(self, tmp_board, monkeypatch):
        """Clean .mas/ + all binaries present → exit 0 with OK rows for all groups."""
        monkeypatch.chdir(tmp_board.parent)
        _setup_clean_mas(tmp_board)
        monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code != 2, (
            f"exit_code=2 means 'doctor' is not registered.\nOutput: {result.output}"
        )
        assert result.exit_code == 0, (
            f"Expected exit 0 for clean setup, got {result.exit_code}.\n{result.output}"
        )
        output = result.output
        assert "OK" in output, f"Expected at least one 'OK' row; got:\n{output}"
        for group in ("Config", "Provider", "Board", "Daemon"):
            assert group in output, (
                f"Expected group '{group}' in output; got:\n{output}"
            )

    def test_no_fail_rows_in_clean_setup(self, tmp_board, monkeypatch):
        """Clean setup must not produce any FAIL rows and must exit 0."""
        monkeypatch.chdir(tmp_board.parent)
        _setup_clean_mas(tmp_board)
        monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code != 2, f"Command not registered.\nOutput: {result.output}"
        assert result.exit_code == 0, (
            f"Expected exit 0 for clean setup, got {result.exit_code}.\n{result.output}"
        )
        assert "FAIL" not in result.output, (
            f"Expected no FAIL rows in clean setup; got:\n{result.output}"
        )


# ---------------------------------------------------------------------------
# (b) Invalid config.yaml → FAIL row in Config group + exit 1
# ---------------------------------------------------------------------------


class TestDoctorConfigGroup:
    def test_unparseable_yaml_shows_fail_and_exits_1(self, tmp_board, monkeypatch):
        """Unparseable config.yaml → FAIL in Config group + exit 1."""
        monkeypatch.chdir(tmp_board.parent)
        (tmp_board / "config.yaml").write_text(":\n  - invalid: [yaml\n")

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code != 2, (
            f"exit_code=2 means 'doctor' is not registered.\nOutput: {result.output}"
        )
        assert result.exit_code == 1, (
            f"Expected exit 1 for unparseable config, got {result.exit_code}.\n{result.output}"
        )
        assert "FAIL" in result.output, (
            f"Expected 'FAIL' in output for bad config; got:\n{result.output}"
        )
        assert "Config" in result.output, (
            f"Expected 'Config' group in output; got:\n{result.output}"
        )

    def test_unknown_field_in_config_shows_fail_and_exits_1(self, tmp_board, monkeypatch):
        """Config with unknown top-level field → FAIL in Config group + exit 1."""
        monkeypatch.chdir(tmp_board.parent)
        (tmp_board / "config.yaml").write_text(
            "unknown_field_that_does_not_exist: true\nproviders: {}\nroles: {}\n"
        )

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code != 2, (
            f"exit_code=2 means 'doctor' is not registered.\nOutput: {result.output}"
        )
        assert result.exit_code == 1, (
            f"Expected exit 1 for config with unknown field, got {result.exit_code}.\n{result.output}"
        )
        assert "FAIL" in result.output, (
            f"Expected 'FAIL' in output; got:\n{result.output}"
        )
        assert "Config" in result.output, (
            f"Expected 'Config' group in output; got:\n{result.output}"
        )


# ---------------------------------------------------------------------------
# (c) Provider binary missing → FAIL row in Provider group + exit 1
# ---------------------------------------------------------------------------


class TestDoctorProviderGroup:
    def test_missing_binary_shows_fail_in_provider_group_and_exits_1(
        self, tmp_board, monkeypatch
    ):
        """shutil.which returns None for provider CLI → FAIL in Provider group + exit 1."""
        monkeypatch.chdir(tmp_board.parent)
        _setup_clean_mas(tmp_board)
        # All which() calls return None — no provider binary present
        monkeypatch.setattr("shutil.which", lambda cmd: None)

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code != 2, (
            f"exit_code=2 means 'doctor' is not registered.\nOutput: {result.output}"
        )
        assert result.exit_code == 1, (
            f"Expected exit 1 when provider binary missing, got {result.exit_code}.\n{result.output}"
        )
        assert "FAIL" in result.output, (
            f"Expected 'FAIL' in output; got:\n{result.output}"
        )
        assert "Provider" in result.output, (
            f"Expected 'Provider' group in output; got:\n{result.output}"
        )


# ---------------------------------------------------------------------------
# (d) Orphan worktree → FAIL row in Board/Worktree group
# ---------------------------------------------------------------------------


class TestDoctorBoardWorktreeGroup:
    def test_orphan_worktree_shows_fail_or_warn(self, monkeypatch, git_repo):
        """mas/<fake_id> branch + worktree with no task dir → FAIL/WARN in Board/Worktree group."""
        mas_dir = git_repo / ".mas"
        board.ensure_layout(mas_dir)
        _setup_clean_mas(mas_dir)
        monkeypatch.chdir(git_repo)
        monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")

        fake_id = "20260427-orphan-fake-9999"
        branch = f"mas/{fake_id}"
        wt_path = git_repo / "wt_orphan"

        subprocess.run(
            ["git", "-C", str(git_repo), "checkout", "-b", branch],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(git_repo), "checkout", "-"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(git_repo), "worktree", "add", str(wt_path), branch],
            check=True,
            capture_output=True,
        )
        # No task dir created for fake_id in any column

        try:
            result = runner.invoke(app, ["doctor"])
            assert result.exit_code != 2, (
                f"exit_code=2 means 'doctor' is not registered.\nOutput: {result.output}"
            )
            output = result.output
            assert "FAIL" in output or "WARN" in output, (
                f"Expected FAIL or WARN for orphan worktree; got:\n{output}"
            )
            assert any(g in output for g in ("Board", "Worktree")), (
                f"Expected Board or Worktree group in output; got:\n{output}"
            )
        finally:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(git_repo),
                    "worktree",
                    "remove",
                    "--force",
                    str(wt_path),
                ],
                capture_output=True,
            )


# ---------------------------------------------------------------------------
# (e) Stale worker PID file → WARN row
# ---------------------------------------------------------------------------


class TestDoctorStalePidFile:
    def test_stale_worker_pid_shows_warn(self, tmp_board, monkeypatch):
        """Stale .mas/pids/<role>.<provider>.pid with dead PID → WARN row."""
        monkeypatch.chdir(tmp_board.parent)
        _setup_clean_mas(tmp_board)
        monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")

        dead = _dead_pid()
        try:
            os.kill(dead, 0)
            pytest.skip(f"PID {dead} is alive on this system; skipping")
        except OSError:
            pass

        pids_dir = tmp_board / "pids"
        pids_dir.mkdir(parents=True, exist_ok=True)
        (pids_dir / "implementer.claude-code.pid").write_text(f"{dead}\n")

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code != 2, (
            f"exit_code=2 means 'doctor' is not registered.\nOutput: {result.output}"
        )
        assert "WARN" in result.output, (
            f"Expected 'WARN' for stale worker PID; got:\n{result.output}"
        )

    def test_stale_worker_pid_does_not_cause_fail(self, tmp_board, monkeypatch):
        """Stale worker PID alone should produce WARN, not FAIL — exit code must not be 1 unless --strict."""
        monkeypatch.chdir(tmp_board.parent)
        _setup_clean_mas(tmp_board)
        monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")

        dead = _dead_pid()
        try:
            os.kill(dead, 0)
            pytest.skip(f"PID {dead} is alive; skipping")
        except OSError:
            pass

        pids_dir = tmp_board / "pids"
        pids_dir.mkdir(parents=True, exist_ok=True)
        (pids_dir / "implementer.claude-code.pid").write_text(f"{dead}\n")

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code != 2, f"Command not registered.\nOutput: {result.output}"
        # WARN-only run without --strict → exit 0
        assert result.exit_code == 0, (
            f"Expected exit 0 for WARN-only run (no --strict), got {result.exit_code}.\n{result.output}"
        )


# ---------------------------------------------------------------------------
# (f) --json flag: JSON output with correct shape, no ANSI sequences
# ---------------------------------------------------------------------------


class TestDoctorJsonFlag:
    def test_json_flag_emits_valid_json(self, tmp_board, monkeypatch):
        """--json emits a JSON document, not Rich table output."""
        monkeypatch.chdir(tmp_board.parent)
        _setup_clean_mas(tmp_board)
        monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")

        result = runner.invoke(app, ["doctor", "--json"])
        assert result.exit_code != 2, (
            f"exit_code=2 means 'doctor' is not registered.\nOutput: {result.output}"
        )
        assert result.output.strip(), "Expected non-empty output from --json flag"
        parsed = None
        try:
            parsed = json.loads(result.output)
        except json.JSONDecodeError:
            pass
        assert parsed is not None, (
            f"--json output is not valid JSON.\nRaw output:\n{result.output}"
        )
        assert isinstance(parsed, dict), f"Expected JSON object at top level; got {type(parsed)}"

    def test_json_output_has_checks_list(self, tmp_board, monkeypatch):
        """JSON output must have a 'checks' key that is a list of check objects."""
        monkeypatch.chdir(tmp_board.parent)
        _setup_clean_mas(tmp_board)
        monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")

        result = runner.invoke(app, ["doctor", "--json"])
        assert result.exit_code != 2, f"Command not registered.\nOutput: {result.output}"
        assert result.output.strip(), "Expected non-empty JSON output from --json flag"
        parsed = None
        try:
            parsed = json.loads(result.output)
        except json.JSONDecodeError:
            pass
        assert parsed is not None, f"--json output is not valid JSON.\nRaw: {result.output}"
        assert "checks" in parsed, (
            f"Expected 'checks' key in JSON; got keys: {list(parsed.keys())}"
        )
        checks = parsed["checks"]
        assert isinstance(checks, list), f"Expected list for 'checks'; got {type(checks)}"
        assert len(checks) > 0, "Expected at least one check item in 'checks'"
        for item in checks:
            for key in ("group", "name", "status", "detail"):
                assert key in item, (
                    f"Missing key '{key}' in check item: {item}"
                )

    def test_json_output_has_summary(self, tmp_board, monkeypatch):
        """JSON output must have a 'summary' key with ok/warn/fail counts."""
        monkeypatch.chdir(tmp_board.parent)
        _setup_clean_mas(tmp_board)
        monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")

        result = runner.invoke(app, ["doctor", "--json"])
        assert result.exit_code != 2, f"Command not registered.\nOutput: {result.output}"
        assert result.output.strip(), "Expected non-empty JSON output from --json flag"
        parsed = None
        try:
            parsed = json.loads(result.output)
        except json.JSONDecodeError:
            pass
        assert parsed is not None, f"--json output is not valid JSON.\nRaw: {result.output}"
        assert "summary" in parsed, (
            f"Expected 'summary' key in JSON; got keys: {list(parsed.keys())}"
        )
        summary = parsed["summary"]
        for key in ("ok", "warn", "fail"):
            assert key in summary, (
                f"Missing key '{key}' in summary: {summary}"
            )
        assert isinstance(summary["ok"], int), "summary.ok must be an integer"
        assert isinstance(summary["warn"], int), "summary.warn must be an integer"
        assert isinstance(summary["fail"], int), "summary.fail must be an integer"

    def test_json_flag_produces_no_ansi_sequences(self, tmp_board, monkeypatch):
        """--json stdout must not contain ANSI escape sequences, and must be non-empty."""
        monkeypatch.chdir(tmp_board.parent)
        _setup_clean_mas(tmp_board)
        monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")

        result = runner.invoke(app, ["doctor", "--json"])
        assert result.exit_code != 2, f"Command not registered.\nOutput: {result.output}"
        assert result.output.strip(), (
            f"Expected non-empty JSON output from --json flag; got empty string"
        )
        assert "\x1b[" not in result.output, (
            f"ANSI escape sequences found in --json output:\n{repr(result.output)}"
        )


# ---------------------------------------------------------------------------
# (g) --strict: exit 1 even when only WARNs present
# ---------------------------------------------------------------------------


class TestDoctorStrictFlag:
    def test_strict_exits_1_on_warn_only_run(self, tmp_board, monkeypatch):
        """--strict on a WARN-only run → exit 1."""
        monkeypatch.chdir(tmp_board.parent)
        _setup_clean_mas(tmp_board)
        monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")

        dead = _dead_pid()
        try:
            os.kill(dead, 0)
            pytest.skip(f"PID {dead} is alive; skipping")
        except OSError:
            pass

        pids_dir = tmp_board / "pids"
        pids_dir.mkdir(parents=True, exist_ok=True)
        (pids_dir / "implementer.claude-code.pid").write_text(f"{dead}\n")

        result = runner.invoke(app, ["doctor", "--strict"])
        assert result.exit_code != 2, (
            f"exit_code=2 means 'doctor' is not registered.\nOutput: {result.output}"
        )
        assert result.exit_code == 1, (
            f"Expected exit 1 with --strict on WARN-only run, got {result.exit_code}.\n{result.output}"
        )
        assert "WARN" in result.output, (
            f"Expected WARN row in --strict output; got:\n{result.output}"
        )

    def test_strict_on_clean_run_exits_0(self, tmp_board, monkeypatch):
        """--strict on a fully clean run → exit 0."""
        monkeypatch.chdir(tmp_board.parent)
        _setup_clean_mas(tmp_board)
        monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")

        result = runner.invoke(app, ["doctor", "--strict"])
        assert result.exit_code != 2, (
            f"exit_code=2 means 'doctor' is not registered.\nOutput: {result.output}"
        )
        assert result.exit_code == 0, (
            f"Expected exit 0 with --strict on clean run, got {result.exit_code}.\n{result.output}"
        )


# ---------------------------------------------------------------------------
# (h) Stale daemon pidfile → FAIL in Daemon group + exit 1
#     Missing daemon pidfile → OK
# ---------------------------------------------------------------------------


class TestDoctorDaemonPidfile:
    def test_stale_daemon_pid_shows_fail_and_exits_1(self, tmp_board, monkeypatch):
        """daemon.pid containing dead PID → FAIL in Daemon group + exit 1."""
        monkeypatch.chdir(tmp_board.parent)
        _setup_clean_mas(tmp_board)
        monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")

        dead = _dead_pid()
        try:
            os.kill(dead, 0)
            pytest.skip(f"PID {dead} is alive; skipping")
        except OSError:
            pass

        (tmp_board / "daemon.pid").write_text(f"{dead}\n")

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code != 2, (
            f"exit_code=2 means 'doctor' is not registered.\nOutput: {result.output}"
        )
        assert result.exit_code == 1, (
            f"Expected exit 1 for stale daemon pid, got {result.exit_code}.\n{result.output}"
        )
        assert "FAIL" in result.output, (
            f"Expected 'FAIL' in output; got:\n{result.output}"
        )
        assert "Daemon" in result.output, (
            f"Expected 'Daemon' group in output; got:\n{result.output}"
        )

    def test_missing_daemon_pidfile_shows_ok_and_exits_0(self, tmp_board, monkeypatch):
        """No daemon.pid file → Daemon group shows OK, exit 0."""
        monkeypatch.chdir(tmp_board.parent)
        _setup_clean_mas(tmp_board)
        monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")

        pid_path = tmp_board / "daemon.pid"
        pid_path.unlink(missing_ok=True)
        assert not pid_path.exists(), "daemon.pid should not exist for this test"

        result = runner.invoke(app, ["doctor"])
        assert result.exit_code != 2, (
            f"exit_code=2 means 'doctor' is not registered.\nOutput: {result.output}"
        )
        assert result.exit_code == 0, (
            f"Expected exit 0 when daemon.pid absent, got {result.exit_code}.\n{result.output}"
        )
        assert "Daemon" in result.output, (
            f"Expected 'Daemon' group in output; got:\n{result.output}"
        )
