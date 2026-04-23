from pathlib import Path

import pytest
from typer.testing import CliRunner

from mas import board
from mas.cli import app, _templates_dir

runner = CliRunner()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Return a tmp_path that looks like an initialized project root."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    return tmp_path


def test_upgrade_copies_template_files(project: Path, monkeypatch):
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["upgrade", "--yes"])
    assert result.exit_code == 0, result.output

    mas = project / ".mas"
    tpl = _templates_dir()
    for name in ("config.yaml", "roles.yaml"):
        if (tpl / name).exists():
            assert (mas / name).exists(), f"{name} was not written"
    prompts_src = tpl / "prompts"
    if prompts_src.exists():
        for p in prompts_src.iterdir():
            assert (mas / "prompts" / p.name).exists(), f"prompt {p.name} was not written"


def test_upgrade_preserves_tasks_and_logs(project: Path, monkeypatch):
    monkeypatch.chdir(project)
    mas = project / ".mas"

    # Seed a task and a log file that must survive upgrade.
    task_dir = mas / "tasks" / "doing" / "t1"
    task_dir.mkdir(parents=True)
    (task_dir / "task.json").write_text('{"id":"t1"}')
    log_file = mas / "logs" / "run.log"
    log_file.write_text("some log")

    result = runner.invoke(app, ["upgrade", "--yes"])
    assert result.exit_code == 0, result.output

    assert (task_dir / "task.json").exists()
    assert log_file.read_text() == "some log"


def test_upgrade_does_not_overwrite_ideas(project: Path, monkeypatch):
    monkeypatch.chdir(project)
    mas = project / ".mas"
    ideas = mas / "ideas.md"
    ideas.write_text("# My ideas\n\n- custom idea\n")

    result = runner.invoke(app, ["upgrade", "--yes"])
    assert result.exit_code == 0, result.output

    assert ideas.read_text() == "# My ideas\n\n- custom idea\n"


def test_upgrade_dry_run_writes_nothing(project: Path, monkeypatch):
    monkeypatch.chdir(project)
    mas = project / ".mas"
    before = {p: p.stat().st_mtime for p in mas.rglob("*") if p.is_file()}

    result = runner.invoke(app, ["upgrade", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output

    after = {p: p.stat().st_mtime for p in mas.rglob("*") if p.is_file()}
    assert before == after, "dry-run must not modify any files"


def test_upgrade_dry_run_lists_files(project: Path, monkeypatch):
    monkeypatch.chdir(project)
    tpl = _templates_dir()
    result = runner.invoke(app, ["upgrade", "--dry-run"])
    assert result.exit_code == 0, result.output

    for name in ("config.yaml", "roles.yaml"):
        if (tpl / name).exists():
            assert name in result.output


def test_upgrade_creates_missing_layout_dirs(project: Path, monkeypatch):
    monkeypatch.chdir(project)
    mas = project / ".mas"
    # Remove prompts dir to simulate a partially initialized .mas.
    import shutil
    shutil.rmtree(mas / "prompts")

    result = runner.invoke(app, ["upgrade", "--yes"])
    assert result.exit_code == 0, result.output
    assert (mas / "prompts").is_dir()


def test_upgrade_shows_diff_for_modified_files(project: Path, monkeypatch):
    monkeypatch.chdir(project)
    mas = project / ".mas"
    tpl = _templates_dir()

    # Seed config.yaml with content that differs from the template so a diff renders.
    src = tpl / "config.yaml"
    if not src.exists():
        pytest.skip("no config.yaml template to compare against")
    (mas / "config.yaml").write_text("# stale user config\n")

    result = runner.invoke(app, ["upgrade", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "update:" in result.output
    # Unified diff markers should appear.
    assert "---" in result.output
    assert "+++" in result.output
    assert "stale user config" in result.output


def test_upgrade_aborts_without_confirmation(project: Path, monkeypatch):
    monkeypatch.chdir(project)
    mas = project / ".mas"
    (mas / "config.yaml").write_text("# stale\n")

    result = runner.invoke(app, ["upgrade"], input="n\n")
    assert result.exit_code == 1, result.output
    assert "aborted" in result.output
    assert (mas / "config.yaml").read_text() == "# stale\n"


def test_upgrade_applies_on_yes_input(project: Path, monkeypatch):
    monkeypatch.chdir(project)
    mas = project / ".mas"
    tpl = _templates_dir()
    src = tpl / "config.yaml"
    if not src.exists():
        pytest.skip("no config.yaml template to compare against")
    (mas / "config.yaml").write_text("# stale\n")

    result = runner.invoke(app, ["upgrade"], input="y\n")
    assert result.exit_code == 0, result.output
    assert (mas / "config.yaml").read_bytes() == src.read_bytes()


def test_upgrade_noop_when_up_to_date(project: Path, monkeypatch):
    monkeypatch.chdir(project)
    # First upgrade brings everything up to date.
    result = runner.invoke(app, ["upgrade", "--yes"])
    assert result.exit_code == 0, result.output

    # Second upgrade should short-circuit without prompting.
    result = runner.invoke(app, ["upgrade"])
    assert result.exit_code == 0, result.output
    assert "already up to date" in result.output


def test_upgrade_restarts_running_daemon(project: Path, monkeypatch):
    from unittest.mock import patch

    monkeypatch.chdir(project)
    mas = project / ".mas"
    (mas / "config.yaml").write_text("# stale\n")

    with patch("mas.daemon.status", return_value=(4242, True)), \
         patch("mas.daemon.stop", return_value=True) as mock_stop, \
         patch("mas.daemon.start", return_value=5151) as mock_start, \
         patch("mas.daemon.read_interval", return_value=120):
        result = runner.invoke(app, ["upgrade", "--yes"])

    assert result.exit_code == 0, result.output
    assert "daemon restarted" in result.output
    mock_stop.assert_called_once()
    mock_start.assert_called_once()
    _, kwargs = mock_start.call_args
    assert kwargs.get("interval_seconds") == 120


def test_upgrade_skips_daemon_restart_when_declined(project: Path, monkeypatch):
    from unittest.mock import patch

    monkeypatch.chdir(project)
    mas = project / ".mas"
    (mas / "config.yaml").write_text("# stale\n")

    with patch("mas.daemon.status", return_value=(4242, True)), \
         patch("mas.daemon.stop") as mock_stop, \
         patch("mas.daemon.start") as mock_start, \
         patch("mas.daemon.read_interval", return_value=300):
        # "y" to apply upgrade, "n" to skip daemon restart.
        result = runner.invoke(app, ["upgrade"], input="y\nn\n")

    assert result.exit_code == 0, result.output
    assert "skipping daemon restart" in result.output
    mock_stop.assert_not_called()
    mock_start.assert_not_called()


def test_upgrade_does_not_prompt_restart_when_no_daemon(project: Path, monkeypatch):
    from unittest.mock import patch

    monkeypatch.chdir(project)
    mas = project / ".mas"
    (mas / "config.yaml").write_text("# stale\n")

    with patch("mas.daemon.status", return_value=(None, False)), \
         patch("mas.daemon.start") as mock_start:
        result = runner.invoke(app, ["upgrade", "--yes"])

    assert result.exit_code == 0, result.output
    assert "daemon is running" not in result.output
    mock_start.assert_not_called()
