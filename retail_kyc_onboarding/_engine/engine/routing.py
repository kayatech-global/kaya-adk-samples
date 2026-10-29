"""Choosing the next edge. Rules: first true conditional edge, else first plain edge, else first target."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..model import END, Edge, Graph, Node
from .evaluator import Scope, evaluate_condition
from .plan import END_ROUTE


def edge_key(index: int) -> str:
    """Route key for the outgoing edge at this index."""
    return f"e{index}"


@dataclass
class RouteDecision:
    """The chosen outgoing edge (or END_ROUTE), why it was chosen, and any condition errors met on the way."""
    key: str  # "e<index>" into the node's outgoing list, or END_ROUTE
    edge: Edge | None
    reason: str
    errors: list[str]


def _target_key(outgoing: list[Edge], i: int) -> str:
    return END_ROUTE if outgoing[i].target == END else edge_key(i)


def select_edge(
    graph: Graph, node: Node, scope: Scope, mode: str = "strict", *, label_fn: Callable[[], str | None] | None = None
) -> RouteDecision:
    """Choose the next edge of a node.

    In label routing, ``label_fn`` result is matched against edge labels, then targets, then a default or only edge. Otherwise the first true conditional edge wins, then the first plain edge, then the first target.
    """
    outgoing = graph.outgoing(node.id)
    if not outgoing:
        return RouteDecision(END_ROUTE, None, "no outgoing edge", [])
    errors: list[str] = []
    if node.routing == "label" and label_fn is not None:
        label = label_fn()
        if label is not None:
            for i, e in enumerate(outgoing):
                if e.label == label:
                    return RouteDecision(_target_key(outgoing, i), e, f"label '{label}'", errors)
            for i, e in enumerate(outgoing):
                if e.target == label:
                    return RouteDecision(_target_key(outgoing, i), e, f"target '{label}'", errors)
            errors.append(f"condition returned '{label}', which matches no outgoing edge")
        for i, e in enumerate(outgoing):
            if e.label in (None, "", "default"):
                return RouteDecision(_target_key(outgoing, i), e, "default edge", errors)
        if len(outgoing) == 1:
            return RouteDecision(_target_key(outgoing, 0), outgoing[0], "only edge", errors)
        return RouteDecision(END_ROUTE, None, "no matching edge", errors)
    for i, e in enumerate(outgoing):
        if e.condition:
            res = evaluate_condition(e.condition, scope, mode)
            if res.error:
                errors.append(f"{e.condition!r}: {res.error}")
            if res.value:
                return RouteDecision(_target_key(outgoing, i), e, f"condition {e.condition!r}", errors)
    for i, e in enumerate(outgoing):
        if not e.condition:
            return RouteDecision(_target_key(outgoing, i), e, "first unconditional edge", errors)
    return RouteDecision(_target_key(outgoing, 0), outgoing[0], "first target", errors)


def allowed_targets(graph: Graph, node: Node) -> list[str]:
    """Targets reachable from a node, excluding the end marker."""
    return [e.target for e in graph.outgoing(node.id) if e.target != END]


def edge_for_target(graph: Graph, node: Node, target: str | None) -> RouteDecision:
    """Decision for routing to a named target; None means finish. An unknown target ends the run and reports an error."""
    outgoing = graph.outgoing(node.id)
    if target is None:
        for e in outgoing:
            if e.target == END:
                return RouteDecision(END_ROUTE, e, "finish", [])
        return RouteDecision(END_ROUTE, None, "finish", [])
    for i, e in enumerate(outgoing):
        if e.target == target:
            return RouteDecision(edge_key(i), e, f"chose {target}", [])
    return RouteDecision(END_ROUTE, None, f"'{target}' is not an outgoing target", [f"'{target}' is not an outgoing target"])


def scope_from(variables: Mapping[str, Any], custom: Mapping[str, Any] | None = None) -> Scope:
    """Build an expression scope from variables and optional custom state."""
    return Scope(variables=variables, custom_state=custom or {})
