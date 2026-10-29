"""Branch-only node: evaluates a generated condition function (plan) or the node's edge conditions (canvas)."""

from __future__ import annotations

from typing import Any

from ...model import Node
from ..plan import NodePlan, Phase, PhaseResult
from ..step import StepCtx
from ..units import ContentUnit
from .common import decide_route, enter


def build(engine: Any, unit: ContentUnit, node: Node) -> NodePlan:
    """Plan a branch node: choose the outgoing edge from the generated condition function or the edge conditions. Never pauses."""
    async def route(sc: StepCtx) -> PhaseResult:
        await enter(sc)
        decision = decide_route(sc, sc.node_input)
        sc.tracer.emit("step.end", node=node.id, kind="branch", output=decision.reason, is_error=False)
        return PhaseResult(output=sc.node_input, route=decision.key)

    return NodePlan(node, [Phase("route", route)])
