"""Run-host routes, shared by an export's own server and by the multi-export service.

The router takes a ``resolve`` dependency that returns who is calling and which engine to use, so authentication
(factory-based in an export, Keycloak in the service) stays outside the routes.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from ..datatypes import Principal
from ..engine.engine import Engine
from ..engine.host import Answer, RunHost
from ..errors import PolicyDenied, RunError, RunNotFound
from ..runtime.port import SESSION_USER
from .models import ApprovalItem, Problem, ResumeRequest, RunRequest, RunResult, RunStatus, TraceRecords
from .ring import RingBufferSink


@dataclass
class RunContext:
    """Per-request bundle: engine, run host, authenticated principal and trace ring."""

    engine: Engine
    host: RunHost
    principal: Principal
    ring: RingBufferSink


Resolver = Callable[[Request], Awaitable[RunContext]]


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, PolicyDenied):
        return HTTPException(403, str(exc))
    if isinstance(exc, RunNotFound):  # before RunError, of which it is a kind: the API documents 404 for an unknown session
        return HTTPException(404, str(exc))
    if isinstance(exc, RunError):
        return HTTPException(409, str(exc))
    raise exc


PROBLEMS: dict[int | str, dict[str, Any]] = {
    401: {
        "model": Problem,
        "description": "The authentication provider rejected the credentials.",
        "content": {"application/problem+json": {}},
    },
    403: {"model": Problem, "description": "The authorization provider denied the action.", "content": {"application/problem+json": {}}},
    404: {"model": Problem, "description": "Unknown session or approval.", "content": {"application/problem+json": {}}},
    409: {
        "model": Problem,
        "description": "The run cannot take this action in its current state.",
        "content": {"application/problem+json": {}},
    },
}


def run_router(resolve: Resolver) -> APIRouter:
    """Build the router for starting, resuming, inspecting and tracing runs and answering approvals."""
    router = APIRouter(responses=PROBLEMS)

    @router.post(
        "/runs",
        tags=["runs"],
        operation_id="start_run",
        summary="Start a run",
        response_model=RunResult,
        responses={
            200: {"content": {"text/event-stream": {}}, "description": "A result, or a server-sent event stream when `stream` is true."}
        },
    )
    async def start_run(body: RunRequest, request: Request, ctx: RunContext = Depends(resolve)) -> Any:
        run_id = str(uuid.uuid4())
        session_id = body.session_id or str(uuid.uuid4())
        wants_stream = body.stream or "text/event-stream" in request.headers.get("accept", "")

        async def work() -> Any:
            return await ctx.host.start(
                message=body.message,
                principal=ctx.principal,
                session_id=session_id,
                variables=body.variables,
                run_id=run_id,
                trace=request.headers,
            )

        if wants_stream:
            return EventSourceResponse(_stream(ctx, run_id, work))
        try:
            return (await work()).to_dict()
        except (PolicyDenied, RunError) as exc:
            raise _http(exc) from exc

    @router.post(
        "/runs/{session_id}/resume",
        tags=["runs"],
        operation_id="resume_run",
        summary="Answer a paused run",
        response_model=RunResult,
    )
    async def resume_run(session_id: str, body: ResumeRequest, request: Request, ctx: RunContext = Depends(resolve)) -> Any:
        answer = Answer(text=body.text, approved=body.approved, comment=body.comment, interrupt_id=body.interrupt_id)
        try:
            raw_state = await ctx.engine.runtime.session_state(ctx.engine.unit, session_id, SESSION_USER)
            run_id = str(((raw_state or {}).get("flow_run") or {}).get("run_id", ""))
            if "text/event-stream" in request.headers.get("accept", ""):
                return EventSourceResponse(
                    _stream(
                        ctx,
                        run_id,
                        lambda: ctx.host.resume(session_id=session_id, answer=answer, answerer=ctx.principal, trace=request.headers),
                    )
                )
            return (await ctx.host.resume(session_id=session_id, answer=answer, answerer=ctx.principal, trace=request.headers)).to_dict()
        except (PolicyDenied, RunError) as exc:
            raise _http(exc) from exc

    @router.get("/runs/{session_id}", tags=["runs"], operation_id="get_run", summary="Status of a run", response_model=RunStatus)
    async def run_status(session_id: str, ctx: RunContext = Depends(resolve)) -> Any:
        try:
            return await ctx.host.status(session_id, ctx.principal)
        except (PolicyDenied, RunError) as exc:
            raise _http(exc) from exc

    @router.get(
        "/runs/{session_id}/trace",
        tags=["runs"],
        operation_id="get_run_trace",
        summary="The recorded decisions of a run",
        response_model=TraceRecords,
    )
    async def run_trace(session_id: str, ctx: RunContext = Depends(resolve)) -> Any:
        try:
            status = await ctx.host.status(session_id, ctx.principal)
        except (PolicyDenied, RunError) as exc:
            raise _http(exc) from exc
        return {"run_id": status["run_id"], "records": ctx.ring.records(str(status["run_id"]))}

    @router.get(
        "/approvals",
        tags=["approvals"],
        operation_id="list_approvals",
        summary="Approvals this caller may answer",
        response_model=list[ApprovalItem],
    )
    async def my_approvals(ctx: RunContext = Depends(resolve)) -> Any:
        items = await ctx.host.worklist(ctx.principal)
        return [{"session_id": i.session_id, "run_id": i.run_id, "requested_by": i.requested_by, **i.pause.to_dict()} for i in items]

    @router.post(
        "/approvals/{approval_id}",
        tags=["approvals"],
        operation_id="answer_approval",
        summary="Approve or reject a tool call",
        response_model=RunResult,
    )
    async def answer_approval(approval_id: str, body: ResumeRequest, request: Request, ctx: RunContext = Depends(resolve)) -> Any:
        session_id = await ctx.host.find_approval(approval_id)
        if session_id is None:
            raise HTTPException(404, "no pending approval with this id")
        try:
            res = await ctx.host.resume(
                session_id=session_id,
                answer=Answer(approved=bool(body.approved), comment=body.comment),
                answerer=ctx.principal,
                trace=request.headers,
            )
        except (PolicyDenied, RunError) as exc:
            raise _http(exc) from exc
        return res.to_dict()

    return router


async def _stream(ctx: RunContext, run_id: str, work: Callable[[], Awaitable[Any]]) -> AsyncIterator[dict[str, str]]:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def on_record(rec: dict[str, Any]) -> None:
        if rec.get("run_id") == run_id:
            loop.call_soon_threadsafe(queue.put_nowait, rec)

    ctx.engine.tracer.live.append(on_record)
    task: asyncio.Future[Any] = asyncio.ensure_future(work())
    try:
        while True:
            if task.done() and queue.empty():
                break
            try:
                rec = await asyncio.wait_for(queue.get(), timeout=0.2)
            except TimeoutError:
                continue
            yield {"event": "trace", "data": json.dumps(rec, default=str)}
        try:
            result = task.result()
            yield {"event": "result", "data": json.dumps(result.to_dict(), default=str)}
        except (PolicyDenied, RunError) as exc:
            yield {"event": "error", "data": json.dumps({"error": str(exc)})}
    finally:
        if on_record in ctx.engine.tracer.live:
            ctx.engine.tracer.live.remove(on_record)
        if not task.done():
            task.cancel()

