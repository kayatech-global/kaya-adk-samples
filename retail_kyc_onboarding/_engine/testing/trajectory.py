"""Does a run follow the graph? Checks the recorded steps and routes against the nodes and edges of the export."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..model import END, Graph


def check_trajectory(records: Iterable[dict[str, Any]], graph: Graph) -> list[str]:
    """Problems found (empty means the trajectory is a valid walk of the graph). Records of child IFLOWs are ignored."""
    problems: list[str] = []
    steps = [r for r in records if r.get("event") == "step.start" and not r.get("scope")]
    routes = [r for r in records if r.get("event") == "route" and not r.get("scope")]
    ids = {n.id for n in graph.nodes}
    if not steps:
        return ["no step was executed"]
    starts = set(graph.start_targets())
    if steps[0]["node"] not in starts:
        problems.append(f"first step '{steps[0]['node']}' is not a start node {sorted(starts)}")
    for s in steps:
        if s["node"] not in ids:
            problems.append(f"step '{s['node']}' is not a node of the graph")
    for r in routes:
        node = r["node"]
        if node not in ids:
            continue
        allowed = {e.target for e in graph.outgoing(node)}
        chosen = r.get("chosen")
        if r.get("supervisor") and chosen == "FINISH":
            chosen = END
        if chosen not in allowed and chosen != END:
            problems.append(f"route from '{node}' went to '{chosen}', which is not an outgoing edge ({sorted(allowed)})")
    # every executed step after the first must be reachable from the previously routed node
    last_route: dict[str, Any] = {r["node"]: r.get("chosen") for r in routes}
    seen_nodes = [s["node"] for s in steps]
    for prev, cur in zip(seen_nodes, seen_nodes[1:], strict=False):
        outgoing = {e.target for e in graph.outgoing(prev)}
        if cur not in outgoing and prev in ids and last_route.get(prev) is not None and cur != last_route.get(prev) and outgoing:
            if not any(cur == t for t in outgoing):
                problems.append(f"step '{cur}' ran after '{prev}' but no edge connects them")
    return problems
