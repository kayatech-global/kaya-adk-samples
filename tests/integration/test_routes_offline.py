"""The routes the live evals do not take, run offline through ADK's Runner: a risk rejection after a clear registry, an enhanced
due diligence review that asks and then stops, and an account the accounts API fails to create."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from google.adk.runners import InMemoryRunner
from google.genai import types

from retail_kyc_onboarding._engine.datatypes import ToolResult
from retail_kyc_onboarding._engine.runtime.entry import build_workflow_and_app
from retail_kyc_onboarding._engine.testing.offline import offline_providers
from retail_kyc_onboarding._engine.testing.trajectory import check_trajectory
from retail_kyc_onboarding.settings import build_config
from retail_kyc_onboarding.workflow import WORKFLOW

MESSAGE = "Hi, I'd like to open a Savings Account. My name is Arturo Conroy, ID number 57-607128-154274-1, email arturo.conroy@example.com"
RECORD = {"full_name": "Arturo Conroy", "id_number": "57-607128-154274-1", "dob": "1996-03-23", "address": "McKenzie Crest", "id_status": "ACTIVE"}
INTAKE = {"full_name": "Arturo Conroy", "id_number": "57-607128-154274-1", "product": "Savings Account", "email": "arturo.conroy@example.com"}
CREATED = {"account_number": "AC-1", "customer_id_number": RECORD["id_number"], "status": "ACTIVE"}
FAILED = ToolResult(False, text="HTTP 500: accounts API down", error="HTTP 500: accounts API down")


def decision(risk: str, recommendation: str) -> dict[str, str]:
    return {"kyc_risk": risk, "recommendation": recommendation, "reasons": json.dumps(["scripted"]), "summary": "Scripted."}


ROUTES = [
    pytest.param(
        decision("HIGH", "REJECT"),
        CREATED,
        ["Proceed"],
        ["assess_kyc_risk", "return_rejection_or_stop_decision"],
        id="risk_rejects_after_a_clear_registry",
    ),
    pytest.param(
        decision("MEDIUM", "APPROVE WITH EDD"),
        CREATED,
        ["Proceed", "Which documents prove the source of funds?", "Proceed", "Stop - source of funds not shown", "Proceed"],
        ["assess_kyc_risk", "enhanced_due_diligence_review", "return_rejection_or_stop_decision"],
        id="edd_review_asks_then_stops",
    ),
    pytest.param(
        decision("LOW", "APPROVE"),
        FAILED,
        ["Proceed", "Approve", "Proceed"],
        ["assess_kyc_risk", "compliance_approval_review", "create_active_account", "handle_account_creation_failure"],
        id="account_creation_fails",
    ),
]


@pytest.fixture(autouse=True)
def world_bank(monkeypatch):
    class WorldBank:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def send(self, text: str, **k: Any) -> Any:
            return SimpleNamespace(text='{"GDP growth": 2.1}', state="completed", raw={}, context_id="c", task_id="t")

    monkeypatch.setattr("retail_kyc_onboarding._engine.engine.executors.external_agent.A2aClient", WorldBank)


@pytest.mark.parametrize(("risk", "account", "replies", "tail"), ROUTES)
async def test_the_route_is_taken(risk, account, replies, tail):
    script = [{"contains": "KYC Intake Agent", "json": INTAKE}, {"contains": "KYC Risk Agent", "json": risk}]
    responses = {"retail_kyc_id_registry_lookup": [RECORD], "retail_kyc_account_creation": account}
    providers = offline_providers(WORKFLOW, model_script=script, api_responses=responses)
    _, app = await build_workflow_and_app(WORKFLOW, build_config(providers=providers))
    runner = InMemoryRunner(app=app)
    session = await runner.session_service.create_session(app_name=app.name, user_id="test-user")
    content = types.Content(role="user", parts=[types.Part(text=MESSAGE)])
    left = list(replies)
    while True:
        pause = None
        async for event in runner.run_async(user_id="test-user", session_id=session.id, new_message=content):
            assert not event.error_code, f"{event.author}: {event.error_code}: {event.error_message}"
            for part in (event.content.parts if event.content else []) or []:
                if part.function_call and part.function_call.name == "adk_request_input":
                    pause = part.function_call
        if pause is None:
            break
        assert left, f"the run paused again ({pause.id}) after the last scripted reply"
        answer = types.FunctionResponse(id=pause.id, name=pause.name, response={"result": left.pop(0)})
        content = types.Content(role="user", parts=[types.Part(function_response=answer)])
    assert not left, f"replies left over: {left}"

    records = providers.telemetry.sink.records
    steps = list(dict.fromkeys(r["node"] for r in records if r.get("event") == "step.start"))
    assert steps == ["extract_kyc_intake", "look_up_id_registry", "retrieve_country_risk_context", *tail]
    assert not check_trajectory(records, WORKFLOW.graph)
