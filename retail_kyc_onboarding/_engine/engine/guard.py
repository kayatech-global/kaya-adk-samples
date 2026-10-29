"""The single enforcement point: every step, tool call, child run and route choice is authorized here and
audited. Executors call the guard; they never call the authorizer directly."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..datatypes import Decision, Principal
from ..errors import PolicyDenied
from ..factories.security import Authorizer
from ..trace import Tracer

ACTIONS = ("run.start", "node.enter", "model.call", "tool.call", "subflow.run", "a2a.call", "route.choose", "approval.answer")


class Guard:
    """Authorizes actions and traces every decision, failing closed by default when the authorizer errors."""
    def __init__(self, authz: Authorizer, tracer: Tracer, fail_closed: bool = True):
        self.authz, self.tracer, self.fail_closed = authz, tracer, fail_closed

    async def check(
        self,
        principal: Principal,
        action: str,
        resource: Mapping[str, Any],
        context: Mapping[str, Any] | None = None,
        tracer: Tracer | None = None,
    ) -> Decision:
        """Ask the authorizer and trace the decision; never raises.

        If the authorizer fails, the result is deny (fail closed) or allow (fail open) per the guard's mode.
        """
        try:
            decision = await self.authz.check(principal, action, resource, context or {})
        except Exception as exc:  # the policy service failed: obey the failure mode, never silently allow
            reason = f"authorization unavailable: {type(exc).__name__}"
            decision = Decision("deny", reason) if self.fail_closed else Decision("allow", reason + ", fail_open")
        (tracer or self.tracer).emit(
            "authz", action=action, resource=dict(resource), principal=principal.public(), effect=decision.effect, reason=decision.reason
        )
        return decision

    async def require(
        self,
        principal: Principal,
        action: str,
        resource: Mapping[str, Any],
        context: Mapping[str, Any] | None = None,
        tracer: Tracer | None = None,
    ) -> Decision:
        """Raises PolicyDenied on deny. Returns the decision so callers can see require_approval."""
        decision = await self.check(principal, action, resource, context, tracer)
        if decision.effect == "deny":
            raise PolicyDenied(action, str(resource.get("id", resource.get("name", ""))), decision.reason)
        return decision
