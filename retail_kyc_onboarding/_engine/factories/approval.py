"""Tool approval as three factories (artifact 17.7): policy (needed?), approver resolver (who?), channel (how?)."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from ..datatypes import ApprovalRequirement, Decision, Principal, ToolDef
from ..trace.context import traced_client
from .base import Provider, ProviderSettings
from .registry import register


class ApprovalPolicy(Provider):
    """Port of the ``approval_policy`` kind: decides whether a tool call needs approval."""
    kind = "approval_policy"

    @abstractmethod
    async def evaluate(
        self, tool: ToolDef, args: Mapping[str, Any], principal: Principal, decision: Decision | None, variables: Mapping[str, Any]
    ) -> ApprovalRequirement:
        """Decide whether a call to ``tool`` needs approval.

        Args:
            tool: The tool being called, including its own approval settings.
            args: The call arguments.
            principal: The caller.
            decision: The authz decision for this call, if one was made.
            variables: Run variables, available to rule expressions.

        Returns:
            An ApprovalRequirement; ``required`` False lets the call proceed.
        """


def _requirement(settings: Mapping[str, Any], reason: str) -> ApprovalRequirement:
    return ApprovalRequirement(
        True,
        dict(settings.get("approvers") or {}),
        dict(settings.get("rules") or {}),
        settings.get("timeout_seconds"),
        str(settings.get("on_timeout", "deny")),
        reason,
    )


@register
class NeverPolicy(ApprovalPolicy):
    """No tool needs approval, whatever its settings say. Development and tests."""

    name = "never"

    async def evaluate(self, tool, args, principal, decision, variables):
        """Return a requirement that never asks for approval."""
        return ApprovalRequirement(False, reason="policy never")


@register
class AlwaysPolicy(ApprovalPolicy):
    """Every tool call needs approval from any authenticated approver unless the tool names approvers."""

    name = "always"

    async def evaluate(self, tool, args, principal, decision, variables):
        """Require approval, using the tool's own approval settings or any authenticated approver when it has none."""
        return _requirement(tool.approval or {"approvers": {"kind": "any_authenticated"}}, "policy always")


@register
class ByToolPolicy(ApprovalPolicy):
    """Follow each tool's own settings: policy never | always | by_tool | by_rule | from_authz (default never)."""

    name = "by_tool"

    async def evaluate(self, tool, args, principal, decision, variables):
        """Resolve the tool's ``approval.policy`` mode into a requirement.

        ``from_authz`` requires approval only when the authz decision was require_approval. ``by_rule`` evaluates ``rules.when`` over run variables, ``arg.*`` and ``args_*`` names, and the principal.
        An unknown mode does not require approval; the reason says so.
        """
        cfg = dict(tool.approval or {})
        mode = str(cfg.get("policy", "by_tool" if cfg.get("required") else "never"))
        if mode == "never":
            return ApprovalRequirement(False, reason="tool policy never")
        if mode == "always" or (mode == "by_tool" and cfg.get("required", True)):
            return _requirement(cfg, f"tool policy {mode}")
        if mode == "by_tool":
            return ApprovalRequirement(False, reason="tool policy by_tool, not required")
        if mode == "from_authz":
            if decision is not None and decision.effect == "require_approval":
                merged = {**cfg, "approvers": dict(decision.approvers or cfg.get("approvers") or {})}
                return _requirement(merged, "authz require_approval")
            return ApprovalRequirement(False, reason="authz did not require approval")
        if mode == "by_rule":
            from ..engine.evaluator import evaluate_condition

            expr = (cfg.get("rules") or {}).get("when", "")
            scope = {
                **dict(variables),
                **{f"arg.{k}": v for k, v in args.items()},
                **{f"args_{k}": v for k, v in args.items()},
                "principal_subject": principal.subject,
                "principal_roles": list(principal.roles),
            }
            if expr and evaluate_condition(expr, scope, mode="strict").value:
                return _requirement(cfg, "tool rule matched")
            return ApprovalRequirement(False, reason="tool rule did not match")
        return ApprovalRequirement(False, reason=f"unknown tool policy '{mode}'")


class ApproverResolver(Provider):
    """Port of the ``approver_resolver`` kind: decides who may answer an approval request."""
    kind = "approver_resolver"

    @abstractmethod
    async def may_answer(self, requirement: ApprovalRequirement, answerer: Principal, requester: Principal) -> tuple[bool, str]:
        """Return ``(allowed, reason)`` for ``answerer`` answering a request raised by ``requester``.

        The reason is a short human-readable explanation of the outcome.
        """


@register
class StandardApprovers(ApproverResolver):
    """approvers: {kind: any_authenticated|roles|users|groups|external_system, any_of: [...]}; rules.requester_not_allowed."""

    name = "standard"

    class Settings(ProviderSettings):
        external_url: str = ""
        timeout: float = 5

    transport: httpx.AsyncBaseTransport | None = None

    async def may_answer(self, requirement, answerer, requester):
        """Check ``answerer`` against the requirement's approver spec and its requester-not-allowed rule.

        An anonymous answerer passes ``any_authenticated`` only under the development profile. ``external_system`` posts to a URL and trusts the ``allowed`` flag in its reply.
        """
        spec = dict(requirement.approvers or {"kind": "any_authenticated"})
        if requirement.rules.get("requester_not_allowed") and answerer.subject == requester.subject:
            return False, "requester may not approve their own request"
        kind = spec.get("kind", "any_authenticated")
        any_of = set(spec.get("any_of") or [])
        if kind == "any_authenticated":
            return (answerer.subject != "anonymous" or self.ctx.profile == "development"), "any authenticated"
        if kind == "roles":
            return bool(any_of & set(answerer.roles)), f"needs one of roles {sorted(any_of)}"
        if kind == "users":
            return answerer.subject in any_of, f"needs one of users {sorted(any_of)}"
        if kind == "groups":
            groups = set(answerer.claims.get("groups", []))
            return bool(any_of & groups), f"needs one of groups {sorted(any_of)}"
        if kind == "external_system":
            url = spec.get("url") or self.settings.external_url
            if not url:
                return False, "external_system has no url"
            try:
                async with traced_client(timeout=self.settings.timeout, transport=self.transport) as client:
                    resp = await client.post(url, json={"answerer": answerer.public(), "requester": requester.public(), "spec": spec})
                allowed = resp.status_code < 300 and resp.json().get("allowed") is True
            except (httpx.HTTPError, ValueError, AttributeError) as exc:  # unreachable or an unreadable reply: refused
                return False, f"external system unavailable ({type(exc).__name__})"
            return allowed, "external system decision"
        return False, f"unknown approver kind '{kind}'"


@dataclass
class ChannelResult:
    """Outcome of delivering an approval request.

    ``answered`` is True only when the channel decided at once; ``approved``, ``approver`` and ``comment`` then carry the decision.
    """
    answered: bool = False  # True: the channel answered at once (no pause)
    approved: bool = False
    approver: Principal | None = None
    comment: str = ""


class ApprovalChannel(Provider):
    """Port of the ``approval_channel`` kind: delivers an approval request to approvers."""
    kind = "approval_channel"

    @abstractmethod
    async def request(self, approval_id: str, summary: Mapping[str, Any], requirement: ApprovalRequirement) -> ChannelResult:
        """Deliver the request. A channel that cannot answer at once returns answered=False; the run then pauses."""


@register
class AdkRequestInputChannel(ApprovalChannel):
    """The run pauses in the session; the caller of the run API answers with POST /runs/{session}/resume."""

    name = "adk_request_input"

    async def request(self, approval_id, summary, requirement):
        """Do nothing and return unanswered, so the run pauses in the session."""
        return ChannelResult(answered=False)


@register
class HttpCallbackChannel(ApprovalChannel):
    """POST the request to an approval service; it answers later through POST /approvals/{id} on the run host."""

    name = "http_callback"

    class Settings(ProviderSettings):
        url: str
        headers: dict[str, str] = {}
        token_secret: str = ""
        timeout: float = 10

    transport: httpx.AsyncBaseTransport | None = None

    async def request(self, approval_id, summary, requirement):
        """POST the request to the approval service and return unanswered.

        Raises httpx.HTTPStatusError when the service replies with an error status.
        """
        headers = dict(self.settings.headers)
        if self.settings.token_secret:
            headers["Authorization"] = "Bearer " + await self.ctx.provider("secret").get(self.settings.token_secret)
        async with traced_client(timeout=self.settings.timeout, transport=self.transport) as client:
            resp = await client.post(
                self.settings.url,
                headers=headers,
                json={"approval_id": approval_id, "summary": dict(summary), "approvers": dict(requirement.approvers)},
            )
        resp.raise_for_status()
        return ChannelResult(answered=False)


@register
class QueueChannel(ApprovalChannel):
    """Collects requests in memory: tests, and embedding applications that poll ``pending``."""

    name = "queue"

    def __init__(self, settings: Any = None, ctx: Any = None):
        super().__init__(settings, ctx)
        self.pending: list[dict[str, Any]] = []

    async def request(self, approval_id, summary, requirement):
        """Append the request to ``pending`` and return unanswered."""
        self.pending.append({"approval_id": approval_id, "summary": dict(summary), "approvers": dict(requirement.approvers)})
        return ChannelResult(answered=False)


@register
class AutoChannel(ApprovalChannel):
    """Answers at once. For tests and unattended demos only."""

    name = "auto"

    class Settings(ProviderSettings):
        approve: bool = True
        approver: str = "auto-approver"
        roles: list[str] = []

    async def request(self, approval_id, summary, requirement):
        """Return an immediate answer built from the ``approve``, ``approver`` and ``roles`` settings."""
        return ChannelResult(
            True, self.settings.approve, Principal(self.settings.approver, roles=tuple(self.settings.roles)), "auto channel"
        )
