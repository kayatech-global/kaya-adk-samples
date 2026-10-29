"""W3C trace context (``traceparent``, ``tracestate``): continue the caller's trace in a run and pass it on to every outbound call.

A run started with a ``traceparent`` header becomes part of the caller's trace: ADK's spans, the engine's trace records and the
headers of tool, partner agent, approval and policy calls carry the same trace id. The carrier is kept in the run state,
so a run resumed later (an answer, an approval, a background result) continues the same trace.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import httpx
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.propagate import extract, inject

FIELDS = ("traceparent", "tracestate")
_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")
# W3C asks for at least 512 characters to be propagated; a longer tracestate is dropped, because opentelemetry-api up to 1.42.1
# (the newest google-adk 2.12 allows) parses it with a backtracking regular expression (CVE-2026-114603)
_TRACESTATE_MAX = 512


def carrier_from(headers: Mapping[str, Any] | None) -> dict[str, str]:
    """The trace context fields of ``headers`` (any case), or ``{}`` when there is no valid ``traceparent``."""
    lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    if not _TRACEPARENT.match(lowered.get("traceparent", "").strip()):
        return {}
    carrier = {k: lowered[k].strip() for k in FIELDS if lowered.get(k)}
    if len(carrier.get("tracestate", "")) > _TRACESTATE_MAX:
        del carrier["tracestate"]
    return carrier


@contextmanager
def use_carrier(carrier: Mapping[str, str] | None) -> Iterator[None]:
    """Run the block as part of the trace in ``carrier``; without one the current context is kept."""
    if not carrier:
        yield
        return
    token = otel_context.attach(extract(dict(carrier)))
    try:
        yield
    finally:
        otel_context.detach(token)


def current_ids() -> dict[str, str]:
    """``trace_id`` and ``span_id`` of the current span as hex, or ``{}`` outside a trace."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return {}
    return {"trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x")}


def outbound_headers() -> dict[str, str]:
    """``traceparent`` and ``tracestate`` for an outbound request in the current context (``{}`` outside a trace)."""
    out: dict[str, str] = {}
    if trace.get_current_span().get_span_context().is_valid:
        inject(out)
    return {k: v for k, v in out.items() if k in FIELDS}


async def _inject(request: httpx.Request) -> None:
    for key, value in outbound_headers().items():
        request.headers.setdefault(key, value)


def traced_client(**kwargs: Any) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` that adds the current trace context to every request it sends."""
    hooks = dict(kwargs.pop("event_hooks", None) or {})
    hooks["request"] = [*hooks.get("request", []), _inject]
    return httpx.AsyncClient(event_hooks=hooks, **kwargs)


class TraceContextMiddleware:
    """ASGI middleware: an HTTP request with a valid ``traceparent`` is handled inside the caller's trace.

    ADK's own server (``get_fast_api_app``, behind ``adk api_server``) continues only its Agent Engine header, so a W3C caller
    would start a new trace; with this middleware its spans, model calls and the engine's outbound calls join the caller's trace.
    """

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
        with use_carrier(carrier_from(headers)):
            await self.app(scope, receive, send)
