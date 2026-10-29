"""Trace plugin: records what ADK sends to and receives from the model, tied back to the graph node."""

from __future__ import annotations

from typing import Any

from google.adk.plugins.base_plugin import BasePlugin

from ..engine.state import RunState


def _last_user_text(llm_request: Any) -> str:
    for content in reversed(llm_request.contents or []):
        if content.role == "user":
            texts = [p.text for p in (content.parts or []) if getattr(p, "text", None)]
            if texts:
                return "\n".join(texts)
    return ""


def _system_text(llm_request: Any) -> str:
    si = getattr(llm_request.config, "system_instruction", None)
    if si is None:
        return ""
    if isinstance(si, str):
        return si
    return "\n".join(getattr(p, "text", "") or "" for p in (getattr(si, "parts", None) or []))


class KayaTracePlugin(BasePlugin):
    """ADK plugin that traces each model call, result and error against the graph node that made it."""
    def __init__(self, engine: Any):
        super().__init__(name="flow_trace")
        self.engine = engine

    def _tracer(self, callback_context: Any) -> Any:
        run = RunState(callback_context.state).run()
        entry = self.engine.agent_index.get(callback_context.agent_name)
        node_id = entry[1].id if entry else callback_context.agent_name
        scope = entry[0].scope if entry else ""
        return self.engine.tracer.bind(run_id=run.get("run_id"), session=run.get("session_id"), node=node_id, scope=scope or None), entry

    async def before_model_callback(self, *, callback_context: Any, llm_request: Any) -> Any:
        """Authorize the model call for its node, then trace the request.

        Raises:
            PolicyDenied: The guard denies ``model.call`` for the node.
        """
        tracer, entry = self._tracer(callback_context)
        principal = RunState(callback_context.state).principal()
        if entry:
            await self.engine.guard.require(
                principal, "model.call", {"id": entry[1].config.get("model") or entry[1].id, "node": entry[1].id}, tracer=tracer
            )
        tracer.emit(
            "model.call",
            model=llm_request.model,
            system=_system_text(llm_request)[:6000],
            input=_last_user_text(llm_request)[:6000],
            tools=[d.name for t in (llm_request.config.tools or []) for d in (getattr(t, "function_declarations", None) or [])]
            if llm_request.config
            else [],
            structured=bool(getattr(llm_request.config, "response_schema", None)),
        )
        return None

    async def after_model_callback(self, *, callback_context: Any, llm_response: Any) -> Any:
        """Trace the model's text, tool calls and token usage."""
        tracer, _ = self._tracer(callback_context)
        text, calls = "", []
        for p in (llm_response.content.parts if llm_response.content else []) or []:
            if getattr(p, "text", None):
                text += p.text
            if getattr(p, "function_call", None):
                calls.append({"name": p.function_call.name, "args": dict(p.function_call.args or {})})
        usage = llm_response.usage_metadata
        tracer.emit(
            "model.result",
            text=text[:6000],
            tool_calls=calls,
            error=getattr(llm_response, "error_message", None),
            tokens={
                "prompt": getattr(usage, "prompt_token_count", None),
                "completion": getattr(usage, "candidates_token_count", None),
                "total": getattr(usage, "total_token_count", None),
            }
            if usage
            else None,
        )
        return None

    async def on_model_error_callback(self, *, callback_context: Any, llm_request: Any, error: Exception) -> Any:
        """Trace a model failure; the error itself continues to propagate."""
        tracer, _ = self._tracer(callback_context)
        tracer.emit("model.error", error=f"{type(error).__name__}: {error}")
        return None
