"""Expose an export as an A2A agent (protocol 0.3): an agent card and JSON-RPC ``message/send`` / ``tasks/get`` (non-streaming).

A task id and a context id are the session id. A paused run reports ``input-required``; a person's answer is the next
``message/send`` on the same context. Approvals are not answered over A2A: they need an authenticated approver (POST /v1/approvals).
Authentication and authorization failures are HTTP errors (401, 403), as the A2A specification puts them at the transport layer.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ..engine.host import Answer, HostResult
from ..errors import PolicyDenied, RunError, RunNotFound
from .routes import RunContext

log = logging.getLogger("flow.a2a")

Resolver = Callable[[Request], Awaitable[RunContext]]
STATE = {"done": "completed", "paused": "input-required", "error": "failed"}

# JSON-RPC 2.0 and A2A 0.3 error codes
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
TASK_NOT_FOUND, PUSH_NOT_SUPPORTED, UNSUPPORTED_OPERATION, CONTENT_TYPE_NOT_SUPPORTED = -32001, -32003, -32004, -32005
EXTENDED_CARD_NOT_CONFIGURED = -32007
UNSUPPORTED = {
    "tasks/cancel": (UNSUPPORTED_OPERATION, "tasks cannot be cancelled by this agent"),
    "message/stream": (UNSUPPORTED_OPERATION, "streaming is not supported; use message/send"),
    "tasks/resubscribe": (UNSUPPORTED_OPERATION, "streaming is not supported; use tasks/get"),
    "tasks/pushNotificationConfig/set": (PUSH_NOT_SUPPORTED, "push notifications are not supported"),
    "tasks/pushNotificationConfig/get": (PUSH_NOT_SUPPORTED, "push notifications are not supported"),
    "tasks/pushNotificationConfig/list": (PUSH_NOT_SUPPORTED, "push notifications are not supported"),
    "tasks/pushNotificationConfig/delete": (PUSH_NOT_SUPPORTED, "push notifications are not supported"),
    "agent/getAuthenticatedExtendedCard": (EXTENDED_CARD_NOT_CONFIGURED, "there is no extended agent card"),
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _message(text: str) -> dict[str, Any]:
    return {"kind": "message", "role": "agent", "messageId": uuid.uuid4().hex, "parts": [{"kind": "text", "text": text}]}


def _artifact(session_id: str, run_id: str | None, text: str) -> dict[str, Any]:
    """The result of one run. The id is derived from the run, so reading the same task twice returns the same artifact."""
    artifact_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"flow-result:{session_id}:{run_id or ''}"))
    return {"artifactId": artifact_id, "name": "result", "parts": [{"kind": "text", "text": text}]}


def _task(session_id: str, state: str, message: str, *, output: str | None = None, run_id: str | None = None) -> dict[str, Any]:
    status: dict[str, Any] = {"state": state, "timestamp": _now()}
    if message:
        status["message"] = _message(message)
    task: dict[str, Any] = {"kind": "task", "id": session_id, "contextId": session_id, "status": status}
    if state == "completed" and output is not None:
        task["artifacts"] = [_artifact(session_id, run_id, output)]
    return task


def _pause_text(kind: str, message: str) -> str:
    return f"Approval is required before this can continue: {message}" if kind == "approval" else message


def _result_task(res: HostResult) -> dict[str, Any]:
    state = STATE.get(res.status, "failed")
    message = _pause_text(res.pause.kind, res.pause.message) if res.pause else (res.error if res.status == "error" else "")
    return _task(res.session_id, state, message, output=res.output, run_id=res.run_id)


def _error(rpc_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _ok(rpc_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def a2a_router(resolve: Resolver, card_info: Callable[[], dict[str, Any]] | None = None) -> APIRouter:
    """Build the router that serves the agent card and the JSON-RPC endpoint.

    ``card_info`` returns the card's ``name``, ``description``, ``version`` and ``securitySchemes``; without it the card is generic.
    """
    router = APIRouter()

    def card(request: Request) -> dict[str, Any]:
        base = str(request.base_url).rstrip("/")
        info: dict[str, Any] = {"name": "workflow-agent", "description": "An exported workflow served as an A2A agent", "version": "1"}
        schemes: dict[str, Any] = {"bearer": {"type": "http", "scheme": "bearer"}}
        if card_info is not None:
            extra = dict(card_info())
            schemes = extra.pop("securitySchemes", schemes)
            info.update(extra)
        body: dict[str, Any] = {
            "protocolVersion": "0.3.0",
            **info,
            "url": f"{base}/a2a",
            "preferredTransport": "JSONRPC",
            "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": False},
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "skills": [
                {
                    "id": "run",
                    "name": "Run the workflow",
                    "description": "Send a request; the workflow handles it end to end",
                    "tags": ["workflow"],
                    "inputModes": ["text/plain"],
                    "outputModes": ["text/plain"],
                }
            ],
        }
        if schemes:
            body["securitySchemes"] = schemes
            body["security"] = [{name: []} for name in schemes]
        return body

    @router.get("/.well-known/agent-card.json", tags=["a2a"], operation_id="get_agent_card", summary="Agent card")
    async def agent_card(request: Request) -> dict[str, Any]:
        return card(request)

    @router.get("/.well-known/agent.json", tags=["a2a"], operation_id="get_agent_card_legacy", summary="Agent card (pre-0.3 path)")
    async def agent_card_legacy(request: Request) -> dict[str, Any]:
        return card(request)

    async def send(ctx: RunContext, rpc_id: Any, params: dict[str, Any], headers: Any) -> dict[str, Any]:
        msg = params.get("message")
        if not isinstance(msg, dict) or not isinstance(msg.get("parts", []), list):
            return _error(rpc_id, INVALID_PARAMS, "params.message with a list of parts is required")
        parts = [p for p in msg.get("parts", []) if isinstance(p, dict)]
        text = "\n".join(str(p.get("text", "")) for p in parts if p.get("kind", "text") == "text")
        if not text.strip():
            if parts:
                return _error(rpc_id, CONTENT_TYPE_NOT_SUPPORTED, "only text parts are supported")
            return _error(rpc_id, INVALID_PARAMS, "message needs a text part")
        task_id, context_id = msg.get("taskId"), msg.get("contextId")
        if task_id and context_id and task_id != context_id:
            return _error(rpc_id, INVALID_PARAMS, "taskId and contextId name the same task here; send the same value or only one")
        session = str(task_id or context_id or uuid.uuid4())
        pending = await ctx.host.pending(session, ctx.principal) if (task_id or context_id) else None
        if task_id and pending is None:
            # a taskId continues a task that is waiting for input; anything else is unknown or already finished
            try:
                await ctx.host.status(session, ctx.principal)
            except RunNotFound:
                return _error(rpc_id, TASK_NOT_FOUND, f"task '{session}' was not found")
            return _error(rpc_id, UNSUPPORTED_OPERATION, "the task has ended; send the message with only the contextId to start a new run")
        if pending is not None and pending.kind == "approval":
            return _error(
                rpc_id,
                UNSUPPORTED_OPERATION,
                "approval is required; an approver answers it through POST /v1/approvals/{id}, not over A2A",
            )
        if pending is not None:
            res = await ctx.host.resume(session_id=session, answer=Answer(text=text), answerer=ctx.principal, trace=headers)
        else:
            res = await ctx.host.start(message=text, principal=ctx.principal, session_id=session, trace=headers)
        return _ok(rpc_id, _result_task(res))

    async def get(ctx: RunContext, rpc_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        task_id = params.get("id")
        if not isinstance(task_id, str) or not task_id:
            return _error(rpc_id, INVALID_PARAMS, "params.id is required")
        try:
            st = await ctx.host.status(task_id, ctx.principal)
        except RunNotFound:
            return _error(rpc_id, TASK_NOT_FOUND, f"task '{task_id}' was not found")
        if st["status"] == "paused":
            pause = st.get("pause") or {}
            return _ok(rpc_id, _task(task_id, "input-required", _pause_text(pause.get("kind", ""), pause.get("message", ""))))
        if st.get("last_status") == "error":
            return _ok(rpc_id, _task(task_id, "failed", st.get("last_error") or "the run failed"))
        return _ok(rpc_id, _task(task_id, "completed", "", output=st["last_output"], run_id=st.get("run_id")))

    @router.post("/a2a", tags=["a2a"], operation_id="a2a_rpc", summary="JSON-RPC: message/send and tasks/get")
    async def rpc(request: Request, ctx: RunContext = Depends(resolve)) -> dict[str, Any]:
        try:
            body = await request.json()
        except ValueError:
            return _error(None, PARSE_ERROR, "parse error")
        if not isinstance(body, dict):
            return _error(None, INVALID_REQUEST, "the request must be a JSON-RPC object")
        rpc_id, method, params = body.get("id"), body.get("method"), body.get("params", {})
        if body.get("jsonrpc") != "2.0" or not isinstance(method, str) or "id" not in body:
            return _error(rpc_id, INVALID_REQUEST, "a JSON-RPC 2.0 request needs jsonrpc '2.0', a method and an id")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _error(rpc_id, INVALID_PARAMS, "params must be an object")
        try:
            if method == "message/send":
                return await send(ctx, rpc_id, params, request.headers)
            if method == "tasks/get":
                return await get(ctx, rpc_id, params)
        except PolicyDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except RunError as exc:
            return _error(rpc_id, INTERNAL_ERROR, str(exc))
        except Exception:
            log.exception("a2a request %s failed", method)
            return _error(rpc_id, INTERNAL_ERROR, "internal error")
        if method in UNSUPPORTED:
            code, text = UNSUPPORTED[method]
            return _error(rpc_id, code, text)
        return _error(rpc_id, METHOD_NOT_FOUND, f"method '{method}' is not supported")

    return router
