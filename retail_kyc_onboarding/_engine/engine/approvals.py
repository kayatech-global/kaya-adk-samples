"""Approval service: ties the three approval factories to the guard, the run state and the trace."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from ..datatypes import ApprovalRequirement, Decision, Principal, ToolDef
from ..factories.approval import ApprovalChannel, ApprovalPolicy, ApproverResolver, ChannelResult
from ..trace import Tracer, redact
from .guard import Guard
from .state import RunState


class ApprovalService:
    """Opens approval requests and checks who may answer them, via the policy, resolver, channel and guard."""
    def __init__(self, policy: ApprovalPolicy, resolver: ApproverResolver, channel: ApprovalChannel, guard: Guard):
        self.policy, self.resolver, self.channel, self.guard = policy, resolver, channel, guard

    async def requirement(
        self, tool: ToolDef, args: Mapping[str, Any], principal: Principal, decision: Decision | None, variables: Mapping[str, Any]
    ) -> ApprovalRequirement:
        """Ask the approval policy whether this tool call needs approval, and from whom."""
        return await self.policy.evaluate(tool, args, principal, decision, variables)

    async def open(
        self,
        *,
        approval_id: str,
        tool: ToolDef,
        args: Mapping[str, Any],
        requirement: ApprovalRequirement,
        requester: Principal,
        node_id: str,
        state: RunState,
        tracer: Tracer,
    ) -> ChannelResult:
        """Record a pending approval in run state, trace it and send it to the channel.

        The summary sent out has its arguments redacted.
        """
        summary = {
            "tool": tool.name,
            "args": redact(dict(args)),
            "reason": requirement.reason,
            "requested_by": requester.subject,
            "node": node_id,
        }
        expires = time.time() + requirement.timeout_seconds if requirement.timeout_seconds else None
        state.set_pending(
            approval_id,
            {
                "approval_id": approval_id,
                "tool": tool.name,
                "node": node_id,
                "requirement": asdict(requirement),
                "requester": requester.to_state(),
                "expires_at": expires,
                "summary": summary,
            },
        )
        tracer.emit(
            "approval.requested",
            approval_id=approval_id,
            tool=tool.name,
            node=node_id,
            approvers=dict(requirement.approvers),
            reason=requirement.reason,
        )
        return await self.channel.request(approval_id, summary, requirement)

    @staticmethod
    def requirement_of(record: Mapping[str, Any]) -> ApprovalRequirement:
        """Rebuild the ApprovalRequirement stored in a pending-approval record."""
        r = record["requirement"]
        return ApprovalRequirement(
            bool(r["required"]),
            r.get("approvers") or {},
            r.get("rules") or {},
            r.get("timeout_seconds"),
            r.get("on_timeout", "deny"),
            r.get("reason", ""),
        )

    async def verify_answer(self, record: Mapping[str, Any], answerer: Principal, tracer: Tracer) -> tuple[bool, str]:
        """Is this principal allowed to answer? Checks expiry, the approver specification and authorization."""
        if record.get("expires_at") and time.time() > float(record["expires_at"]):
            return False, "approval request expired"
        requirement = self.requirement_of(record)
        requester = Principal.from_state(record.get("requester"))
        ok, why = await self.resolver.may_answer(requirement, answerer, requester)
        if not ok:
            tracer.emit("approval.rejected_answerer", approval_id=record["approval_id"], answerer=answerer.public(), reason=why)
            return False, why
        decision = await self.guard.check(
            answerer, "approval.answer", {"id": record["tool"], "approval_id": record["approval_id"]}, tracer=tracer
        )
        if decision.effect == "deny":
            return False, decision.reason or "not authorized to answer approvals"
        return True, why

    def resolve(self, state: RunState, approval_id: str, approved: bool, approver: Principal, comment: str, tracer: Tracer) -> None:
        """Clear the pending record for an approval and trace the answer."""
        state.set_pending(approval_id, None)
        tracer.emit("approval.answered", approval_id=approval_id, approved=approved, approver=approver.public(), comment=comment)
