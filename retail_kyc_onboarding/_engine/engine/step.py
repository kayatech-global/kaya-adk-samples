"""What a phase function sees: one node execution, independent of the ADK version."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..datatypes import CallContext, Principal
from ..model import Node
from ..trace import Tracer
from .evaluator import Scope
from .state import RunState
from .units import ContentUnit

if TYPE_CHECKING:
    from .engine import Engine


@dataclass
class StepCtx:
    """Everything a phase function sees for one node execution, independent of the ADK version."""
    engine: Engine
    unit: ContentUnit
    node: Node
    state: RunState
    node_input: Any
    tracer: Tracer
    reply: Any = None  # set when this phase receives the answer to a pause
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def principal(self) -> Principal:
        """The principal who requested the run."""
        return self.state.principal()

    @property
    def session_id(self) -> str:
        """Id of the session this run belongs to."""
        return str(self.state.run().get("session_id", ""))

    def variables(self) -> dict[str, Any]:
        """A copy of the workflow variables."""
        return self.state.variables()

    def scope(self) -> Scope:
        """Expression scope built from the current variables."""
        return Scope(variables=self.variables(), custom_state={})

    def call(self, extra_key: str = "") -> CallContext:
        """Build the CallContext for an outbound call.

        The idempotency key is stable for the same session, node, extra_key and run counter, so a retry of the same call repeats it.
        """
        run = self.state.run()
        key = hashlib.sha256(f"{self.session_id}|{self.node.id}|{extra_key}|{run.get('run_counter', 0)}".encode()).hexdigest()[:32]
        return CallContext(
            self.principal,
            str(run.get("run_id", "")),
            self.session_id,
            self.node.id,
            key,
            self.engine.unit.manifest.export.id,
            self.variables(),
        )

    def input_text(self) -> str:
        """The node input rendered as text."""
        from .render import stringify

        return stringify(self.node_input)
