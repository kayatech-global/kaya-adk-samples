"""Version-neutral execution plan of one node: a chain of phases. Runtime adapters turn phases into ADK objects.

Phases (not whole nodes) are the unit because ADK resumes a paused node with the reply as the input of the next node.
A phase that needs a human returns a Pause; the adapter adds a wait node, and the reply flows into the phase named by
``NodePlan.pauses``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from ..model import Node, NodeKind

END_ROUTE = "__end__"


@dataclass
class Pause:
    """A request for a human reply that suspends the node until it arrives."""
    kind: Literal["input", "approval"]
    interrupt_id: str
    message: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class PhaseResult:
    """What a phase returns: its output, an optional route to take, or a pause."""
    output: Any = None
    route: str | None = None  # edge key for the last phase ("e0", "e1", "__end__"), or a loop label
    pause: Pause | None = None


@dataclass
class AgentSpec:
    """What an agent phase needs from the runtime. instruction() renders the system prompt from live run state."""

    name: str
    model_ref: str
    instruction: Callable[[Any], Awaitable[str]]
    tools: list[Any] = field(default_factory=list)  # ToolHandle objects
    output_schema: dict[str, Any] | None = None  # field spec, see engine.schemas
    lenient: bool = False  # ask for JSON but parse it ourselves: a malformed answer is handled by the node, not a crashed run
    scope: str = ""


PhaseFn = Callable[[Any], Awaitable[PhaseResult]]


@dataclass
class Phase:
    """One step of a node plan: a plain function or an agent call."""
    name: str
    fn: PhaseFn | None = None
    agent: AgentSpec | None = None
    exits: bool = False  # a side phase that ends the node and routes onward like the last phase


@dataclass
class NodePlan:
    """Ordered phases of one node, with the pause, loop and side-phase wiring the adapters need."""
    node: Node
    phases: list[Phase]
    pauses: dict[str, str] = field(default_factory=dict)  # phase name -> phase that receives the reply
    loops: dict[str, dict[str, str]] = field(default_factory=dict)  # phase name -> {route label: phase name}
    side: list[Phase] = field(default_factory=list)  # phases reached only by a route (e.g. halt), not chained

    @property
    def kind(self) -> NodeKind:
        """The kind of the underlying node."""
        return self.node.kind

    @property
    def entry(self) -> str:
        """Name of the first phase."""
        return self.phases[0].name

    @property
    def exit(self) -> str:
        """Name of the last chained phase."""
        return self.phases[-1].name

    def phase_names(self) -> list[str]:
        """Names of the chained phases, in order."""
        return [p.name for p in self.phases]
