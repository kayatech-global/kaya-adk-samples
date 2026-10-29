"""The export behaves as the Kaya plan does on the platform: routing, reviews, partner agent, tool mappings, step context."""

import json
from types import SimpleNamespace

import pytest

from retail_kyc_onboarding._engine.engine.evaluator import analyse_snippet, build_route_function
from retail_kyc_onboarding._engine.engine.executors.common import declines, readable, reply_hint, with_conversation
from retail_kyc_onboarding._engine.engine.executors.external_agent import compose_message
from retail_kyc_onboarding._engine.engine.state import RunState
from retail_kyc_onboarding._engine.engine.templates import apply_output_mapping
from retail_kyc_onboarding._engine.model import NodeKind
from retail_kyc_onboarding._engine.model.nodes import InterruptSide
from retail_kyc_onboarding.workflow import WORKFLOW

NODES = {n.id: n for n in WORKFLOW.graph.nodes}
REGISTRY = {
    "id_registry_lookup.registry_id_number": "88-151470-757212-3",
    "id_registry_lookup.registered_name": "Joann Armstrong",
    "kyc_intake_agent.full_name": "joann  ARMSTRONG",
    "kyc_intake_agent.id_number": "88 151470 757212 3",
    "id_registry_lookup.id_status": "ACTIVE",
    "id_registry_lookup.sanctions_hit": "false",
}


def route(condition_id: str):
    return build_route_function(WORKFLOW.conditions[condition_id].source)


def test_every_plan_condition_is_exported():
    """No step falls back to its first edge: the five Kaya conditions all route by label."""
    assert not WORKFLOW.identity.exclusions
    routed = {n.id for n in WORKFLOW.graph.nodes if n.routing == "label"}
    assert routed == {
        "look_up_id_registry",
        "assess_kyc_risk",
        "compliance_approval_review",
        "enhanced_due_diligence_review",
        "create_active_account",
    }
    assert routed == set(WORKFLOW.conditions)


@pytest.mark.parametrize(
    ("change", "is_error", "label"),
    [
        ({}, False, "registry_clear"),
        ({}, True, "hard_reject"),
        ({"kyc_intake_agent.full_name": "John Smith"}, False, "hard_reject"),
        ({"id_registry_lookup.id_status": "INACTIVE"}, False, "hard_reject"),
        ({"id_registry_lookup.sanctions_hit": "yes"}, False, "hard_reject"),
        ({"id_registry_lookup.registry_id_number": "", "id_registry_lookup.registered_name": ""}, False, "hard_reject"),
        ({"kyc_intake_agent.id_number": "", "kyc_intake_agent.full_name": ""}, False, "hard_reject"),
        ({"kyc_intake_agent.id_number": "null"}, False, "hard_reject"),
        ({"kyc_intake_agent.id_number": "88-151470"}, False, "hard_reject"),
    ],
)
def test_the_registry_check_rejects_as_kaya_does(change: dict, is_error: bool, label: str):
    """The registry condition (it imports re for its name normaliser) runs in the sandbox."""
    assert route("look_up_id_registry")(None, is_error, {**REGISTRY, **change}) == label


@pytest.mark.parametrize("review", ["compliance_approval_review", "enhanced_due_diligence_review"])
@pytest.mark.parametrize(
    ("reply", "label"),
    [("Approve", "approve"), ("go ahead", "approve"), ("Stop", "stop"), ("not approved", "stop"), ("why?", "followup")],
)
def test_a_review_routes_on_the_officers_reply(review: str, reply: str, label: str):
    assert route(review)(reply, False, {}) == label


@pytest.mark.parametrize(
    "snippet",
    [
        "import os\nnext = 'a'",
        "import re as r\nnext = 'a'",
        "import re\nnext = re.compile('a')",
        "import re\nf = re.sub\nnext = 'a'",
    ],
)
def test_the_sandbox_still_refuses_other_modules_and_module_attributes(snippet: str):
    assert analyse_snippet(snippet).errors


def test_review_steps_ask_through_their_worker():
    """Kaya dispatches a follow-up step to its worker, which writes the question with its own prompt, model and review gate."""
    for review in ("compliance_approval_review", "enhanced_due_diligence_review"):
        node = NODES[review]
        assert node.kind == NodeKind.FOLLOWUP
        agent = node.config["agent"]
        assert agent["prompt"] == "kyc_risk_agent_prompt" and agent["model"] == "gemini_3_8_flash"
        assert agent["task"] == node.config["message"]
        assert agent["interrupt"]["before"]["enabled"]


def test_country_risk_is_the_world_bank_partner_agent():
    node = NODES["retrieve_country_risk_context"]
    assert node.kind == NodeKind.EXTERNAL_AGENT
    assert node.config["url"] == "https://agentbazaar.tech/a2a/connectors/worldbank"
    assert node.config["output_prefix"] == "world_bank_country_risk_agent"
    assert "gemini_gemini_3_5_flash" not in WORKFLOW.models


def test_the_partner_agent_gets_the_template_then_the_task():
    template = '{"country":"{{Variable:id_registry_lookup.nationality_code}}"}'
    assert compose_message(template, "Get GDP growth.", {"id_registry_lookup.nationality_code": "US"}) == (
        '{"country":"US"}\n\nGet GDP growth.'
    )
    assert compose_message("Ask: {{Variable:kaya.message}}", "Get GDP growth.", {}) == "Ask: Get GDP growth."


@pytest.mark.parametrize("node_id", ["look_up_id_registry", "create_active_account"])
def test_tool_steps_keep_their_request_and_output_mappings(node_id: str):
    config = NODES[node_id].config
    assert config["request_structures"] and config["output_structures"]


@pytest.mark.parametrize(
    ("reply", "stops"), [("proceed", False), ("Proceed", False), ("yes", False), ("ok", True), ("stop", True)]
)
def test_a_review_gate_proceeds_only_on_an_affirmative_reply(reply: str, stops: bool):
    side = InterruptSide(enabled=True, actions=[{"label": "Proceed", "value": "proceed"}, {"label": "Stop", "value": "stop"}])
    assert declines(reply, side) is stops


def test_a_step_sees_the_conversation_and_its_task():
    """Like Kaya's message history: what the person typed, earlier tasks and outputs, then this step's task."""
    state = RunState({})
    state.add_message("user", "Open a Savings Account for Joann Armstrong")
    state.add_message("step", '{"full_name": "Joann Armstrong"}', "Extract KYC intake")
    sc = SimpleNamespace(state=state, node=SimpleNamespace(title="Look up ID registry"))
    text = with_conversation(sc, "Use the registry tool.")
    assert text.startswith("Conversation so far:\nUser: Open a Savings Account for Joann Armstrong")
    assert 'Extract KYC intake: {"full_name": "Joann Armstrong"}' in text
    assert text.endswith("Current task:\nUse the registry tool.")
    assert state.conversation()[-1] == {"role": "task", "name": "Look up ID registry", "text": "Use the registry tool."}


def test_the_registry_answer_reaches_the_variables():
    """The registry answers with an array of matching customers; the first one is mapped (the plan mapped nothing)."""
    body = [
        {
            "full_name": "Arturo Conroy",
            "id_number": "57-607128-154274-1",
            "dob": "1996-03-23",
            "address": "McKenzie Crest",
            "id_status": "ACTIVE",
        }
    ]
    found: dict = {}
    mapping = NODES["look_up_id_registry"].config["output_structures"]["retail_kyc_id_registry_lookup"]
    apply_output_mapping(mapping, body, found.__setitem__)
    assert found == {
        "id_registry_lookup.registered_name": "Arturo Conroy",
        "id_registry_lookup.registry_id_number": "57-607128-154274-1",
        "id_registry_lookup.dob": "1996-03-23",
        "id_registry_lookup.address": "McKenzie Crest",
        "id_registry_lookup.id_status": "ACTIVE",
    }
    applicant = {"kyc_intake_agent.full_name": "Arturo Conroy", "kyc_intake_agent.id_number": "57-607128-154274-1"}
    assert route("look_up_id_registry")(None, False, {**found, **applicant}) == "registry_clear"


def test_a_review_gate_says_what_each_reply_does():
    side = InterruptSide(enabled=True, actions=[{"label": "Proceed", "value": "proceed"}, {"label": "Stop", "value": "stop"}])
    assert reply_hint(side) == 'Reply "Proceed" to continue, or "Stop" to stop the run.'
    assert reply_hint(InterruptSide(enabled=True)) == 'Reply "Proceed" to continue; any other reply stops the run.'


def test_the_review_question_reads_as_text_not_json():
    """The worker answers with its structured output; the officer sees the summary, the short fields and the reasons."""
    answer = {
        "kyc_risk": "LOW",
        "reasons": ["ID status is ACTIVE", "Name matches the registry"],
        "recommendation": "APPROVE",
        "summary": "Applicant Arturo Conroy, ID 57-607128-154274-1, Savings Account. Reply Approve or Stop.",
    }
    reasons = "Reasons:\n- ID status is ACTIVE\n- Name matches the registry"
    assert readable(json.dumps(answer)) == f"{answer['summary']}\n\nKYC risk: LOW\nRecommendation: APPROVE\n\n{reasons}"
    assert readable("plain text stays as it is") == "plain text stays as it is"


@pytest.mark.parametrize("reply", ["Proceed", "1.50", "null", "[draft]", '{"note": "x"}'])
def test_a_free_text_reply_reaches_the_step_unchanged(reply: str):
    """ADK json-parses a "result" string on resume; the run host encodes free text so the step gets exactly what was typed."""
    from google.adk.workflow.utils._rehydration_utils import _unwrap_response

    from retail_kyc_onboarding._engine.runtime.adk_v2.adapter import _content_for
    from retail_kyc_onboarding._engine.runtime.port import ResumeInput

    content = _content_for(None, ResumeInput(interrupt_id="step:ask:1", kind="input", response=reply))
    assert _unwrap_response(content.parts[0].function_response.response) == reply
