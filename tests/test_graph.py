"""Tests for parent_dir/graph.json: nodes, edges, and prior-results derivation."""
from __future__ import annotations

from pathlib import Path

from mas.graph import (
    Graph,
    GraphEdge,
    GraphNode,
    add_arbiter_link,
    add_revision_link,
    derive_prior_results,
    graph_path,
    read_graph,
    sync_from_plan,
    update_node_from_result,
    write_graph,
)
from mas.schemas import Plan, Result, SubtaskSpec


def _plan(subtasks: list[SubtaskSpec], parent_id: str = "20260415-p-aaaa") -> Plan:
    return Plan(parent_id=parent_id, summary="s", subtasks=subtasks)


def test_read_graph_missing_returns_empty(tmp_path: Path):
    g = read_graph(tmp_path)
    assert isinstance(g, Graph)
    assert g.nodes == []
    assert g.edges == []


def test_write_read_roundtrip(tmp_path: Path):
    g = Graph(
        nodes=[GraphNode(subtask_id="t-1", role="tester")],
        edges=[GraphEdge(from_id="t-1", to_id="i-1", kind="sequence")],
    )
    write_graph(tmp_path, g)
    assert graph_path(tmp_path).exists()
    out = read_graph(tmp_path)
    assert out.model_dump() == g.model_dump()


def test_invalid_graph_falls_back_to_empty(tmp_path: Path):
    graph_path(tmp_path).write_text("{not json")
    g = read_graph(tmp_path)
    assert g == Graph()


def test_sync_from_plan_creates_nodes_and_sequence_edges():
    g = Graph()
    plan = _plan([
        SubtaskSpec(id="t-1", role="tester", goal="t"),
        SubtaskSpec(id="i-1", role="implementer", goal="i"),
        SubtaskSpec(id="e-1", role="evaluator", goal="e"),
    ])
    assert sync_from_plan(g, plan) is True
    assert [n.subtask_id for n in g.nodes] == ["t-1", "i-1", "e-1"]
    assert [n.role for n in g.nodes] == ["tester", "implementer", "evaluator"]
    assert all(n.cycle == 0 for n in g.nodes)
    assert [(e.from_id, e.to_id, e.kind) for e in g.edges] == [
        ("t-1", "i-1", "sequence"),
        ("i-1", "e-1", "sequence"),
    ]


def test_sync_from_plan_idempotent():
    g = Graph()
    plan = _plan([
        SubtaskSpec(id="t-1", role="tester", goal="t"),
        SubtaskSpec(id="i-1", role="implementer", goal="i"),
    ])
    sync_from_plan(g, plan)
    assert sync_from_plan(g, plan) is False
    assert len(g.nodes) == 2
    assert len(g.edges) == 1


def test_sync_from_plan_cycle_inferred_from_revision_id():
    g = Graph()
    plan = _plan([
        SubtaskSpec(id="t-1", role="tester", goal="t"),
        SubtaskSpec(id="rev-2-implementer", role="implementer", goal="i"),
    ])
    sync_from_plan(g, plan)
    cycles = {n.subtask_id: n.cycle for n in g.nodes}
    assert cycles == {"t-1": 0, "rev-2-implementer": 2}


def test_update_node_from_result_fills_status_and_handoff():
    g = Graph()
    plan = _plan([SubtaskSpec(id="t-1", role="tester", goal="t")])
    sync_from_plan(g, plan)
    spec = plan.subtasks[0]
    r = Result(
        task_id="t-1",
        status="success",
        summary="wrote tests",
        verdict=None,
        feedback=None,
        artifacts=["tests/x.py"],
        handoff={"test_command": "pytest -q", "initial_exit_code": 1},
    )
    assert update_node_from_result(g, spec, r) is True
    n = g.nodes[0]
    assert n.status == "success"
    assert n.summary == "wrote tests"
    assert n.artifacts == ["tests/x.py"]
    assert n.handoff == {"test_command": "pytest -q", "initial_exit_code": 1}


def test_update_node_inserts_missing_node():
    g = Graph()
    spec = SubtaskSpec(id="orphan-1", role="implementer", goal="x")
    r = Result(task_id="orphan-1", status="failure", summary="bad")
    update_node_from_result(g, spec, r)
    assert len(g.nodes) == 1
    assert g.nodes[0].subtask_id == "orphan-1"
    assert g.nodes[0].status == "failure"


def test_add_revision_link_attaches_feedback_to_edges():
    g = Graph()
    plan = _plan([
        SubtaskSpec(id="e-1", role="evaluator", goal="e"),
        SubtaskSpec(id="rev-1-tester", role="tester", goal="rt"),
        SubtaskSpec(id="rev-1-implementer", role="implementer", goal="ri"),
    ])
    sync_from_plan(g, plan)
    feedback = "missing coverage for edge case Y"
    added = add_revision_link(
        g,
        from_evaluator_id="e-1",
        new_subtask_ids=["rev-1-tester", "rev-1-implementer"],
        feedback=feedback,
    )
    assert added is True
    rev_edges = [e for e in g.edges if e.kind == "revision"]
    assert {(e.from_id, e.to_id) for e in rev_edges} == {
        ("e-1", "rev-1-tester"),
        ("e-1", "rev-1-implementer"),
    }
    assert all(e.reason == feedback for e in rev_edges)


def test_add_revision_link_idempotent():
    g = Graph()
    sync_from_plan(g, _plan([
        SubtaskSpec(id="e-1", role="evaluator", goal="e"),
        SubtaskSpec(id="rev-1-tester", role="tester", goal="rt"),
    ]))
    add_revision_link(g, from_evaluator_id="e-1", new_subtask_ids=["rev-1-tester"], feedback="x")
    assert add_revision_link(
        g, from_evaluator_id="e-1", new_subtask_ids=["rev-1-tester"], feedback="x"
    ) is False
    assert sum(1 for e in g.edges if e.kind == "revision") == 1


def test_add_arbiter_link():
    g = Graph()
    sync_from_plan(g, _plan([
        SubtaskSpec(id="e-1", role="evaluator", goal="e"),
        SubtaskSpec(id="arbiter-1", role="arbiter", goal="a"),
    ]))
    assert add_arbiter_link(
        g, from_evaluator_id="e-1", arbiter_id="arbiter-1", feedback="dispute"
    ) is True
    arb = [e for e in g.edges if e.kind == "arbiter"]
    assert len(arb) == 1
    assert arb[0].from_id == "e-1"
    assert arb[0].to_id == "arbiter-1"
    assert arb[0].reason == "dispute"


def test_derive_prior_results_returns_only_completed_priors():
    g = Graph()
    plan = _plan([
        SubtaskSpec(id="t-1", role="tester", goal="t"),
        SubtaskSpec(id="i-1", role="implementer", goal="i"),
        SubtaskSpec(id="e-1", role="evaluator", goal="e"),
    ])
    sync_from_plan(g, plan)
    update_node_from_result(g, plan.subtasks[0],
        Result(task_id="t-1", status="success", summary="ok",
               handoff={"test_command": "pytest -q"}))
    # i-1 has no result yet → status stays None → excluded.
    out = derive_prior_results(g, plan, current_id="e-1")
    assert [task_id for _, task_id in [(role, r.task_id) for role, r in out]] == ["t-1"]
    assert out[0][0] == "tester"
    assert out[0][1].handoff == {"test_command": "pytest -q"}


def test_derive_prior_results_folds_revision_causality_into_feedback():
    """A node downstream of a `revision` edge surfaces the edge reason as a
    `[caused by ...]` prefix in its feedback. Lets evaluators see "rev-2
    exists because rev-1 evaluator complained about X"."""
    g = Graph()
    plan = _plan([
        SubtaskSpec(id="e-1", role="evaluator", goal="e"),
        SubtaskSpec(id="rev-1-implementer", role="implementer", goal="ri"),
        SubtaskSpec(id="rev-1-evaluator", role="evaluator", goal="re"),
    ])
    sync_from_plan(g, plan)
    update_node_from_result(g, plan.subtasks[0],
        Result(task_id="e-1", status="success", verdict="needs_revision",
               summary="missed Y", feedback="add coverage for Y"))
    add_revision_link(
        g,
        from_evaluator_id="e-1",
        new_subtask_ids=["rev-1-implementer", "rev-1-evaluator"],
        feedback="add coverage for Y",
    )
    update_node_from_result(g, plan.subtasks[1],
        Result(task_id="rev-1-implementer", status="success",
               summary="patched", feedback="green"))

    out = derive_prior_results(g, plan, current_id="rev-1-evaluator")
    impl = [r for role, r in out if role == "implementer"][0]
    assert impl.feedback is not None
    assert "[caused by e-1 (revision)" in impl.feedback
    assert "add coverage for Y" in impl.feedback
    # Original feedback preserved after the prefix.
    assert impl.feedback.endswith("green")


def test_derive_prior_results_stops_at_current_id():
    g = Graph()
    plan = _plan([
        SubtaskSpec(id="t-1", role="tester", goal="t"),
        SubtaskSpec(id="i-1", role="implementer", goal="i"),
    ])
    sync_from_plan(g, plan)
    update_node_from_result(g, plan.subtasks[0],
        Result(task_id="t-1", status="success", summary="t"))
    update_node_from_result(g, plan.subtasks[1],
        Result(task_id="i-1", status="success", summary="i"))
    out = derive_prior_results(g, plan, current_id="i-1")
    assert [r.task_id for _, r in out] == ["t-1"]
