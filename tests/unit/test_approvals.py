"""Tool approval: the three factories of `_engine/factories/approval.py` and the ApprovalService that ties them to the guard."""

import json
import time

import httpx
import pytest

from retail_kyc_onboarding._engine.datatypes import ApprovalRequirement, Decision, Principal, ToolDef
from retail_kyc_onboarding._engine.engine.approvals import ApprovalService
from retail_kyc_onboarding._engine.engine.guard import Guard
from retail_kyc_onboarding._engine.engine.state import RunState
from retail_kyc_onboarding._engine.factories.approval import (
    AdkRequestInputChannel,
    AlwaysPolicy,
    AutoChannel,
    ByToolPolicy,
    ChannelResult,
    HttpCallbackChannel,
    NeverPolicy,
    QueueChannel,
    StandardApprovers,
)
from retail_kyc_onboarding._engine.factories.base import BuildContext
from retail_kyc_onboarding._engine.factories.registry import REGISTRY
from retail_kyc_onboarding._engine.factories.secret import MockSecrets
from retail_kyc_onboarding._engine.factories.security import AllowAllAuthz, RulesAuthz
from retail_kyc_onboarding._engine.trace import MemorySink, Tracer

ANA = Principal("ana", tenant="acme", roles=("clerk",), claims={"groups": ["branch-1"]}, token_ref="handle-1")
OFFICER = Principal("olu", roles=("compliance_officer",), claims={"groups": ["kyc"]})
OPEN_ACCOUNT = ToolDef("open_account", "api")


def tracer() -> tuple[Tracer, MemorySink]:
    """A tracer writing to a sink the test can read."""
    sink = MemorySink()
    return Tracer([sink]), sink


def requirement(**fields) -> ApprovalRequirement:
    """A required approval, by default from the compliance officer role."""
    return ApprovalRequirement(True, **{"approvers": {"kind": "roles", "any_of": ["compliance_officer"]}, **fields})


def service(authz=None, channel=None, resolver=None, trace: Tracer | None = None) -> ApprovalService:
    """An approval service on the by_tool policy, the standard resolver and the queue channel by default."""
    trace = trace or tracer()[0]
    return ApprovalService(
        ByToolPolicy(), resolver or StandardApprovers(), channel or QueueChannel(), Guard(authz or AllowAllAuthz(), trace)
    )


def test_every_approval_provider_is_registered_under_its_kind():
    """Each policy, resolver and channel can be named in runtime.yaml."""
    assert {"never", "always", "by_tool"} <= set(REGISTRY.names("approval_policy"))
    assert {"standard"} <= set(REGISTRY.names("approver_resolver"))
    assert {"adk_request_input", "http_callback", "queue", "auto"} <= set(REGISTRY.names("approval_channel"))


# ---------------------------------------------------------------- policies


async def test_the_never_policy_ignores_the_tool_settings():
    """No call needs approval under the never policy, even a tool that asks for one."""
    tool = OPEN_ACCOUNT.with_approval(policy="always")
    result = await NeverPolicy().evaluate(tool, {}, ANA, None, {})
    assert (result.required, result.reason) == (False, "policy never")


async def test_the_always_policy_uses_the_tool_approvers_or_any_authenticated():
    """Every call needs approval: from the tool's approvers when it names some, else from any authenticated principal."""
    plain = await AlwaysPolicy().evaluate(OPEN_ACCOUNT, {}, ANA, None, {})
    assert (plain.required, plain.approvers, plain.reason) == (True, {"kind": "any_authenticated"}, "policy always")
    tool = OPEN_ACCOUNT.with_approval(approvers={"kind": "users", "any_of": ["olu"]}, timeout_seconds=60, on_timeout="approve")
    named = await AlwaysPolicy().evaluate(tool, {}, ANA, None, {})
    assert (named.approvers, named.timeout_seconds, named.on_timeout) == ({"kind": "users", "any_of": ["olu"]}, 60, "approve")


@pytest.mark.parametrize(
    ("approval", "required", "reason"),
    [
        ({}, False, "tool policy never"),
        ({"policy": "never", "required": True}, False, "tool policy never"),
        ({"required": True}, True, "tool policy by_tool"),
        ({"policy": "by_tool"}, True, "tool policy by_tool"),
        ({"policy": "always"}, True, "tool policy always"),
        ({"policy": "by_tool", "required": False}, False, "tool policy by_tool, not required"),
        ({"policy": "sometimes"}, False, "unknown tool policy 'sometimes'"),
    ],
)
async def test_by_tool_follows_the_tool_policy_mode(approval, required, reason):
    """The tool's approval.policy decides: never, by_tool (with required) or always; an unknown mode requires nothing."""
    result = await ByToolPolicy().evaluate(OPEN_ACCOUNT.with_approval(**approval), {}, ANA, None, {})
    assert (result.required, result.reason) == (required, reason)


async def test_by_tool_keeps_the_tool_approval_settings():
    """A required approval carries the tool's approvers, rules and timeout."""
    tool = OPEN_ACCOUNT.with_approval(
        required=True,
        approvers={"kind": "roles", "any_of": ["compliance_officer"]},
        rules={"requester_not_allowed": True},
        timeout_seconds=30,
    )
    result = await ByToolPolicy().evaluate(tool, {}, ANA, None, {})
    assert result.approvers == {"kind": "roles", "any_of": ["compliance_officer"]}
    assert (result.rules, result.timeout_seconds, result.on_timeout) == ({"requester_not_allowed": True}, 30, "deny")


async def test_from_authz_needs_approval_only_when_authz_asked_for_it():
    """from_authz requires approval for a require_approval decision, taking its approvers over the tool's."""
    tool = OPEN_ACCOUNT.with_approval(policy="from_authz", approvers={"kind": "users", "any_of": ["olu"]})
    policy = ByToolPolicy()
    for decision in (None, Decision("allow")):
        result = await policy.evaluate(tool, {}, ANA, decision, {})
        assert (result.required, result.reason) == (False, "authz did not require approval")
    asked = await policy.evaluate(
        tool, {}, ANA, Decision("require_approval", approvers={"kind": "roles", "any_of": ["lead"]}), {}
    )
    assert (asked.required, asked.approvers, asked.reason) == (
        True,
        {"kind": "roles", "any_of": ["lead"]},
        "authz require_approval",
    )
    fallback = await policy.evaluate(tool, {}, ANA, Decision("require_approval"), {})
    assert fallback.approvers == {"kind": "users", "any_of": ["olu"]}


@pytest.mark.parametrize(
    ("when", "required"),
    [
        ("args_amount > 1000", True),
        ("arg.amount > 1000", True),
        ("args_amount > 5000", False),
        ("risk == 'high'", True),
        ("'clerk' in principal_roles and principal_subject == 'ana'", True),
        ("principal_subject == 'olu'", False),
        ("", False),
    ],
)
async def test_by_rule_evaluates_its_condition_over_args_variables_and_the_principal(when, required):
    """by_rule requires approval when ``rules.when`` holds over run variables, ``arg.*``/``args_*`` and the principal."""
    tool = OPEN_ACCOUNT.with_approval(policy="by_rule", rules={"when": when})
    result = await ByToolPolicy().evaluate(tool, {"amount": 2500}, ANA, None, {"risk": "high"})
    assert result.required is required
    assert result.reason == ("tool rule matched" if required else "tool rule did not match")


# ---------------------------------------------------------------- approver resolver


async def test_the_requester_cannot_approve_their_own_request_when_the_rules_say_so():
    """requester_not_allowed refuses the requester even when they hold the approver role."""
    req = requirement(rules={"requester_not_allowed": True})
    officer_asking = Principal("olu", roles=("compliance_officer",))
    ok, why = await StandardApprovers().may_answer(req, officer_asking, officer_asking)
    assert (ok, why) == (False, "requester may not approve their own request")
    assert (await StandardApprovers().may_answer(requirement(), officer_asking, officer_asking))[0]


@pytest.mark.parametrize(
    ("approvers", "answerer", "allowed", "reason"),
    [
        ({"kind": "roles", "any_of": ["compliance_officer"]}, OFFICER, True, "needs one of roles ['compliance_officer']"),
        ({"kind": "roles", "any_of": ["compliance_officer"]}, ANA, False, "needs one of roles ['compliance_officer']"),
        ({"kind": "users", "any_of": ["olu", "zed"]}, OFFICER, True, "needs one of users ['olu', 'zed']"),
        ({"kind": "users", "any_of": ["zed"]}, OFFICER, False, "needs one of users ['zed']"),
        ({"kind": "groups", "any_of": ["kyc"]}, OFFICER, True, "needs one of groups ['kyc']"),
        ({"kind": "groups", "any_of": ["kyc"]}, ANA, False, "needs one of groups ['kyc']"),
        ({"kind": "groups", "any_of": ["kyc"]}, Principal("nogroups"), False, "needs one of groups ['kyc']"),
        ({"kind": "telepathy"}, OFFICER, False, "unknown approver kind 'telepathy'"),
    ],
)
async def test_the_standard_resolver_checks_roles_users_and_groups(approvers, answerer, allowed, reason):
    """The answerer must hold one of the roles, be one of the users or belong to one of the groups the spec names."""
    assert await StandardApprovers().may_answer(requirement(approvers=approvers), answerer, ANA) == (allowed, reason)


async def test_any_authenticated_lets_anonymous_in_only_under_the_development_profile():
    """An empty spec means any authenticated principal; anonymous passes only in development."""
    anonymous = Principal("anonymous")
    empty = ApprovalRequirement(True)
    assert await StandardApprovers().may_answer(empty, OFFICER, ANA) == (True, "any authenticated")
    assert await StandardApprovers(None, BuildContext(env={}, profile="development")).may_answer(empty, anonymous, ANA) == (
        True,
        "any authenticated",
    )
    production = StandardApprovers(None, BuildContext(env={}, profile="production"))
    assert await production.may_answer(empty, anonymous, ANA) == (False, "any authenticated")


async def test_external_system_trusts_the_allowed_flag_of_a_successful_reply():
    """external_system posts both principals and the spec, and allows only on a 2xx reply with allowed true."""
    seen = []

    def decide(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((str(request.url), body))
        if body["answerer"]["subject"] == "broken":
            return httpx.Response(500, text="down")
        return httpx.Response(200, json={"allowed": body["answerer"]["subject"] == "olu"})

    resolver = StandardApprovers({"external_url": "http://approvers.test/check"})
    resolver.transport = httpx.MockTransport(decide)
    req = requirement(approvers={"kind": "external_system"})
    assert await resolver.may_answer(req, OFFICER, ANA) == (True, "external system decision")
    assert (await resolver.may_answer(req, Principal("zed"), ANA))[0] is False
    assert (await resolver.may_answer(req, Principal("broken"), ANA))[0] is False
    url, body = seen[0]
    assert url == "http://approvers.test/check"
    assert body == {"answerer": OFFICER.public(), "requester": ANA.public(), "spec": {"kind": "external_system"}}
    assert "claims" not in body["requester"] and "token_ref" not in body["requester"]


async def test_external_system_prefers_the_url_in_the_spec():
    """A url in the approver spec wins over the resolver's external_url setting."""
    urls = []

    def record(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, json={"allowed": True})

    resolver = StandardApprovers({"external_url": "http://default.test/"})
    resolver.transport = httpx.MockTransport(record)
    await resolver.may_answer(requirement(approvers={"kind": "external_system", "url": "http://spec.test/ok"}), OFFICER, ANA)
    assert urls == ["http://spec.test/ok"]


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (httpx.ConnectError("refused"), "external system unavailable (ConnectError)"),
        (httpx.Response(200, text="<html>proxy</html>"), "external system unavailable (JSONDecodeError)"),
        (httpx.Response(200, json=["allowed"]), "external system unavailable (AttributeError)"),
        (httpx.Response(200, json={"allowed": "false"}), "external system decision"),
    ],
)
async def test_external_system_refuses_when_it_cannot_be_read(reply, reason):
    """An unreachable approver system, an unreadable reply or an allowed flag that is not true refuses; nothing is raised."""

    def answer(request: httpx.Request) -> httpx.Response:
        if isinstance(reply, Exception):
            raise reply
        return reply

    resolver = StandardApprovers({"external_url": "http://approvers.test/check"})
    resolver.transport = httpx.MockTransport(answer)
    assert await resolver.may_answer(requirement(approvers={"kind": "external_system"}), OFFICER, ANA) == (False, reason)


async def test_external_system_without_a_url_refuses():
    """No url in the spec and no external_url setting: refused before any request."""
    assert await StandardApprovers().may_answer(requirement(approvers={"kind": "external_system"}), OFFICER, ANA) == (
        False,
        "external_system has no url",
    )


# ---------------------------------------------------------------- channels


async def test_the_auto_channel_answers_at_once_from_its_settings():
    """The auto channel decides without a pause, as the configured approver with the configured roles."""
    result = await AutoChannel().request("a1", {}, requirement())
    assert (result.answered, result.approved, result.approver.subject, result.comment) == (
        True,
        True,
        "auto-approver",
        "auto channel",
    )
    refusing = await AutoChannel({"approve": False, "approver": "bot", "roles": ["compliance_officer"]}).request(
        "a1", {}, requirement()
    )
    assert (refusing.answered, refusing.approved, refusing.approver.roles) == (True, False, ("compliance_officer",))


async def test_the_request_input_channel_leaves_the_run_paused():
    """The ADK request-input channel never answers, so the run pauses in the session."""
    assert await AdkRequestInputChannel().request("a1", {"tool": "t"}, requirement()) == ChannelResult(answered=False)


async def test_the_queue_channel_collects_requests_unanswered():
    """The queue channel keeps each request in ``pending`` and returns unanswered."""
    channel = QueueChannel()
    result = await channel.request("a1", {"tool": "open_account"}, requirement())
    assert result.answered is False
    assert channel.pending == [
        {
            "approval_id": "a1",
            "summary": {"tool": "open_account"},
            "approvers": {"kind": "roles", "any_of": ["compliance_officer"]},
        }
    ]


async def test_the_http_callback_channel_posts_the_request_with_the_token_from_the_secret_store():
    """The callback channel posts id, summary and approvers with its headers and a bearer token read by logical name."""
    seen = []

    def accept(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202)

    ctx = BuildContext(env={}, built={"secret": MockSecrets({"values": {"approvals_token": "tok-1"}})})
    channel = HttpCallbackChannel(
        {"url": "http://approvals.test/requests", "headers": {"X-Source": "kyc"}, "token_secret": "approvals_token"}, ctx
    )
    channel.transport = httpx.MockTransport(accept)
    result = await channel.request("a1", {"tool": "open_account"}, requirement())
    assert result.answered is False
    request = seen[0]
    assert (request.headers["Authorization"], request.headers["X-Source"]) == ("Bearer tok-1", "kyc")
    assert json.loads(request.content) == {
        "approval_id": "a1",
        "summary": {"tool": "open_account"},
        "approvers": {"kind": "roles", "any_of": ["compliance_officer"]},
    }


async def test_the_http_callback_channel_sends_no_token_without_a_secret_and_raises_on_an_error_status():
    """Without token_secret no Authorization header is sent, and an error reply raises HTTPStatusError."""
    seen = []

    def refuse(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(503)

    channel = HttpCallbackChannel({"url": "http://approvals.test/requests"})
    channel.transport = httpx.MockTransport(refuse)
    with pytest.raises(httpx.HTTPStatusError):
        await channel.request("a1", {}, requirement())
    assert "Authorization" not in seen[0].headers


# ---------------------------------------------------------------- approval service


async def test_requirement_asks_the_policy():
    """ApprovalService.requirement is the policy's answer for the call."""
    tool = OPEN_ACCOUNT.with_approval(policy="always")
    assert (await service().requirement(tool, {}, ANA, None, {})).reason == "tool policy always"


async def test_open_records_the_pending_approval_with_redacted_args_and_traces_it():
    """open stores the request in run state with secret-looking args redacted, traces it and sends it to the channel."""
    channel = QueueChannel()
    trace, sink = tracer()
    state = RunState({})
    req = requirement(timeout_seconds=600, reason="tool policy always")
    before = time.time()
    result = await service(channel=channel).open(
        approval_id="a1",
        tool=OPEN_ACCOUNT,
        args={"name": "Ada", "api_key": "k-123", "nested": {"password": "pw"}},
        requirement=req,
        requester=ANA,
        node_id="open_account_step",
        state=state,
        tracer=trace,
    )
    assert result.answered is False
    record = state.pending()["a1"]
    assert record["summary"] == {
        "tool": "open_account",
        "args": {"name": "Ada", "api_key": "[redacted]", "nested": {"password": "[redacted]"}},
        "reason": "tool policy always",
        "requested_by": "ana",
        "node": "open_account_step",
    }
    assert (record["tool"], record["node"], record["requester"]) == ("open_account", "open_account_step", ANA.to_state())
    assert record["requirement"]["approvers"] == {"kind": "roles", "any_of": ["compliance_officer"]}
    assert before + 600 <= record["expires_at"] <= time.time() + 600
    assert channel.pending[0]["summary"] == record["summary"]
    (event,) = sink.of("approval.requested")
    assert {k: event[k] for k in ("approval_id", "tool", "node", "approvers", "reason")} == {
        "approval_id": "a1",
        "tool": "open_account",
        "node": "open_account_step",
        "approvers": {"kind": "roles", "any_of": ["compliance_officer"]},
        "reason": "tool policy always",
    }
    assert "k-123" not in json.dumps(sink.records) and "k-123" not in json.dumps(state.raw)


async def test_open_without_a_timeout_never_expires_and_returns_the_channel_answer():
    """A requirement without timeout_seconds stores no expiry; an auto channel's answer is returned as is."""
    state = RunState({})
    result = await service(channel=AutoChannel()).open(
        approval_id="a2",
        tool=OPEN_ACCOUNT,
        args={},
        requirement=requirement(),
        requester=ANA,
        node_id="n",
        state=state,
        tracer=tracer()[0],
    )
    assert (result.answered, result.approved) == (True, True)
    assert state.pending()["a2"]["expires_at"] is None


async def opened(svc: ApprovalService, req: ApprovalRequirement, requester: Principal = ANA) -> dict:
    """Open approval ``a1`` for open_account under ``req`` and return the stored record."""
    state = RunState({})
    await svc.open(
        approval_id="a1",
        tool=OPEN_ACCOUNT,
        args={},
        requirement=req,
        requester=requester,
        node_id="n",
        state=state,
        tracer=tracer()[0],
    )
    return state.pending()["a1"]


def test_requirement_of_rebuilds_the_stored_requirement_with_defaults():
    """A stored record turns back into the same ApprovalRequirement, and missing fields take their defaults."""
    req = requirement(rules={"requester_not_allowed": True}, timeout_seconds=5.0, on_timeout="approve", reason="r")
    assert (
        ApprovalService.requirement_of(
            {"requirement": {"required": True, **{k: v for k, v in vars(req).items() if k != "required"}}}
        )
        == req
    )
    assert ApprovalService.requirement_of({"requirement": {"required": 1}}) == ApprovalRequirement(True)


async def test_a_valid_approver_is_accepted_and_authorized_by_the_guard():
    """An officer answering another person's request passes the resolver, and the guard is asked for approval.answer."""
    trace, sink = tracer()
    svc = service()
    record = await opened(svc, requirement(rules={"requester_not_allowed": True}))
    assert await svc.verify_answer(record, OFFICER, trace) == (True, "needs one of roles ['compliance_officer']")
    (authz,) = sink.of("authz")
    assert (authz["action"], authz["resource"], authz["effect"]) == (
        "approval.answer",
        {"id": "open_account", "approval_id": "a1"},
        "allow",
    )


async def test_the_requester_cannot_answer_their_own_request():
    """With requester_not_allowed the requester is refused, the refusal is traced and the guard is not asked."""
    trace, sink = tracer()
    svc = service()
    requester = Principal("olu", roles=("compliance_officer",))
    record = await opened(svc, requirement(rules={"requester_not_allowed": True}), requester=requester)
    assert await svc.verify_answer(record, requester, trace) == (False, "requester may not approve their own request")
    (event,) = sink.of("approval.rejected_answerer")
    assert (event["approval_id"], event["answerer"]["subject"]) == ("a1", "olu")
    assert sink.of("authz") == []


async def test_an_approver_without_the_required_role_is_refused():
    """An answerer outside the approver roles is refused with the roles it needs, and the refusal is traced."""
    trace, sink = tracer()
    svc = service()
    record = await opened(svc, requirement())
    assert await svc.verify_answer(record, ANA, trace) == (False, "needs one of roles ['compliance_officer']")
    assert sink.of("approval.rejected_answerer")[0]["reason"] == "needs one of roles ['compliance_officer']"


async def test_an_expired_request_is_refused_before_anyone_is_checked():
    """Past expires_at no answerer is accepted, not even a valid approver."""
    trace, sink = tracer()
    svc = service()
    record = {**await opened(svc, requirement(timeout_seconds=60)), "expires_at": time.time() - 1}
    assert await svc.verify_answer(record, OFFICER, trace) == (False, "approval request expired")
    assert sink.records == []


async def test_a_request_not_yet_expired_is_still_answerable():
    """Before expires_at a valid approver is accepted."""
    svc = service()
    record = await opened(svc, requirement(timeout_seconds=60))
    assert (await svc.verify_answer(record, OFFICER, tracer()[0]))[0] is True


async def test_the_guard_can_still_refuse_an_approver_the_resolver_accepts():
    """When authz denies approval.answer the answer is refused with the authz reason."""
    trace, sink = tracer()
    rules = RulesAuthz(
        {"rules": [{"effect": "deny", "actions": ["approval.answer"], "subjects": ["olu"], "reason": "olu is on leave"}]}
    )
    svc = service(authz=rules, trace=trace)
    record = await opened(svc, requirement())
    assert await svc.verify_answer(record, OFFICER, trace) == (False, "olu is on leave")
    assert sink.of("authz")[0]["effect"] == "deny"


async def test_a_guard_denial_without_a_reason_has_a_default_one():
    """A deny with no reason is reported as not authorized to answer approvals."""
    rules = RulesAuthz({"rules": [{"effect": "deny", "actions": ["approval.*"], "reason": ""}]})
    svc = service(authz=rules)
    assert await svc.verify_answer(await opened(svc, requirement()), OFFICER, tracer()[0]) == (
        False,
        "not authorized to answer approvals",
    )


async def test_rules_authz_with_a_matching_allow_rule_accepts_the_approver():
    """The rules authz allows an officer whose role a rule lists, and denies everyone else by default."""
    rules = RulesAuthz(
        {"rules": [{"effect": "allow", "roles": ["compliance_officer"], "actions": ["approval.answer"], "resources": ["open_*"]}]}
    )
    svc = service(authz=rules, resolver=StandardApprovers())
    record = await opened(svc, ApprovalRequirement(True, {"kind": "any_authenticated"}))
    assert (await svc.verify_answer(record, OFFICER, tracer()[0]))[0] is True
    assert await svc.verify_answer(record, ANA, tracer()[0]) == (False, "no rule matched")


async def test_resolve_clears_the_pending_record_and_traces_the_answer():
    """resolve removes the pending approval from run state and traces approval.answered with the public approver."""
    trace, sink = tracer()
    svc = service()
    state = RunState({})
    await svc.open(
        approval_id="a1",
        tool=OPEN_ACCOUNT,
        args={},
        requirement=requirement(),
        requester=ANA,
        node_id="n",
        state=state,
        tracer=trace,
    )
    await svc.open(
        approval_id="a2",
        tool=OPEN_ACCOUNT,
        args={},
        requirement=requirement(),
        requester=ANA,
        node_id="n",
        state=state,
        tracer=trace,
    )
    svc.resolve(state, "a1", True, OFFICER, "documents verified", trace)
    assert list(state.pending()) == ["a2"]
    (event,) = sink.of("approval.answered")
    assert (event["approval_id"], event["approved"], event["approver"], event["comment"]) == (
        "a1",
        True,
        OFFICER.public(),
        "documents verified",
    )
    assert "claims" not in event["approver"]
