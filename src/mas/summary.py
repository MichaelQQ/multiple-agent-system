"""Hierarchical summary for parents with many completed subtasks.

Once a parent has more than `SUMMARY_THRESHOLD` subtasks with recorded
results in its graph, walk the graph to render a markdown digest at
`parent_dir/summary.md`. Subsequent child dispatches inject this digest in
place of the full per-subtask `prior_results_json` so prompts stop growing
linearly with cycle count.

Deterministic — produced from `graph.json` data only, no LLM call.
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import graph as _graph
from .graph import Graph, GraphEdge, GraphNode

log = logging.getLogger("mas.summary")

SUMMARY_THRESHOLD = 5
SUMMARY_FILENAME = "summary.md"


def summary_path(parent_dir: Path) -> Path:
    return parent_dir / SUMMARY_FILENAME


def read_summary(parent_dir: Path) -> str | None:
    p = summary_path(parent_dir)
    if not p.exists():
        return None
    try:
        return p.read_text()
    except OSError:
        return None


def _completed(graph: Graph) -> list[GraphNode]:
    return [n for n in graph.nodes if n.status is not None]


def should_generate_summary(graph: Graph) -> bool:
    return len(_completed(graph)) > SUMMARY_THRESHOLD


def _causal_edges(graph: Graph) -> dict[str, list[GraphEdge]]:
    by_target: dict[str, list[GraphEdge]] = {}
    for e in graph.edges:
        if e.kind in ("revision", "arbiter", "replan"):
            by_target.setdefault(e.to_id, []).append(e)
    return by_target


def render_summary(graph: Graph, parent_goal: str) -> str:
    nodes = _completed(graph)
    causes = _causal_edges(graph)

    by_cycle: dict[int, list[GraphNode]] = {}
    for n in nodes:
        by_cycle.setdefault(n.cycle, []).append(n)

    lines: list[str] = [
        "# Parent task summary",
        "",
        f"**Goal:** {parent_goal}",
        "",
        f"Completed subtasks: {len(nodes)}",
        "",
    ]

    for cycle in sorted(by_cycle):
        title = "Initial cycle" if cycle == 0 else f"Revision cycle {cycle}"
        lines.append(f"## {title}")
        lines.append("")
        for n in by_cycle[cycle]:
            verdict = f", verdict={n.verdict}" if n.verdict else ""
            lines.append(
                f"- **{n.subtask_id}** ({n.role}, status={n.status}{verdict})"
            )
            if n.summary:
                snippet = " ".join(n.summary.split())[:240]
                lines.append(f"  - {snippet}")
            for e in causes.get(n.subtask_id, []):
                reason = (e.reason or "").splitlines()[0][:200] if e.reason else ""
                if reason:
                    lines.append(
                        f"  - caused by `{e.from_id}` ({e.kind}): {reason}"
                    )
                else:
                    lines.append(f"  - caused by `{e.from_id}` ({e.kind})")
            if n.artifacts:
                lines.append(
                    f"  - artifacts: {', '.join(str(a) for a in n.artifacts[:6])}"
                )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def maybe_write_summary(parent_dir: Path, parent_goal: str) -> Path | None:
    """Write/refresh `parent_dir/summary.md` when the threshold is exceeded.

    Returns the path written, or None when the threshold is not yet met or
    the graph is unreadable. Idempotent — overwrites with current graph state.
    """
    graph = _graph.read_graph(parent_dir)
    if not should_generate_summary(graph):
        return None
    text = render_summary(graph, parent_goal)
    path = summary_path(parent_dir)
    path.write_text(text)
    return path
