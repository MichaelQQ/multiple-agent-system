"""Tests that prompt templates for child roles write result.json to $task_dir,
not ./ (which resolves to the worktree when cwd=worktree).

Background: implementer and evaluator are dispatched with cwd=worktree, so
'./result.json' lands in the wrong directory. The tick loop looks for
result.json in the subtask dir, causing spurious 'exited without writing
result.json' failures."""

from pathlib import Path

import pytest

from mas.roles import render_prompt
from mas.schemas import Task

# Roles dispatched with cwd=worktree — must NOT use './result.json'.
CHILD_ROLES = ["implementer", "evaluator"]

# Proposer is dispatched with cwd=task_dir, so './' is fine there.
# Orchestrator already uses $task_dir — also safe.

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates" / "prompts"


def _make_task(role: str) -> Task:
    return Task(id="t1", role=role, goal="test goal", parent_id="parent-1")


def test_render_prompt_substitutes_task_dir(tmp_path: Path):
    """render_prompt must replace $task_dir with the supplied absolute path."""
    template = tmp_path / "implementer.md"
    template.write_text("Write $task_dir/result.json when done.")

    task = _make_task("implementer")
    result = render_prompt(template, task, task_dir="/abs/subtask/dir", worktree="/abs/worktree")

    assert "/abs/subtask/dir/result.json" in result
    assert "$task_dir" not in result


@pytest.mark.parametrize("role", CHILD_ROLES)
def test_child_role_template_uses_task_dir_not_dot_slash(role: str):
    """Child role templates must reference $task_dir/result.json, not ./result.json."""
    template = TEMPLATES_DIR / f"{role}.md"
    if not template.exists():
        pytest.skip(f"no template for {role}")

    content = template.read_text()

    assert "./result.json" not in content, (
        f"templates/prompts/{role}.md uses './result.json' — "
        "this resolves to the worktree, not the subtask dir. Use '$task_dir/result.json'."
    )
    assert "$task_dir/result.json" in content, (
        f"templates/prompts/{role}.md must write to '$task_dir/result.json'."
    )


@pytest.mark.parametrize("role", CHILD_ROLES)
def test_rendered_child_prompt_contains_absolute_result_path(tmp_path: Path, role: str):
    """After render_prompt, the result.json path must be absolute (contain task_dir)."""
    template = TEMPLATES_DIR / f"{role}.md"
    if not template.exists():
        pytest.skip(f"no template for {role}")

    task = _make_task(role)
    subtask_dir = str(tmp_path / "subtasks" / "impl-1")
    rendered = render_prompt(template, task, task_dir=subtask_dir, worktree=str(tmp_path / "worktree"))

    assert subtask_dir + "/result.json" in rendered
    assert "./result.json" not in rendered


def test_render_prompt_injects_prior_results_json(tmp_path: Path):
    """prior_results on the task are rendered as JSON into $prior_results_json."""
    from mas.schemas import Result

    template = tmp_path / "p.md"
    template.write_text("Priors:\n$prior_results_json\n")

    task = Task(
        id="impl-1",
        role="implementer",
        goal="make tests pass",
        parent_id="p",
        prior_results=[
            Result(
                task_id="test-1",
                status="success",
                summary="failing tests authored",
                handoff={"test_command": "pytest -q", "test_files": ["tests/x.py"]},
            ),
        ],
    )
    rendered = render_prompt(template, task, task_dir=str(tmp_path), worktree=str(tmp_path))

    assert '"task_id": "test-1"' in rendered
    assert '"test_command": "pytest -q"' in rendered


@pytest.mark.parametrize("role", ["tester", "implementer", "evaluator"])
def test_tdd_child_templates_reference_prior_results(role: str):
    """TDD child templates must surface $prior_results_json so the subtask
    can see preceding siblings' result.json (e.g. tester handoff)."""
    template = TEMPLATES_DIR / f"{role}.md"
    assert "$prior_results_json" in template.read_text(), (
        f"templates/prompts/{role}.md must reference $prior_results_json"
    )
