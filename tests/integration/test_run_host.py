"""The governed run host, through RunHost and the HTTP routes: a run that pauses at the review gate, is listed, read and
resumed by the right people, tool approvals found and answered, background results delivered, and clean errors for unknown
or foreign runs."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio

from retail_kyc_onboarding._engine.app import AppConfig, FlowSettings
from retail_kyc_onboarding._engine.datatypes import ApprovalRequirement, Principal
from retail_kyc_onboarding._engine.engine.engine import Engine
from retail_kyc_onboarding._engine.engine.host import TRACE, Answer, RunHost
from retail_kyc_onboarding._engine.errors import PolicyDenied, RunError, RunNotFound
from retail_kyc_onboarding._engine.factories.approval import AdkRequestInputChannel, ApprovalPolicy
from retail_kyc_onboarding._engine.factories.security import RulesAuthz, TrustedHeaderAuthn
from retail_kyc_onboarding._engine.runtime.port import PauseInfo, UnitResult
from retail_kyc_onboarding._engine.server import create_app
from retail_kyc_onboarding._engine.testing.offline import offline_providers
from retail_kyc_onboarding._engine.testing.trajectory import check_trajectory
from retail_kyc_onboarding.workflow import WORKFLOW

ID_NUMBER = "57-607128-154274-1"
MESSAGE = f"Hi, I'd like to open a Savings Account. My name is Arturo Conroy, ID number {ID_NUMBER}, email arturo.conroy@example.com"
RECORD = {"full_name": "Arturo Conroy", "id_number": ID_NUMBER, "dob": "1996-03-23", "address": "McKenzie Crest", "id_status": "ACTIVE"}
INTAKE = {"full_name": "Arturo Conroy", "id_number": ID_NUMBER, "product": "Savings Account", "email": "arturo.conroy@example.com"}
RISK = {"kyc_risk": "LOW", "recommendation": "APPROVE", "reasons": json.dumps(["scripted"]), "summary": "Scripted."}
CREATED = {"account_number": "AC-1", "customer_id_number": RECORD["id_number"], "status": "ACTIVE"}
REGISTRY_TOOL = "retail_kyc_id_registry_lookup"
GATE = "compliance_approval_review:before:1"
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

ALICE = Principal("alice", roles=("offline-approver",))  # the applicant's caseworker, who starts the runs
BOB = Principal("bob", roles=("kyc-officer",))  # may approve the registry lookup, may not read other people's runs
CAROL = Principal("carol", roles=("auditor",))  # may read any run, may not answer one
DAVE = Principal("dave", roles=("team-lead",))  # may answer questions on runs of their team
BLOCKED = Principal("blocked")

AUTHZ = {
    "default": "allow",
    "rules": [
        {"effect": "deny", "subjects": ["blocked"], "actions": ["run.start"], "reason": "suspended"},
        {"effect": "allow", "roles": ["auditor"], "actions": ["run.read"]},
        {"effect": "allow", "roles": ["team-lead"], "actions": ["run.read", "run.resume"]},
        {"effect": "deny", "actions": ["run.read", "run.resume"], "reason": "not the requester"},
    ],
}


def headers(principal: Principal) -> dict[str, str]:
    return {"x-user": principal.subject, "x-roles": ",".join(principal.roles)}


class OfficerApproval(ApprovalPolicy):
    """The registry lookup needs a KYC officer's approval; no other tool does."""

    name = "test_officer_approval"

    async def evaluate(self, tool, args, principal, decision, variables):
        officers = {"kind": "roles", "any_of": ["kyc-officer"]}
        return ApprovalRequirement(tool.name == REGISTRY_TOOL, officers, reason="registry lookup")


@pytest.fixture(autouse=True)
def world_bank(monkeypatch):
    class WorldBank:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def send(self, text: str, **k: Any) -> Any:
            return SimpleNamespace(text='{"GDP growth": 2.1}', state="completed", raw={}, context_id="c", task_id="t")

    monkeypatch.setattr("retail_kyc_onboarding._engine.engine.executors.external_agent.A2aClient", WorldBank)


async def build_engine(*, approvals: bool = False) -> Engine:
    script = [{"contains": "KYC Intake Agent", "json": INTAKE}, {"contains": "KYC Risk Agent", "json": RISK}]
    responses = {REGISTRY_TOOL: [RECORD], "retail_kyc_account_creation": CREATED}
    providers = offline_providers(WORKFLOW, model_script=script, api_responses=responses)
    providers = replace(providers, authn=TrustedHeaderAuthn(), authz=RulesAuthz(AUTHZ))
    if approvals:
        providers = replace(providers, approval_policy=OfficerApproval(), approval_channel=AdkRequestInputChannel())
    return await Engine.from_content(WORKFLOW, AppConfig(settings=FlowSettings.model_construct(), providers=providers))


@pytest_asyncio.fixture
async def engine():
    eng = await build_engine()
    try:
        yield eng
    finally:
        await eng.aclose()


@pytest_asyncio.fixture
async def approval_engine():
    eng = await build_engine(approvals=True)
    try:
        yield eng
    finally:
        await eng.aclose()


async def serve(eng: Engine):
    """An HTTP client on the run host app for this engine, with its lifespan started."""
    app = create_app(engine=eng)
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return app, lifespan, client


@pytest_asyncio.fixture
async def client(engine):
    _, lifespan, http = await serve(engine)
    try:
        yield http
    finally:
        await http.aclose()
        await lifespan.__aexit__(None, None, None)


@pytest_asyncio.fixture
async def approval_client(approval_engine):
    _, lifespan, http = await serve(approval_engine)
    try:
        yield http
    finally:
        await http.aclose()
        await lifespan.__aexit__(None, None, None)


def steps(eng: Engine) -> list[str]:
    return list(dict.fromkeys(r["node"] for r in eng.factories.telemetry.sink.records if r.get("event") == "step.start"))


async def start(client: httpx.AsyncClient, principal: Principal = ALICE, **body: Any) -> dict[str, Any]:
    res = await client.post("/v1/runs", json={"message": MESSAGE, **body}, headers=headers(principal))
    assert res.status_code == 200, res.text
    return res.json()


async def resume(client: httpx.AsyncClient, session_id: str, principal: Principal = ALICE, **body: Any) -> httpx.Response:
    return await client.post(f"/v1/runs/{session_id}/resume", json=body, headers=headers(principal))


def sse_events(text: str) -> list[tuple[str, str]]:
    """(event, data) pairs of a server-sent event stream."""
    events = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in fields:
            events.append((fields["event"], fields.get("data", "")))
    return events


# the review gate


async def test_a_run_pauses_at_the_review_gate_and_is_listed_for_its_requester(client):
    """A low-risk applicant reaches the review gate; the requester sees it in the status and their worklist, others do not."""
    run = await start(client)
    assert run["status"] == "paused" and run["pause"]["kind"] == "input" and run["pause"]["interrupt_id"] == GATE
    assert 'Reply "Proceed" to continue' in run["pause"]["message"]
    status = (await client.get(f"/v1/runs/{run['session_id']}", headers=headers(ALICE))).json()
    assert (status["status"], status["last_status"], status["requested_by"]) == ("paused", "paused", "alice")
    assert status["run_id"] == run["run_id"]
    assert status["pause"]["interrupt_id"] == GATE and status["variables"]["kyc_risk_agent.kyc_risk"] == "LOW"
    mine = (await client.get("/v1/approvals", headers=headers(ALICE))).json()
    assert [(i["session_id"], i["interrupt_id"], i["requested_by"]) for i in mine] == [(run["session_id"], GATE, "alice")]
    assert (await client.get("/v1/approvals", headers=headers(BOB))).json() == []


async def test_an_affirmative_reply_continues_and_an_approval_opens_the_account(client, engine):
    """Proceed passes the gate to the officer's question; Approve creates the account and the run finishes."""
    run = await start(client)
    asked = await resume(client, run["session_id"], text="Proceed")
    assert asked.status_code == 200 and asked.json()["pause"]["interrupt_id"] == "compliance_approval_review:ask:2"
    done = (await resume(client, run["session_id"], text="Approve")).json()
    assert done["status"] == "done" and done["run_id"] == run["run_id"]
    assert steps(engine)[-2:] == ["create_active_account", "summarize_opened_account"]
    assert not check_trajectory(engine.factories.telemetry.sink.records, engine.unit.graph)
    status = (await client.get(f"/v1/runs/{run['session_id']}", headers=headers(ALICE))).json()
    assert (status["status"], status["last_status"], status["pause"], status["last_error"]) == ("idle", "done", None, "")
    assert (await client.get("/v1/approvals", headers=headers(ALICE))).json() == []


async def test_any_other_reply_at_the_gate_stops_the_run(client, engine):
    """A reply that is not affirmative stops the run before the review, and the stopped run takes no further answer."""
    run = await start(client)
    stopped = (await resume(client, run["session_id"], text="Stop")).json()
    assert (stopped["status"], stopped["output"]) == ("done", "Stopped by the user.")
    assert steps(engine)[-1] == "compliance_approval_review" and "create_active_account" not in steps(engine)
    again = await resume(client, run["session_id"], text="Proceed")
    assert again.status_code == 409 and again.json()["detail"] == "this session is not waiting for an answer"


async def test_an_answer_for_another_interrupt_is_refused(client):
    """A reply that names an interrupt the run is not waiting on is a conflict, and the run stays paused."""
    run = await start(client)
    res = await resume(client, run["session_id"], text="Proceed", interrupt_id="compliance_approval_review:ask:9")
    assert res.status_code == 409
    assert res.json()["detail"] == f"the session is waiting on '{GATE}', not 'compliance_approval_review:ask:9'"
    assert (await client.get(f"/v1/runs/{run['session_id']}", headers=headers(ALICE))).json()["status"] == "paused"


async def test_a_paused_session_takes_no_new_message(client):
    """Starting a run on a session that waits for an answer is a conflict."""
    run = await start(client)
    res = await client.post("/v1/runs", json={"message": "again", "session_id": run["session_id"]}, headers=headers(ALICE))
    assert res.status_code == 409 and "resume it instead" in res.json()["detail"]


async def test_the_run_host_reports_the_pending_pause_and_the_trace(client, engine):
    """RunHost.pending returns the gate of a paused session, and the trace route returns the records of its run."""
    run = await start(client)
    host = RunHost(engine)
    assert (await host.pending(run["session_id"], ALICE)).interrupt_id == GATE
    trace = (await client.get(f"/v1/runs/{run['session_id']}/trace", headers=headers(ALICE))).json()
    assert trace["run_id"] == run["run_id"] and trace["records"]
    assert {r["run_id"] for r in trace["records"]} == {run["run_id"]}


async def test_a_run_keeps_the_callers_trace_for_its_resume(engine):
    """A run started inside a caller's trace stores that context and a resume without one continues it."""
    host = RunHost(engine)
    run = await host.start(message=MESSAGE, principal=ALICE, trace={"traceparent": TRACEPARENT})
    assert (await engine.factories.runstore.get(TRACE + run.session_id))["traceparent"] == TRACEPARENT
    resumed = await host.resume(session_id=run.session_id, answer=Answer(text="Proceed"), answerer=ALICE, owner="ignored")
    assert resumed.status == "paused" and resumed.pause.interrupt_id == "compliance_approval_review:ask:2"


# streaming


async def test_a_streamed_run_sends_trace_events_then_the_result(client):
    """With stream set, the start route answers with server-sent trace events and ends with the paused result."""
    res = await client.post("/v1/runs", json={"message": MESSAGE, "stream": True}, headers=headers(ALICE))
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/event-stream")
    events = sse_events(res.text)
    assert events[-1][0] == "result" and {e for e, _ in events[:-1]} == {"trace"}
    result = json.loads(events[-1][1])
    assert result["status"] == "paused" and result["pause"]["interrupt_id"] == GATE


async def test_a_streamed_resume_ends_with_the_result_or_the_error(client):
    """A resume that accepts an event stream ends with its result, and one the run cannot take ends with an error event."""
    run = await start(client)
    accept = {**headers(ALICE), "accept": "text/event-stream"}
    res = await client.post(f"/v1/runs/{run['session_id']}/resume", json={"text": "Stop"}, headers=accept)
    events = sse_events(res.text)
    assert events[-1][0] == "result" and json.loads(events[-1][1])["output"] == "Stopped by the user."
    res = await client.post(f"/v1/runs/{run['session_id']}/resume", json={"text": "Stop"}, headers=accept)
    assert sse_events(res.text) == [("error", json.dumps({"error": "this session is not waiting for an answer"}))]


# who may read and answer a run


async def test_another_principal_cannot_read_or_answer_the_run(client):
    """A principal who is neither the requester nor allowed to read gets 403 on status, trace and resume."""
    run = await start(client)
    sid = run["session_id"]
    for res in (
        await client.get(f"/v1/runs/{sid}", headers=headers(BOB)),
        await client.get(f"/v1/runs/{sid}/trace", headers=headers(BOB)),
        await resume(client, sid, BOB, text="Proceed"),
    ):
        assert res.status_code == 403 and res.headers["content-type"] == "application/problem+json"
        assert res.json()["detail"].endswith("(not the requester)")


async def test_a_principal_allowed_to_read_sees_the_run_but_cannot_answer_it(client):
    """An auditor reads someone else's paused run, but answering it is still denied."""
    run = await start(client)
    status = await client.get(f"/v1/runs/{run['session_id']}", headers=headers(CAROL))
    assert status.status_code == 200 and status.json()["requested_by"] == "alice"
    assert (await resume(client, run["session_id"], CAROL, text="Proceed")).status_code == 403


async def test_a_principal_allowed_to_resume_answers_for_the_requester(client, engine):
    """A team lead the policy lets resume answers the gate, the run continues as the requester and the answer is traced."""
    run = await start(client)
    res = await resume(client, run["session_id"], DAVE, text="Proceed")
    assert res.status_code == 200 and res.json()["pause"]["interrupt_id"] == "compliance_approval_review:ask:2"
    answers = [r for r in engine.factories.telemetry.sink.records if r.get("event") == "run.answer"]
    assert [(a["answerer"]["subject"], a["requester"]["subject"]) for a in answers] == [("dave", "alice")]


async def test_the_requester_continues_their_own_finished_session(client):
    """A new message on the requester's own idle session starts a new run in it."""
    run = await start(client)
    await resume(client, run["session_id"], text="Stop")
    again = await start(client, session_id=run["session_id"])
    assert again["session_id"] == run["session_id"] and again["run_id"] != run["run_id"]


async def test_the_run_host_refuses_a_foreign_read_with_policy_denied(engine):
    """RunHost.status raises PolicyDenied for a principal the policy does not let read the run."""
    host = RunHost(engine)
    run = await host.start(message=MESSAGE, principal=ALICE)
    with pytest.raises(PolicyDenied, match="run.read"):
        await host.status(run.session_id, BOB)
    assert (await host.status(run.session_id, CAROL))["status"] == "paused"


async def test_a_finished_session_of_another_user_cannot_be_continued(client):
    """A new message on someone else's idle session is denied: the session belongs to its requester."""
    run = await start(client)
    await resume(client, run["session_id"], text="Stop")
    res = await client.post("/v1/runs", json={"message": "mine now", "session_id": run["session_id"]}, headers=headers(BOB))
    assert res.status_code == 403 and "the session belongs to another user" in res.json()["detail"]


async def test_a_principal_denied_run_start_gets_an_error_result(engine):
    """When the policy denies run.start the host returns an error result instead of running."""
    res = await RunHost(engine).start(message=MESSAGE, principal=BLOCKED, session_id="s-blocked", run_id="r-blocked")
    assert (res.status, res.session_id, res.run_id) == ("error", "s-blocked", "r-blocked")
    assert res.error == "Denied: run.start (suspended)"
    assert steps(engine) == []


async def test_a_request_without_credentials_is_unauthorized(client):
    """The routes need an authenticated caller."""
    res = await client.get("/v1/approvals")
    assert res.status_code == 401 and res.headers["www-authenticate"] == "Bearer"


# unknown ids


async def test_unknown_run_and_approval_ids_give_clean_problem_errors(client):
    """Unknown sessions and approvals answer with a problem document, never a server error."""
    for res in (
        await client.get("/v1/runs/no-such-run", headers=headers(ALICE)),
        await client.get("/v1/runs/no-such-run/trace", headers=headers(ALICE)),
    ):
        assert res.status_code < 500 and res.headers["content-type"] == "application/problem+json"
        assert res.json()["detail"] == "unknown session"
    missing = await client.post("/v1/approvals/no-such-approval", json={"approved": True}, headers=headers(BOB))
    assert missing.status_code == 404 and missing.json()["detail"] == "no pending approval with this id"
    idle = await resume(client, "no-such-run", text="Proceed")
    assert idle.status_code == 409 and idle.json()["detail"] == "this session is not waiting for an answer"


async def test_an_unknown_run_is_not_found(client):
    """Reading a session that does not exist answers 404, as the API's error table documents."""
    assert (await client.get("/v1/runs/no-such-run", headers=headers(ALICE))).status_code == 404


async def test_the_run_host_raises_not_found_for_an_unknown_session(engine):
    """RunHost.status raises RunNotFound, and an unknown approval id is not found."""
    host = RunHost(engine)
    with pytest.raises(RunNotFound, match="unknown session"):
        await host.status("no-such-run", ALICE)
    assert await host.find_approval("no-such-approval") is None
    assert await host.pending("no-such-run", ALICE) is None


# tool approvals


async def test_an_approval_is_listed_for_its_approvers_and_answered_by_one(approval_client, approval_engine):
    """The registry lookup waits for a KYC officer: only officers see it, others and text are refused, approval runs it."""
    run = await start(approval_client)
    pause = run["pause"]
    assert run["status"] == "paused" and pause["kind"] == "approval"
    assert pause["approvers"] == {"kind": "roles", "any_of": ["kyc-officer"]} and pause["payload"]["tool"] == REGISTRY_TOOL
    assert await RunHost(approval_engine).find_approval(pause["approval_id"]) == run["session_id"]
    listed = (await approval_client.get("/v1/approvals", headers=headers(BOB))).json()
    assert [(i["approval_id"], i["kind"], i["requested_by"]) for i in listed] == [(pause["approval_id"], "approval", "alice")]
    assert (await approval_client.get("/v1/approvals", headers=headers(ALICE))).json() == []

    url = f"/v1/approvals/{pause['approval_id']}"
    by_requester = await approval_client.post(url, json={"approved": True}, headers=headers(ALICE))
    assert by_requester.status_code == 403 and "needs one of roles ['kyc-officer']" in by_requester.json()["detail"]
    as_text = await resume(approval_client, run["session_id"], BOB, text="yes")
    assert as_text.status_code == 409 and "approved true or false" in as_text.json()["detail"]

    approved = await approval_client.post(url, json={"approved": True, "comment": "ok"}, headers=headers(BOB))
    assert approved.status_code == 200 and approved.json()["pause"]["interrupt_id"] == GATE
    calls = [r for r in approval_engine.factories.telemetry.sink.records if r.get("event") == "approval.answered"]
    assert [(c["approved"], c["approver"]["subject"], c["comment"]) for c in calls] == [(True, "bob", "ok")]
    assert await RunHost(approval_engine).find_approval(pause["approval_id"]) is None


async def test_a_rejected_approval_skips_the_tool_and_rejects_the_applicant(approval_client, approval_engine):
    """An officer's rejection skips the registry lookup, which routes the applicant to the rejection."""
    run = await start(approval_client)
    url = f"/v1/approvals/{run['pause']['approval_id']}"
    rejected = await approval_client.post(url, json={"approved": False}, headers=headers(BOB))
    assert rejected.status_code == 200, rejected.text
    skipped = [r for r in approval_engine.factories.telemetry.sink.records if r.get("event") == "tool.skipped"]
    assert skipped and skipped[0]["tool"] == REGISTRY_TOOL
    assert "retrieve_country_risk_context" not in steps(approval_engine)


# background results and pauses only the system may answer (the runtime is stubbed: this export has no asynchronous child)


@pytest_asyncio.fixture
async def paused(engine):
    """A run of alice's paused at the gate, and its host."""
    host = RunHost(engine)
    run = await host.start(message=MESSAGE, principal=ALICE)
    return host, run.session_id


def stub_pause(monkeypatch, eng: Engine, pause: PauseInfo) -> list[dict[str, Any]]:
    """Make the session wait on ``pause`` and record the runs the host starts instead of running them."""
    calls: list[dict[str, Any]] = []

    async def pending_pause(unit, session_id, user_id):
        return pause

    async def run_unit(unit, **kw):
        calls.append(kw)
        return UnitResult("done", "woken", {"x": 1})

    monkeypatch.setattr(eng.runtime, "pending_pause", pending_pause)
    monkeypatch.setattr(eng.runtime, "run_unit", run_unit)
    return calls


async def test_a_background_result_wakes_the_run_with_its_token(paused, engine, monkeypatch):
    """deliver stores the result under its token and resumes the run as its requester; the token never reaches a client."""
    host, sid = paused
    pause = PauseInfo("input", "child:wait:1", "Waiting for the child", {"await_token": "tok-123456789", "internal": True})
    calls = stub_pause(monkeypatch, engine, pause)
    assert "await_token" not in (await host.pending(sid, ALICE)).payload
    res = await host.deliver(session_id=sid, token="tok-123456789", result={"status": "done", "output": "child ok"})
    assert (res.status, res.output, res.variables) == ("done", "woken", {"x": 1})
    assert await engine.factories.runstore.get("delivered:tok-123456789") == {"status": "done", "output": "child ok"}
    [call] = calls
    assert call["principal"].subject == "alice" and call["resume"].response == {"await_token": "tok-123456789"}
    assert (await engine.factories.runstore.get("outcome:" + sid))["status"] == "done"


async def test_a_result_with_the_wrong_token_or_for_a_run_that_is_not_waiting_is_refused(paused, engine, monkeypatch):
    """deliver refuses a token the run is not waiting for, including a run paused at a person's question."""
    host, sid = paused
    with pytest.raises(RunError, match="not waiting for this result"):
        await host.deliver(session_id=sid, token="anything", result={})
    stub_pause(monkeypatch, engine, PauseInfo("input", "child:wait:1", payload={"await_token": "right", "internal": True}))
    with pytest.raises(RunError, match="not waiting for this result"):
        await host.deliver(session_id=sid, token="wrong", result={})


async def test_a_run_waiting_for_background_work_takes_no_answer(paused, engine, monkeypatch):
    """A person cannot answer a pause that only the system may complete."""
    host, sid = paused
    stub_pause(monkeypatch, engine, PauseInfo("input", "child:wait:1", payload={"await_token": "t", "internal": True}))
    with pytest.raises(RunError, match="only the system can complete it"):
        await host.resume(session_id=sid, answer=Answer(text="Proceed"), answerer=ALICE)


def approval_record(approval_id: str, approvers: dict[str, Any]) -> dict[str, Any]:
    return {
        "approval_id": approval_id,
        "tool": REGISTRY_TOOL,
        "requirement": {"required": True, "approvers": approvers},
        "requester": ALICE.to_state(),
    }


async def test_a_tool_confirmation_is_answered_with_a_confirmed_flag(paused, engine, monkeypatch):
    """An approval paused as an ADK tool confirmation is resumed with the confirmed flag and the approval payload."""
    host, sid = paused
    record = approval_record("ap-1", {"kind": "users", "any_of": ["bob"]})
    calls = stub_pause(monkeypatch, engine, PauseInfo("confirmation", "fc-1", "Confirm?", {}, "ap-1", record))
    res = await host.resume(session_id=sid, answer=Answer(approved=False, comment="no"), answerer=BOB)
    assert res.status == "done"
    response = calls[0]["resume"].response
    assert response["confirmed"] is False and response["payload"]["approver"]["subject"] == "bob"
    assert (response["payload"]["approval_id"], response["payload"]["comment"]) == ("ap-1", "no")


async def test_an_approval_that_is_no_longer_pending_is_refused(paused, engine, monkeypatch):
    """A pause for an approval whose record is gone cannot be answered."""
    host, sid = paused
    stub_pause(monkeypatch, engine, PauseInfo("input", "ap-gone", payload={}, approval_id="ap-gone"))
    with pytest.raises(RunError, match="no longer pending"):
        await host.resume(session_id=sid, answer=Answer(approved=True), answerer=BOB)


async def test_the_worklist_skips_approvals_without_a_record_empty_pauses_and_background_waits(engine, monkeypatch):
    """A pending approval with no stored record, a session with no pause view, or a background wait only the system can complete,
    is left out of the worklist."""
    run = {"principal": ALICE.to_state(), "run_id": "r1"}
    record = approval_record("ap-ok", {"kind": "users", "any_of": ["bob"]})
    pending = [
        ("s-none", None, run),
        ("s-norecord", PauseInfo("input", "i1", approval_id="ap-x"), run),
        ("s-child", PauseInfo("input", "child:wait:1", payload={"await_token": "t", "internal": True}), run),
        ("s-ok", PauseInfo("input", "i2", approval_id="ap-ok", approval_record=record), run),
    ]

    async def pending_sessions(unit, limit=200):
        return pending

    monkeypatch.setattr(engine.runtime, "pending_sessions", pending_sessions)
    items = await RunHost(engine).worklist(BOB)
    assert [(i.session_id, i.run_id, i.requested_by) for i in items] == [("s-ok", "r1", "alice")]
    assert items[0].pause.approvers == {"kind": "users", "any_of": ["bob"]}
