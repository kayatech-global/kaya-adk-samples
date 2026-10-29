"""Minimal A2A (agent-to-agent) client: JSON-RPC ``message/send`` and ``tasks/get`` over HTTP. Non-streaming."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..errors import ToolError
from ..trace.context import traced_client

FINAL_STATES = {"completed", "failed", "canceled", "rejected"}


@dataclass
class A2aReply:
    """Normalised outcome of an A2A call: task ``state``, reply ``text``, task and context ids, and the ``raw`` result."""
    state: str  # completed | input-required | failed | canceled | rejected | working | auth-required
    text: str = ""
    task_id: str | None = None
    context_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _parts_text(parts: list[dict[str, Any]] | None) -> str:
    return "\n".join(str(p.get("text", "")) for p in (parts or []) if p.get("kind", "text") == "text" and p.get("text"))


def parse_result(result: dict[str, Any]) -> A2aReply:
    """Convert a JSON-RPC ``result`` (a message or a task) into an A2aReply.

    A bare message counts as completed. For a task, the text comes from the artifacts when completed, else from the status message, else from the last agent message in the history.
    """
    if result.get("kind") == "message" or ("parts" in result and "status" not in result):
        return A2aReply("completed", _parts_text(result.get("parts")), None, result.get("contextId"), result)
    status = result.get("status") or {}
    state = str(status.get("state", "working")).lower()
    text = ""
    if state == "completed":
        text = "\n".join(_parts_text(a.get("parts")) for a in (result.get("artifacts") or []))
    if not text:
        text = _parts_text((status.get("message") or {}).get("parts"))
    if not text:
        for m in reversed(result.get("history") or []):
            if m.get("role") == "agent":
                text = _parts_text(m.get("parts"))
                break
    return A2aReply(state, text.strip(), result.get("id"), result.get("contextId"), result)


class A2aClient:
    """Client for one A2A agent endpoint. Each call opens a short-lived HTTP client, so nothing is kept between calls."""
    def __init__(
        self,
        url: str,
        headers: dict[str, str],
        *,
        timeout: float = 30,
        poll_interval: float = 1.0,
        poll_attempts: int = 10,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.url, self.headers, self.timeout = url, headers, timeout
        self.poll_interval, self.poll_attempts, self.transport = poll_interval, poll_attempts, transport

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params}
        try:
            async with traced_client(timeout=self.timeout, transport=self.transport) as client:
                resp = await client.post(self.url, json=body, headers={"Content-Type": "application/json", **self.headers})
        except httpx.HTTPError as exc:
            raise ToolError(f"A2A call failed: {type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400:
            raise ToolError(f"A2A agent returned HTTP {resp.status_code}")
        data = resp.json()
        if "error" in data:
            raise ToolError(f"A2A error {data['error'].get('code')}: {str(data['error'].get('message', ''))[:300]}")
        return dict(data.get("result") or {})

    async def send(
        self, text: str, *, context_id: str | None = None, task_id: str | None = None, metadata: dict[str, Any] | None = None
    ) -> A2aReply:
        """Send a user message and poll ``tasks/get`` while the task is working or submitted.

        Args:
            text: The user message.
            context_id: Continue an existing conversation.
            task_id: Continue an existing task, for example one waiting for input.
            metadata: Passed through in the request params.

        Returns:
            The first reply that is not working or submitted, including input-required.

        Raises:
            ToolError: On a transport failure, an HTTP error status, a JSON-RPC error, or when the task is still unfinished after ``poll_attempts`` polls.
        """
        message: dict[str, Any] = {"role": "user", "parts": [{"kind": "text", "text": text}], "messageId": uuid.uuid4().hex}
        if context_id:
            message["contextId"] = context_id
        if task_id:
            message["taskId"] = task_id
        params: dict[str, Any] = {"message": message}
        if metadata:
            params["metadata"] = metadata
        reply = parse_result(await self._rpc("message/send", params))
        polls = 0
        while reply.state in {"working", "submitted"} and reply.task_id and polls < self.poll_attempts:
            await asyncio.sleep(self.poll_interval)
            reply = parse_result(await self._rpc("tasks/get", {"id": reply.task_id}))
            polls += 1
        if reply.state in {"working", "submitted"}:
            raise ToolError(f"the A2A agent did not finish within {self.poll_attempts} polls")
        return reply
