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
    result = runner.invoke(app, ["upgrade"])
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

    result = runner.invoke(app, ["upgrade"])
    assert result.exit_code == 0, result.output

    assert (task_dir / "task.json").exists()
    assert log_file.read_text() == "some log"


def test_upgrade_does_not_overwrite_ideas(project: Path, monkeypatch):
    monkeypatch.chdir(project)
    mas = project / ".mas"
    ideas = mas / "ideas.md"
    ideas.write_text("# My ideas\n\n- custom idea\n")

    result = runner.invoke(app, ["upgrade"])
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

    result = runner.invoke(app, ["upgrade"])
    assert result.exit_code == 0, result.output
    assert (mas / "prompts").is_dir()
