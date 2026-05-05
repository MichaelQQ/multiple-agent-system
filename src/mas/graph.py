"""Per-parent task graph: nodes = subtasks, edges = causality links.

Replaces the flat `prior_results` view with a structured DAG so revision
cycles, arbiter dispatches, and replans carry explicit provenance ("rev-2
exists because rev-1 evaluator returned needs_revision: <feedback>").

Lives at `parent_dir/graph.json`. Updated by tick on plan parse, child
completion, revision-cycle append, arbiter append, and replan trigger.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field

from .schemas import Plan, Result, SubtaskSpec

log = logging.getLogger("mas.graph")


EdgeKind = str  # "sequence" | "revision" | "arbiter" | "replan"


class GraphNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subtask_id: str
    role: str
    cycle: int = 0
    status: str | None = None
    verdict: str | None = None
    summary: str | None = None
    feedback: str | None = None
    artifacts: list[str] = Field(default_factory=list)
    handoff: dict[str, Any] | None = None


class GraphEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_id: str
    to_id: str
    kind: EdgeKind
    reason: str | None = None


class Graph(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)


def graph_path(parent_dir: Path) -> Path:
    return parent_dir / "graph.json"


def read_graph(parent_dir: Path) -> Graph:
    p = graph_path(parent_dir)
    if not p.exists():
        return Graph()
    try:
        return Graph.model_validate_json(p.read_text())
    except Exception:
        log.warning("invalid graph.json, starting fresh", extra={"path": str(p)})
        return Graph()


def write_graph(parent_dir: Path, graph: Graph) -> None:
    graph_path(parent_dir).write_text(graph.model_dump_json(indent=2))


def _cycle_from_spec_id(spec_id: str) -> int:
    if spec_id.startswith("rev-"):
        try:
            return int(spec_id.split("-", 2)[1])
        except (IndexError, ValueError):
            return 0
    return 0


def _node_index(graph: Graph) -> dict[str, int]:
    return {n.subtask_id: i for i, n in enumerate(graph.nodes)}


def _edge_exists(graph: Graph, from_id: str, to_id: str, kind: EdgeKind) -> bool:
    return any(
        e.from_id == from_id and e.to_id == to_id and e.kind == kind
        for e in graph.edges
    )


def sync_from_plan(graph: Graph, plan: Plan) -> bool:
    """Ensure every plan subtask has a node and consecutive subtasks have a
    sequence edge. Idempotent. Returns True if anything was added."""
    changed = False
    idx = _node_index(graph)

    for spec in plan.subtasks:
        if spec.id not in idx:
            graph.nodes.append(
                GraphNode(
                    subtask_id=spec.id,
                    role=spec.role,
                    cycle=_cycle_from_spec_id(spec.id),
                )
            )
            idx[spec.id] = len(graph.nodes) - 1
            changed = True

    for prev, curr in zip(plan.subtasks, plan.subtasks[1:]):
        if not _edge_exists(graph, prev.id, curr.id, "sequence"):
            graph.edges.append(GraphEdge(from_id=prev.id, to_id=curr.id, kind="sequence"))
            changed = True

    return changed


def update_node_from_result(graph: Graph, spec: SubtaskSpec, result: Result) -> bool:
    """Fill in result fields on the node matching `spec.id`. Inserts the node
    if missing. Returns True if anything changed."""
    idx = _node_index(graph)
    if spec.id not in idx:
        graph.nodes.append(
            GraphNode(subtask_id=spec.id, role=spec.role, cycle=_cycle_from_spec_id(spec.id))
        )
        idx[spec.id] = len(graph.nodes) - 1

    node = graph.nodes[idx[spec.id]]
    node.status = result.status
    node.verdict = result.verdict
    node.summary = result.summary or None
    node.feedback = result.feedback or None
    node.artifacts = list(result.artifacts) if result.artifacts else []
    node.handoff = result.handoff
    return True


def add_revision_link(
    graph: Graph,
    *,
    from_evaluator_id: str,
    new_subtask_ids: Iterable[str],
    feedback: str,
) -> bool:
    """Add `revision` edges from a failing evaluator to each newly-appended
    revision-cycle subtask, carrying the evaluator's feedback as the edge
    reason. Returns True if any edge was added."""
    changed = False
    for new_id in new_subtask_ids:
        if not _edge_exists(graph, from_evaluator_id, new_id, "revision"):
            graph.edges.append(
                GraphEdge(
                    from_id=from_evaluator_id,
                    to_id=new_id,
                    kind="revision",
                    reason=feedback or None,
                )
            )
            changed = True
    return changed


def add_arbiter_link(
    graph: Graph,
    *,
    from_evaluator_id: str,
    arbiter_id: str,
    feedback: str,
) -> bool:
    """Add an `arbiter` edge from a needs_revision evaluator to the arbiter
    subtask dispatched to resolve the disagreement."""
    if _edge_exists(graph, from_evaluator_id, arbiter_id, "arbiter"):
        return False
    graph.edges.append(
        GraphEdge(
            from_id=from_evaluator_id,
            to_id=arbiter_id,
            kind="arbiter",
            reason=feedback or None,
        )
    )
    return True


def derive_prior_results(
    graph: Graph, plan: Plan, current_id: str
) -> list[tuple[str, Result]]:
    """Walk the graph in plan order and return (role, Result) pairs for
    every subtask preceding `current_id` that has a recorded status.

    Causality from incoming `revision` / `arbiter` edges is folded into the
    Result.feedback as a `[caused by <from_id>: ...]` prefix, so the
    consumer (compress/retrieval helpers, prompt rendering) sees explicit
    provenance instead of a flat list.
    """
    by_id: dict[str, GraphNode] = {n.subtask_id: n for n in graph.nodes}
    incoming: dict[str, list[GraphEdge]] = {}
    for e in graph.edges:
        if e.kind in ("revision", "arbiter"):
            incoming.setdefault(e.to_id, []).append(e)

    out: list[tuple[str, Result]] = []
    for spec in plan.subtasks:
        if spec.id == current_id:
            break
        node = by_id.get(spec.id)
        if node is None or node.status is None:
            continue

        feedback = node.feedback
        causality_lines = [
            f"[caused by {e.from_id} ({e.kind}): {(e.reason or '').splitlines()[0][:200]}]"
            for e in incoming.get(spec.id, [])
            if e.reason
        ]
        if causality_lines:
            prefix = "\n".join(causality_lines)
            feedback = f"{prefix}\n{feedback}" if feedback else prefix

        out.append(
            (
                node.role,
                Result(
                    task_id=node.subtask_id,
                    status=node.status,  # type: ignore[arg-type]
                    summary=node.summary or "",
                    artifacts=list(node.artifacts),
                    handoff=node.handoff,
                    verdict=node.verdict,  # type: ignore[arg-type]
                    feedback=feedback,
                ),
            )
        )
    return out
