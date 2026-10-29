"""Live evals: every flow of tests/eval/flows.json, run like `adk web` (ADK's Runner, the real model, the registry, the World
Bank agent, the accounts API), checked for the steps taken and the KYC decision.

Opt-in, because they need GOOGLE_API_KEY, call real services and open real accounts in the mock API:
`FLOW_LIVE_EVALS=1 pytest tests/eval -m live`. Decisions come from a model, so a rare different answer is possible.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from google.adk.runners import InMemoryRunner
from google.genai import types

from retail_kyc_onboarding.agent import abuild
from retail_kyc_onboarding.workflow import WORKFLOW

FLOWS = json.loads((Path(__file__).parent / "flows.json").read_text())
STEP_IDS = {n.name: n.id for n in WORKFLOW.graph.nodes}

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("FLOW_LIVE_EVALS"), reason="live evals: set FLOW_LIVE_EVALS=1 (needs GOOGLE_API_KEY)"),
]


async def run_flow(message: str, replies: list[str]) -> dict[str, Any]:
    """One conversation: the message, then each reply to the pause that is open. Returns the session state."""
    _, app = await abuild()
    runner = InMemoryRunner(app=app)
    session = await runner.session_service.create_session(app_name=app.name, user_id="eval")
    content = types.Content(role="user", parts=[types.Part(text=message)])
    replies = list(replies)
    while True:
        pause = None
        async for event in runner.run_async(user_id="eval", session_id=session.id, new_message=content):
            assert not event.error_code, f"{event.author}: {event.error_code}: {event.error_message}"
            for part in (event.content.parts if event.content else []) or []:
                if part.function_call and part.function_call.name == "adk_request_input":
                    pause = part.function_call
        if pause is None:
            break
        assert replies, f"the run paused again ({pause.id}) after the last scripted reply"
        answer = types.FunctionResponse(id=pause.id, name=pause.name, response={"result": replies.pop(0)})
        content = types.Content(role="user", parts=[types.Part(function_response=answer)])
    assert not replies, f"replies left over: {replies}"
    done = await runner.session_service.get_session(app_name=app.name, user_id="eval", session_id=session.id)
    return dict(done.state)


@pytest.mark.parametrize("flow", FLOWS, ids=[f["id"] for f in FLOWS])
async def test_the_flow_takes_the_documented_path(flow: dict[str, Any]):
    state = await run_flow(flow["message"], flow["replies"])
    titles = [m["name"] for m in state.get("flow_conversation", []) if m["role"] == "step"]
    assert [STEP_IDS[t] for t in dict.fromkeys(titles)] == flow["steps"]
    variables = state.get("flow_vars", {})
    if flow["recommendation"] is not None:
        assert variables.get("kyc_risk_agent.recommendation") == flow["recommendation"]
    assert (variables.get("account_creation.account_status") == "ACTIVE") is flow["account_created"]
