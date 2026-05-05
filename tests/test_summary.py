"""Tests for hierarchical summary generation (mas.summary)."""
from __future__ import annotations

from pathlib import Path

from mas.graph import (
    Graph,
    GraphEdge,
    GraphNode,
    write_graph,
)
from mas.summary import (
    SUMMARY_THRESHOLD,
    SUMMARY_FILENAME,
    maybe_write_summary,
    read_summary,
    render_summary,
    should_generate_summary,
    summary_path,
)


def _node(
    subtask_id: str,
    role: str,
    *,
    cycle: int = 0,
    status: str | None = "success",
    verdict: str | None = None,
    summary: str | None = None,
    artifacts: list[str] | None = None,
) -> GraphNode:
    return GraphNode(
        subtask_id=subtask_id,
        role=role,
        cycle=cycle,
        status=status,
        verdict=verdict,
        summary=summary,
        artifacts=artifacts or [],
    )


def test_threshold_constant_is_five():
    assert SUMMARY_THRESHOLD == 5


def test_should_generate_returns_false_below_threshold():
    g = Graph(nodes=[_node(f"s-{i}", "implementer") for i in range(SUMMARY_THRESHOLD)])
    assert should_generate_summary(g) is False


def test_should_generate_true_above_threshold():
    g = Graph(
        nodes=[_node(f"s-{i}", "implementer") for i in range(SUMMARY_THRESHOLD + 1)]
    )
    assert should_generate_summary(g) is True


def test_should_generate_ignores_pending_nodes():
    """Nodes without a recorded status (status is None) don't count."""
    g = Graph(
        nodes=[_node(f"s-{i}", "implementer") for i in range(SUMMARY_THRESHOLD)]
        + [_node("pending", "implementer", status=None)]
    )
    assert should_generate_summary(g) is False


def test_read_summary_returns_none_when_missing(tmp_path: Path):
    assert read_summary(tmp_path) is None


def test_maybe_write_summary_below_threshold_returns_none(tmp_path: Path):
    g = Graph(nodes=[_node(f"s-{i}", "implementer") for i in range(3)])
    write_graph(tmp_path, g)
    assert maybe_write_summary(tmp_path, "parent goal") is None
    assert not summary_path(tmp_path).exists()


def test_maybe_write_summary_above_threshold(tmp_path: Path):
    g = Graph(
        nodes=[
            _node("test-1", "tester", summary="wrote failing tests"),
            _node("impl-1", "implementer", summary="implemented X"),
            _node("eval-1", "evaluator", verdict="needs_revision",
                  summary="more tests needed"),
            _node("rev-1-tester", "tester", cycle=1, summary="extra tests"),
            _node("rev-1-implementer", "implementer", cycle=1, summary="fix"),
            _node("rev-1-evaluator", "evaluator", cycle=1, verdict="pass",
                  summary="ok now"),
        ],
        edges=[
            GraphEdge(from_id="eval-1", to_id="rev-1-tester", kind="revision",
                      reason="missing edge case"),
        ],
    )
    write_graph(tmp_path, g)
    written = maybe_write_summary(tmp_path, "Add feature X")
    assert written == summary_path(tmp_path)
    text = read_summary(tmp_path)
    assert text is not None
    assert "Add feature X" in text
    assert "Completed subtasks: 6" in text
    assert "Initial cycle" in text
    assert "Revision cycle 1" in text
    # Causal edge surfaces with the failing evaluator's reason
    assert "caused by `eval-1` (revision): missing edge case" in text
    # Each subtask id appears
    for sid in ("test-1", "impl-1", "eval-1", "rev-1-implementer"):
        assert sid in text


def test_render_summary_truncates_long_node_summaries():
    long = "x" * 500
    g = Graph(
        nodes=[_node(f"s-{i}", "implementer", summary=long) for i in range(6)]
    )
    text = render_summary(g, "goal")
    # Each node summary capped at 240 chars in the rendered list
    assert "x" * 241 not in text


def test_render_summary_lists_artifacts():
    g = Graph(
        nodes=[
            _node(
                "impl-1",
                "implementer",
                summary="did the thing",
                artifacts=["src/foo.py", "src/bar.py"],
            )
        ]
        + [_node(f"f-{i}", "tester") for i in range(5)]
    )
    text = render_summary(g, "goal")
    assert "src/foo.py" in text
    assert "src/bar.py" in text


def test_summary_path_is_under_parent_dir(tmp_path: Path):
    assert summary_path(tmp_path) == tmp_path / SUMMARY_FILENAME
