"""In-memory trace ring: the most recent records per run, for GET .../trace. Durable sinks come from the telemetry factory."""

from __future__ import annotations

from collections import OrderedDict, deque
from typing import Any

from ..trace import TraceSink


class RingBufferSink(TraceSink):
    """A bounded in-memory sink that keeps the latest records of the most recent runs."""

    def __init__(self, per_run: int = 1000, runs: int = 200):
        self.per_run, self.runs = per_run, runs
        self.data: OrderedDict[str, deque[dict[str, Any]]] = OrderedDict()

    def emit(self, record: dict[str, Any]) -> None:
        """Store a record under its run id, evicting the oldest run beyond the cap; records without a run id are ignored."""
        run = record.get("run_id")
        if not run:
            return
        buf = self.data.get(run)
        if buf is None:
            buf = self.data[run] = deque(maxlen=self.per_run)
            while len(self.data) > self.runs:
                self.data.popitem(last=False)
        else:
            self.data.move_to_end(run)
        buf.append(record)

    def records(self, run_id: str) -> list[dict[str, Any]]:
        """Return the stored records of a run, oldest first, or an empty list when unknown."""
        return list(self.data.get(run_id, ()))
