"""``ExecutionAgent``: the export as one ADK agent that delegates to the run host.

Purpose
    Some consumers can only embed an ADK *agent*, not an ADK workflow or an HTTP endpoint. This class is that agent. It holds no
    workflow logic of its own: every message goes through ``RunHost``, so authentication, authorization, session ownership and
    run-level tracing all apply exactly as on the HTTP surface.

Why not an ``LlmAgent`` with a tool
    That would put a model call, and its variability, in front of a deterministic workflow. This agent calls the host directly.

Behaviour per message
    * No run waiting in this ADK session: ``RunHost.start`` with the user's text.
    * A run waiting for an input answer: ``RunHost.resume`` with the text (a follow-up question, a quick reply).
    * A run waiting for an approval: never answered here. The reply names the approvals endpoint, as the A2A server does.
    * Done: one final event with the output. Metadata carries ``run_id`` and the export version.
    * Paused for input: the question is emitted together with a long-running function call event, so a consumer that exposes this agent
      over A2A reports ``input-required``. (ASSUMPTION: ADK's A2A converter maps long-running calls this way; verified in its source
      and in tests/interop with a real client, not with every ADK release.)
    * Errors become error events; nothing is raised through ADK and no token or internal address is in the text.

Principal
    ``principal_from: fixed`` (default) runs as one configured subject; refused when ``profile: production``.
    ``principal_from: state`` reads ``session.state["flow_principal"]`` (a dict, as ``Principal.to_state`` writes it), for a host that
    authenticates its callers and passes the identity along (a custom host fills ``flow_principal`` from its own identity).
    ``principal_from: user_id`` runs as the ADK session's user; refused when ``profile: production`` unless
    ``FLOW_ALLOW_USER_ID`` is true. Trust decision: the embedding platform vouches for the user identity.

Extending
    Configure with the environment variables ``FLOW_PRINCIPAL_FROM``, ``FLOW_FIXED_SUBJECT`` and ``FLOW_ALLOW_USER_ID``. Subclass and
    override ``principal_for`` for another identity source.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types
from pydantic import ConfigDict, PrivateAttr

from ..app import FlowSettings
from ..datatypes import Principal
from ..engine.host import Answer, RunHost
from ..errors import KayaAdkError

INPUT_REQUIRED_CALL = "flow_input_required"


def _text(content: types.Content | None) -> str:
    return "\n".join(p.text for p in (content.parts if content else []) or [] if getattr(p, "text", None)).strip()


class ExecutionAgent(BaseAgent):
    """An ADK agent whose every turn is a ``RunHost`` call. See the module docstring."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    principal_from: str = "fixed"
    fixed_subject: str = "adk-agent"
    allow_user_id: bool = False

    _engine: Any = PrivateAttr(default=None)
    _host: Any = PrivateAttr(default=None)
    _build: Any = PrivateAttr(default=None)  # async () -> Engine, called on the first message

    @classmethod
    def for_engine(cls, engine: Any, *, name: str = "flow_agent", description: str = "", **kw: Any) -> ExecutionAgent:
        """Build the agent around an engine that is already loaded (tests, embedding applications)."""
        agent = cls(name=name, description=description or "Runs the exported workflow", **kw)
        agent._engine, agent._host = engine, RunHost(engine)
        return agent

    @classmethod
    def for_builder(
        cls, build: Callable[[], Awaitable[Any]], *, name: str = "flow_agent", settings: FlowSettings | None = None
    ) -> ExecutionAgent:
        """Build the agent around a function that starts the engine. Nothing is built until the first message."""
        agent = cls(name=name, description="Runs the exported workflow")
        agent._build = build
        agent.apply_settings(settings or FlowSettings())
        return agent

    def apply_settings(self, settings: FlowSettings) -> None:
        """Take who a run is for from the export's settings (``FLOW_PRINCIPAL_FROM``, ``FLOW_FIXED_SUBJECT``, ``FLOW_ALLOW_USER_ID``)."""
        self.principal_from = settings.principal_from
        self.fixed_subject = settings.fixed_subject
        self.allow_user_id = settings.allow_user_id

    async def _host_for_run(self) -> RunHost:
        if self._host is None:
            if self._build is None:
                raise KayaAdkError("this agent has no engine: use ExecutionAgent.for_engine or ExecutionAgent.for_builder")
            self._engine = await self._build()
            self._host = RunHost(self._engine)
        return self._host

    def principal_for(self, ctx: InvocationContext) -> Principal:
        """Who the run is for. Override for another identity source."""
        if self.principal_from == "state":
            data = ctx.session.state.get("flow_principal")
            if not data:
                raise KayaAdkError("principal_from is 'state' but the session carries no flow_principal")
            return Principal.from_state(data)
        if self.principal_from == "user_id":
            if self._engine is not None and self._engine.settings.profile == "production" and not self.allow_user_id:
                raise KayaAdkError(
                    "principal_from 'user_id' is not allowed in production unless FLOW_ALLOW_USER_ID is true; "
                    "the embedding platform must vouch for the user identity"
                )
            user_id = ctx.session.user_id
            if not user_id:
                raise KayaAdkError("principal_from is 'user_id' but the session carries no user_id")
            return Principal(user_id)
        if self._engine is not None and self._engine.settings.profile == "production":
            raise KayaAdkError("principal_from 'fixed' is not allowed when profile is production; use 'state', 'user_id', or a custom host")
        return Principal(self.fixed_subject)

    def _event(self, ctx: InvocationContext, text: str, *, call: dict[str, Any] | None = None, meta: dict[str, Any] | None = None) -> Event:
        parts = [types.Part(text=text)]
        long_running: set[str] | None = None
        if call is not None:
            call_id = f"flow-input-{uuid.uuid4().hex[:12]}"
            parts.append(types.Part(function_call=types.FunctionCall(id=call_id, name=INPUT_REQUIRED_CALL, args=call)))
            long_running = {call_id}
        event = Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(role="model", parts=parts),
            long_running_tool_ids=long_running,
        )
        if meta:
            event.custom_metadata = meta
        return event

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        text = _text(ctx.user_content)
        session_id = ctx.session.id
        try:
            host = await self._host_for_run()
            principal = self.principal_for(ctx)
            pending = await host.pending(session_id, principal)
            if pending is not None and pending.kind == "approval":
                yield self._event(
                    ctx, "An approval is required before this can continue. An approver answers it at POST /v1/approvals/{id}, not here."
                )
                return
            if pending is not None:
                result = await host.resume(session_id=session_id, answer=Answer(text=text), answerer=principal)
            else:
                result = await host.start(message=text, principal=principal, session_id=session_id)
        except Exception as exc:  # nothing is raised through ADK; the text carries the reason but no token, key or internal address
            yield self._event(ctx, f"The request could not be completed: {type(exc).__name__}: {exc}"[:500])
            return
        m = self._engine.unit.manifest
        meta = {"run_id": result.run_id, "export_version": m.export.version}
        if result.status == "paused" and result.pause is not None:
            if result.pause.kind == "approval":
                yield self._event(
                    ctx, f"Approval is required: {result.pause.message}. An approver answers it at POST /v1/approvals/{{id}}.", meta=meta
                )
            else:
                yield self._event(ctx, result.pause.message, call={"question": result.pause.message, **result.pause.payload}, meta=meta)
        elif result.status == "error":
            yield self._event(ctx, f"The run failed: {result.error}"[:500], meta=meta)
        else:
            yield self._event(ctx, result.output, meta=meta)
