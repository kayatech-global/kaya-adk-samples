"""ADK tool wrapper: a ToolHandle seen by an LlmAgent, with authorization, approval and trace in front of it."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from ..datatypes import CallContext, Principal, ToolResult
from ..engine.state import RunState
from ..errors import PolicyDenied
from ..factories.api import ToolHandle


class KayaAdkTool(BaseTool):
    """Exposes a ToolHandle to an LlmAgent, checking policy and approval and tracing before the call."""
    def __init__(self, handle: ToolHandle, engine: Any, unit: Any, node_id: str):
        safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in handle.name)
        super().__init__(name=safe, description=handle.definition.description or handle.name)
        self.handle, self.engine, self.unit, self.node_id = handle, engine, unit, node_id

    def _get_declaration(self) -> types.FunctionDeclaration:
        js = self.handle.json_schema()
        return types.FunctionDeclaration(
            name=self.name, description=self.description, parameters_json_schema=js if js.get("properties") else None
        )

    async def run_async(self, *, args: dict[str, Any], tool_context: ToolContext) -> Any:
        """Run the tool and return a result or error dict for the model; never raises for a tool failure.

        A denied call returns an error. A call needing approval pauses through ADK's confirmation and returns ``pending_approval``. The call's idempotency key is derived from the arguments.
        """
        engine, handle = self.engine, self.handle
        state = RunState(tool_context.state)
        run = state.run()
        principal = state.principal()
        tracer = engine.tracer.bind(
            run_id=run.get("run_id"), session=run.get("session_id"), node=self.node_id, scope=self.unit.scope or None
        )
        call_id = tool_context.function_call_id or ""
        try:
            decision = await engine.guard.require(
                principal, "tool.call", {"id": handle.name, "type": handle.definition.type, "node": self.node_id}, tracer=tracer
            )
        except PolicyDenied as exc:
            tracer.emit("tool.call", tool=handle.name, args=args, ok=False, denied=True, error=str(exc))
            return {"error": f"Not allowed: {exc.reason or 'denied by policy'}"}
        confirmation = tool_context.tool_confirmation
        approval_id = hashlib.sha256(f"{run.get('session_id')}|{self.node_id}|{handle.name}|{call_id}".encode()).hexdigest()[:24]
        if confirmation is None:
            req = await engine.approvals.requirement(handle.definition, args, principal, decision, state.variables())
            if req.required:
                result = await engine.approvals.open(
                    approval_id=approval_id,
                    tool=handle.definition,
                    args=args,
                    requirement=req,
                    requester=principal,
                    node_id=self.node_id,
                    state=state,
                    tracer=tracer,
                )
                if result.answered:
                    engine.approvals.resolve(state, approval_id, result.approved, result.approver or principal, result.comment, tracer)
                    if not result.approved:
                        return {"error": "The approver rejected this action."}
                else:
                    tool_context.request_confirmation(
                        hint=f"Approve '{handle.name}'?",
                        payload={"approval_id": approval_id, "tool": handle.name, "args": args, "node": self.node_id},
                    )
                    tool_context.actions.skip_summarization = True  # the model does not answer a call that is waiting for approval
                    return {"status": "pending_approval", "approval_id": approval_id}
        else:
            info = confirmation.payload or {}
            who_p = info.get("approver") or {}
            engine.approvals.resolve(
                state,
                info.get("approval_id", approval_id),
                bool(confirmation.confirmed),
                Principal(who_p.get("subject", "unknown"), roles=tuple(who_p.get("roles", ()))),
                str(info.get("comment", "")),
                tracer,
            )
            if not confirmation.confirmed:
                who = (confirmation.payload or {}).get("approver", {}).get("subject", "approver")
                tracer.emit("tool.skipped", tool=handle.name, reason=f"rejected by {who}")
                return {"error": f"The approver ({who}) rejected this action."}
        call = CallContext(
            principal,
            str(run.get("run_id", "")),
            str(run.get("session_id", "")),
            self.node_id,
            hashlib.sha256(
                f"{run.get('session_id')}|{self.node_id}|{handle.name}|{json.dumps(args, sort_keys=True, default=str)}".encode()
            ).hexdigest()[:32],
            engine.unit.manifest.export.id,
            state.variables(),
        )
        try:
            res = await handle.invoke(dict(args), call)
        except Exception as exc:
            res = ToolResult(False, error=f"{type(exc).__name__}: {exc}")
        tracer.emit(
            "tool.call",
            tool=handle.name,
            args=args,
            ok=res.ok,
            status=res.status,
            result=res.as_text()[:4000],
            version=handle.definition.version,
            approved_by=((confirmation.payload or {}).get("approver") if confirmation else None),
        )
        if not res.ok:
            return {"error": res.as_text()}
        return {"result": res.data if res.data is not None else res.text}
