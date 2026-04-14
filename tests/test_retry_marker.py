"""End-to-end-ish test: verify that .previous_failure is read into a Task at
dispatch time. We don't actually spawn subprocesses; we invoke the internal
helper after seeding state."""

from pathlib import Path

import pytest

from mas import board
from mas.schemas import MasConfig, ProviderConfig, RoleConfig, Task


def test_previous_failure_injection(tmp_path: Path):
    """Test that .previous_failure file contents get injected into the task."""
    from mas.tick import _dispatch_role, TickEnv

    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    task_dir = board.task_dir(mas, "doing", "t1")
    task_dir.mkdir(parents=True)

    # Use mock provider with a fixture result.
    fixture = tmp_path / "fx.json"
    fixture.write_text('{"task_id":"t1","status":"success","summary":"ok","duration_s":0}')

    cfg = MasConfig(
        providers={"mock": ProviderConfig(cli="sh", max_concurrent=1, extra_args=[str(fixture)])},
        roles={
            "proposer": RoleConfig(provider="mock"),
            "orchestrator": RoleConfig(provider="mock"),
            "implementer": RoleConfig(provider="mock"),
            "tester": RoleConfig(provider="mock"),
            "evaluator": RoleConfig(provider="mock"),
        },
    )

    (task_dir / ".previous_failure").write_text("prior run crashed on X")

    # Minimal prompt template so render_prompt doesn't no-op
    (mas / "prompts").mkdir(exist_ok=True)
    (mas / "prompts" / "implementer.md").write_text("goal=$goal prev=$previous_failure")

    env = TickEnv(repo=tmp_path, mas=mas, cfg=cfg)
    task = Task(id="t1", role="implementer", goal="do")
    _dispatch_role(env, task, task_dir, tmp_path, role="implementer")

    # .previous_failure should be consumed; task.json should contain the marker.
    assert not (task_dir / ".previous_failure").exists()
    persisted = board.read_task(task_dir)
    assert persisted.previous_failure == "prior run crashed on X"
