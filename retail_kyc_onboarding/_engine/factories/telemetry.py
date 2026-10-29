"""telemetry: trace, metric and audit sinks."""

from __future__ import annotations

import json
from abc import abstractmethod
from typing import Any

from ..trace.records import ConsoleSink, JsonlSink, MemorySink, TraceSink
from .base import Provider, ProviderSettings
from .registry import register


class TelemetryFactory(Provider):
    """Factory kind that supplies the trace sinks."""

    kind = "telemetry"

    @abstractmethod
    def sinks(self) -> list[TraceSink]:
        """Return the sinks that every trace record is written to."""


@register
class ConsoleTelemetry(TelemetryFactory):
    """Prints trace records to the console."""

    name = "console"

    class Settings(ProviderSettings):
        compact: bool = True

    def sinks(self) -> list[TraceSink]:
        """Return a console sink."""
        return [ConsoleSink(compact=self.settings.compact)]


@register
class JsonlTelemetry(TelemetryFactory):
    """Writes trace records to a JSON Lines file."""

    name = "jsonl"

    class Settings(ProviderSettings):
        path: str = "trace.jsonl"

    def sinks(self) -> list[TraceSink]:
        """Return a JSONL sink for ``path``."""
        return [JsonlSink(self.settings.path)]


@register
class MemoryTelemetry(TelemetryFactory):
    """Keeps trace records in memory (``sink``), for tests."""

    name = "memory"

    def __init__(self, settings=None, ctx=None):
        super().__init__(settings, ctx)
        self.sink = MemorySink()

    def sinks(self) -> list[TraceSink]:
        """Return the in-memory sink."""
        return [self.sink]


_CONTENT_FIELDS = {"prompt", "system", "input", "output", "result", "text", "args", "reply", "message"}


class OtlpSink(TraceSink):
    """Each trace record becomes one short OpenTelemetry span ``flow.<event>`` with flat attributes.

    ``capture_content`` false (default) drops prompt, input, output and tool argument text: only ids, routes, statuses
    and token counts leave the process. ADK's own spans (invoke_workflow, invoke_node, model calls) flow through the same provider.
    """

    def __init__(self, tracer: Any, capture_content: bool):
        self.tracer, self.capture_content = tracer, capture_content

    def emit(self, record: dict[str, Any]) -> None:
        """Emit one span for ``record``; content fields are dropped unless ``capture_content`` is set."""
        attrs: dict[str, Any] = {}
        for key, value in record.items():
            if key in {"ts", "event"}:
                continue
            if not self.capture_content and key in _CONTENT_FIELDS:
                continue
            attrs[f"flow.{key}"] = value if isinstance(value, (str, int, float, bool)) else json.dumps(value, default=str)[:2000]
        span = self.tracer.start_span(f"flow.{record['event']}", attributes=attrs)
        span.end()


@register
class OtlpTelemetry(TelemetryFactory):
    """Exports trace records, and ADK's own spans, over OTLP/HTTP."""

    name = "otlp"

    class Settings(ProviderSettings):
        endpoint: str = ""  # e.g. https://collector:4318/v1/traces (OTLP over HTTP)
        headers: dict[str, str] = {}
        service_name: str = "flow-adk-export"
        capture_content: bool = False
        set_global: bool = True  # also route ADK's own spans through this provider

    exporter: Any = None  # tests inject an in-memory exporter

    def sinks(self) -> list[TraceSink]:
        """Install a tracer provider (as the global one when ``set_global``) and return a sink writing to it; an injected exporter replaces the network one."""
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

        if self.exporter is not None:
            processor: Any = SimpleSpanProcessor(self.exporter)
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=self.settings.endpoint or None, headers=self.settings.headers or None))
        current = trace.get_tracer_provider()
        if self.settings.set_global and isinstance(current, TracerProvider):
            # ADK's server (adk web, api_server, server.py) installs the global provider first and OpenTelemetry keeps the first one
            provider = current
        else:
            provider = TracerProvider(resource=Resource.create({"service.name": self.settings.service_name}))
            if self.settings.set_global:
                trace.set_tracer_provider(provider)
        provider.add_span_processor(processor)
        return [OtlpSink(provider.get_tracer("flow"), self.settings.capture_content)]
