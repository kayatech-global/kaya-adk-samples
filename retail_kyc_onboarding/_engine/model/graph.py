"""Version-neutral graph model. Plans and IFLOWs both become a Graph; runtime adapters turn it into ADK objects."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

START = "__START__"
END = "__END__"


class NodeKind(StrEnum):
    """The kinds of node a graph can contain."""

    AGENT = "agent"
    TOOL = "tool"
    SUPERVISOR = "supervisor"
    BRANCH = "branch"
    FOLLOWUP = "followup"
    SUBFLOW = "subflow"
    ITERATION = "iteration"
    EXTERNAL_AGENT = "external_agent"


class Policy(BaseModel):
    """Per-node execution policy: retry attempts and timeout."""

    model_config = ConfigDict(extra="forbid")
    retry_attempts: int = 0
    timeout_seconds: float | None = None


class Edge(BaseModel):
    """A directed link between two nodes, optionally labelled or guarded by a condition."""

    model_config = ConfigDict(extra="forbid")
    source: str
    target: str
    label: str | None = None
    condition: str | None = None

    @property
    def conditional(self) -> bool:
        """True when the edge carries a condition."""
        return bool(self.condition)


class Node(BaseModel):
    """A graph node: its kind, kind-specific config, policy and routing mode."""

    model_config = ConfigDict(extra="forbid")
    id: str
    kind: NodeKind
    name: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    policy: Policy = Field(default_factory=Policy)
    # "edges": canvas order (first true conditional edge, else first plain edge)
    # "label": plan order (a condition function returns an edge label)
    routing: Literal["edges", "label"] = "edges"
    condition_id: str | None = None  # the condition that picks the edge label when routing is "label"
    source_id: str | None = None  # the source node id this came from, for traceability

    @property
    def title(self) -> str:
        """The node name, or its id when unnamed."""
        return self.name or self.id


class VariableSpec(BaseModel):
    """A declared workflow variable with its type, default and description."""

    model_config = ConfigDict(extra="forbid")
    name: str
    type: str = "string"
    default: Any = None
    description: str = ""


class Graph(BaseModel):
    """A validated workflow graph: unique node ids, edges that point at known nodes, and at least one start edge."""

    model_config = ConfigDict(extra="forbid")
    id: str
    name: str = ""
    version: str = ""
    source_kind: Literal["iflow", "plan"] = "iflow"
    nodes: list[Node]
    edges: list[Edge]
    variables: list[VariableSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> Graph:
        ids = [n.id for n in self.nodes]
        if len(set(ids)) != len(ids):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"duplicate node ids: {dupes}")
        known = set(ids)
        for e in self.edges:
            if e.source != START and e.source not in known:
                raise ValueError(f"edge source '{e.source}' is not a node")
            if e.target != END and e.target not in known:
                raise ValueError(f"edge target '{e.target}' is not a node")
        if not any(e.source == START for e in self.edges):
            raise ValueError("graph has no edge from __START__")
        return self

    def node(self, node_id: str) -> Node:
        """Return the node with this id, raising ``KeyError`` if there is none."""
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(node_id)

    def outgoing(self, node_id: str) -> list[Edge]:
        """Return the edges that leave a node, in declaration order."""
        return [e for e in self.edges if e.source == node_id]

    def start_targets(self) -> list[str]:
        """Return the ids of the nodes the start marker points at."""
        return [e.target for e in self.edges if e.source == START]
