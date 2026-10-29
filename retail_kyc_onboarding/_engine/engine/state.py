"""Run state layout. All run data lives in session state under a few keys so a paused run survives a restart."""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from ..datatypes import Principal

VARS = "flow_vars"  # workflow variables: name -> value
RUN = "flow_run"  # run info: ids, principal, counters, pending approvals
OUTPUTS = "flow_outputs"  # node id -> last output text
TRANSCRIPT = "flow_transcript"  # progress log the supervisor reads
LAST = "flow_last"  # output of the previous node, the default input of the next one
CONVERSATION = "flow_conversation"  # what step agents read, like Kaya's message history: user turns, step tasks, step outputs


class RunState:
    """Typed access to a state mapping (ADK ctx.state, or a plain dict in tests). Always rewrites whole values so
    ADK records a state delta."""

    def __init__(self, raw: MutableMapping[str, Any]):
        self.raw = raw

    # variables
    def variables(self) -> dict[str, Any]:
        """A copy of the workflow variables."""
        return dict(self.raw.get(VARS) or {})

    def set_variable(self, name: str, value: Any) -> None:
        """Set one workflow variable."""
        data = self.variables()
        data[name] = value
        self.raw[VARS] = data

    def set_variables(self, values: dict[str, Any]) -> None:
        """Merge several workflow variables; a no-op when empty."""
        if values:
            data = self.variables()
            data.update(values)
            self.raw[VARS] = data

    # run info
    def run(self) -> dict[str, Any]:
        """A copy of the run info record."""
        return dict(self.raw.get(RUN) or {})

    def update_run(self, **fields: Any) -> None:
        """Merge fields into the run info record."""
        data = self.run()
        data.update(fields)
        self.raw[RUN] = data

    def principal(self) -> Principal:
        """The principal who requested this run."""
        return Principal.from_state(self.run().get("principal"))

    def counter(self, key: str) -> int:
        """Value of a named run counter, 0 when unset."""
        return int(self.run().get("counters", {}).get(key, 0))

    def set_counter(self, key: str, value: int) -> None:
        """Set a named run counter."""
        run = self.run()
        counters = dict(run.get("counters", {}))
        counters[key] = value
        run["counters"] = counters
        self.raw[RUN] = run

    # pending approvals / pauses
    def pending(self) -> dict[str, Any]:
        """Pending approval and pause records, keyed by id."""
        return dict(self.run().get("pending", {}))

    def set_pending(self, key: str, value: dict[str, Any] | None) -> None:
        """Store a pending record under a key, or remove it when value is None."""
        run = self.run()
        pending = dict(run.get("pending", {}))
        if value is None:
            pending.pop(key, None)
        else:
            pending[key] = value
        run["pending"] = pending
        self.raw[RUN] = run

    def set_decision(self, node_id: str, data: dict[str, Any] | None) -> None:
        """Store the authorization decision for a node, or clear it when data is None."""
        run = self.run()
        decisions = dict(run.get("decisions", {}))
        if data is None:
            decisions.pop(node_id, None)
        else:
            decisions[node_id] = data
        run["decisions"] = decisions
        self.raw[RUN] = run

    def decision(self, node_id: str) -> dict[str, Any] | None:
        """The stored authorization decision for a node, or None."""
        return self.run().get("decisions", {}).get(node_id)

    # outputs
    def set_output(self, node_id: str, text: str) -> None:
        """Record a node's output and make it the default input of the next node."""
        outs = dict(self.raw.get(OUTPUTS) or {})
        outs[node_id] = text
        self.raw[OUTPUTS] = outs
        self.raw[LAST] = text

    def add_transcript(self, node_id: str, text: str, limit: int = 30) -> None:
        """Append a node's text to the progress log, keeping only the newest entries."""
        items = list(self.raw.get(TRANSCRIPT) or [])
        items.append({"node": node_id, "text": text[:2000]})
        self.raw[TRANSCRIPT] = items[-limit:]

    def transcript(self) -> list[dict[str, str]]:
        """The progress log the supervisor reads."""
        return list(self.raw.get(TRANSCRIPT) or [])

    def add_message(self, role: str, text: str, name: str = "", limit: int = 60) -> None:
        """Append to the conversation: ``user`` for what a person typed, ``task`` for a step's task, ``step`` for its output."""
        if not text.strip():
            return
        items = list(self.raw.get(CONVERSATION) or [])
        items.append({"role": role, "name": name, "text": text[:8000]})
        self.raw[CONVERSATION] = items[-limit:]

    def conversation(self) -> list[dict[str, str]]:
        """The conversation so far, oldest first."""
        return list(self.raw.get(CONVERSATION) or [])

    def last(self) -> str:
        """Output of the previous node as text, empty when none."""
        return str(self.raw.get(LAST) or "")
