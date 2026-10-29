"""Trace records and the tracer. Every executor writes through the tracer; sinks decide where records go."""

from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Any

from .context import current_ids

log = logging.getLogger("flow.trace")
_SENSITIVE = re.compile(r"(authorization|token|secret|password|api[_-]?key|credential|cookie)", re.I)
REDACTED = "[redacted]"


def redact(value: Any, depth: int = 0) -> Any:
    """Drop values of keys that look like credentials. Keys named like a secret never reach a sink."""
    if depth > 12:
        return "[depth]"
    if isinstance(value, dict):
        return {
            k: (REDACTED if isinstance(k, str) and _SENSITIVE.search(k) and not isinstance(v, (dict, list)) else redact(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v, depth + 1) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class TraceSink(ABC):
    """Destination for trace records."""

    @abstractmethod
    def emit(self, record: dict[str, Any]) -> None:
        """Write one record."""

    def flush(self) -> None:
        """Flush buffered records; the default does nothing."""
        return None


class MemorySink(TraceSink):
    """Sink that keeps records in a list, for tests."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def emit(self, record: dict[str, Any]) -> None:
        """Append the record."""
        self.records.append(record)

    def of(self, event: str) -> list[dict[str, Any]]:
        """Return the records of one event type."""
        return [r for r in self.records if r["event"] == event]


class JsonlSink(TraceSink):
    """Sink that appends one JSON object per line to a file."""

    def __init__(self, path: str):
        self.path = path

    def emit(self, record: dict[str, Any]) -> None:
        """Append the record as a line of JSON with sorted keys."""
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str, sort_keys=True) + "\n")


class ConsoleSink(TraceSink):
    """Sink that prints records as JSON lines to a stream, stderr by default."""

    def __init__(self, stream: Any = None, compact: bool = True):
        import sys

        self.stream, self.compact = stream or sys.stderr, compact

    def emit(self, record: dict[str, Any]) -> None:
        """Print the record, truncated to 2000 characters when compact."""
        line = json.dumps(record, default=str, sort_keys=True)
        print(line[:2000] if self.compact else line, file=self.stream)


class Tracer:
    """Fan-out to sinks with redaction. ``context`` fields (run id, export id, modified flag) go on every record."""

    def __init__(self, sinks: Iterable[TraceSink] = (), **context: Any):
        self.sinks = list(sinks)
        self.context: dict[str, Any] = dict(context)
        self.live: list[Any] = []  # callables receiving each record (the SSE stream of a run)

    def bind(self, **context: Any) -> Tracer:
        """Return a tracer with extra context fields that shares this tracer's sinks and live callbacks."""
        child = Tracer(self.sinks, **{**self.context, **context})
        child.live = self.live
        return child

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        """Redact the fields, stamp the record, and send it to every sink and live callback; sink failures are logged, not raised."""
        record = {"ts": round(time.time(), 6), "event": event, **current_ids(), **self.context, **redact(fields)}
        for sink in self.sinks:
            try:
                sink.emit(record)
            except Exception:  # a broken sink must not fail the run
                log.exception("trace sink failed")
        for cb in list(self.live):
            try:
                cb(record)
            except Exception:
                log.exception("live trace callback failed")
        return record

    def flush(self) -> None:
        """Flush every sink."""
        for sink in self.sinks:
            sink.flush()
