from mas.schemas import Plan, Result, SubtaskSpec, Task


def test_task_roundtrip():
    t = Task(id="abc", role="implementer", goal="do the thing")
    data = t.model_dump_json()
    t2 = Task.model_validate_json(data)
    assert t2 == t


def test_result_roundtrip():
    r = Result(task_id="abc", status="success", summary="ok", duration_s=1.5)
    assert Result.model_validate_json(r.model_dump_json()) == r


def test_plan_subtask_order():
    p = Plan(
        parent_id="abc",
        summary="s",
        subtasks=[
            SubtaskSpec(id="a", role="implementer", goal="g"),
            SubtaskSpec(id="b", role="tester", goal="g"),
            SubtaskSpec(id="c", role="evaluator", goal="g"),
        ],
    )
    assert [s.id for s in p.subtasks] == ["a", "b", "c"]
    assert p.max_revision_cycles == 2
