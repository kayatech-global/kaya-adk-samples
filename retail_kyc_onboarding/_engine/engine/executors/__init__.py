"""Node executors. Each ``build`` turns one graph node into a NodePlan (a chain of phases)."""

from __future__ import annotations

from typing import Any

from ...errors import ConfigError
from ...model import Node, NodeKind
from ..plan import NodePlan
from ..units import ContentUnit


def build_plan(engine: Any, unit: ContentUnit, node: Node) -> NodePlan:
    """Build the NodePlan for a node using the executor for its kind.

    Raises:
        ConfigError: The node kind has no executor in this core version.
    """
    from . import agent_node, branch, external_agent, followup, iteration, subflow, supervisor, tool

    builders = {
        NodeKind.AGENT: agent_node.build,
        NodeKind.TOOL: tool.build,
        NodeKind.SUPERVISOR: supervisor.build,
        NodeKind.BRANCH: branch.build,
        NodeKind.FOLLOWUP: followup.build,
        NodeKind.SUBFLOW: subflow.build,
        NodeKind.ITERATION: iteration.build,
        NodeKind.EXTERNAL_AGENT: external_agent.build,
    }
    try:
        return builders[node.kind](engine, unit, node)
    except KeyError as exc:
        raise ConfigError(f"node '{node.id}': kind '{node.kind.value}' is not supported by this core version") from exc
