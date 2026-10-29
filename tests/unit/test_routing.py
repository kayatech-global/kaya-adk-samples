"""Choosing the next edge (label routing with the export's sandboxed conditions, condition routing, defaults) and the
router providers."""

from __future__ import annotations

import pytest

from retail_kyc_onboarding._engine.engine.evaluator import Scope, build_route_function
from retail_kyc_onboarding._engine.engine.plan import END_ROUTE
from retail_kyc_onboarding._engine.engine.routing import allowed_targets, edge_for_target, edge_key, scope_from, select_edge
from retail_kyc_onboarding._engine.factories.registry import REGISTRY
from retail_kyc_onboarding._engine.factories.router import (
    FINISH_WORDS,
    LlmStructuredRouter,
    RouterInput,
    RulesRouter,
    _lenient_json,
)
from retail_kyc_onboarding._engine.model import END, START, Edge, Graph, Node
from retail_kyc_onboarding.workflow import WORKFLOW

GRAPH = WORKFLOW.graph
EMPTY = Scope()


def label_of(node_id: str, result=None, is_error: bool = False, **variables):
    """The label function the engine builds for a node: its sandboxed condition over a result and the run variables."""
    route = build_route_function(WORKFLOW.conditions[GRAPH.node(node_id).condition_id].source)
    return lambda: route(result, is_error, variables)


def route(node_id: str, label_fn=None, scope: Scope = EMPTY):
    return select_edge(GRAPH, GRAPH.node(node_id), scope, label_fn=label_fn)


def small_graph(*edges: Edge, routing: str = "edges") -> Graph:
    nodes = [Node(id=n, kind="agent", routing=routing) for n in ("a", "b", "c")]
    return Graph(id="g", nodes=nodes, edges=[Edge(source=START, target="a"), *edges])


# label routing with the export's own conditions


@pytest.mark.parametrize(
    ("risk", "recommendation", "target"),
    [
        ("HIGH", "APPROVE", "return_rejection_or_stop_decision"),
        ("LOW", "REJECT", "return_rejection_or_stop_decision"),
        ("MEDIUM", "APPROVE WITH EDD", "enhanced_due_diligence_review"),
        ("LOW", "APPROVE", "compliance_approval_review"),
        ("", "", "return_rejection_or_stop_decision"),
    ],
)
def test_the_risk_assessment_routes_by_the_label_its_condition_returns(risk, recommendation, target):
    """The KYC risk and recommendation choose the reject, enhanced or standard review edge through the sandboxed condition."""
    fn = label_of("assess_kyc_risk", **{"kyc_risk_agent.kyc_risk": risk, "kyc_risk_agent.recommendation": recommendation})
    decision = route("assess_kyc_risk", fn)
    assert decision.edge.target == target
    assert decision.reason == f"label '{decision.edge.label}'" and not decision.errors
    assert decision.key == edge_key(GRAPH.outgoing("assess_kyc_risk").index(decision.edge))


@pytest.mark.parametrize(
    ("reply", "target"),
    [
        ("Approve", "create_active_account"),
        ("Stop - source of funds not shown", "return_rejection_or_stop_decision"),
        ("Which documents do you need?", "enhanced_due_diligence_review"),
    ],
)
def test_an_officer_reply_routes_the_review_to_approve_stop_or_ask_again(reply, target):
    """A review's condition reads the officer's reply: approve goes on, stop ends, anything else loops back to ask again."""
    assert route("enhanced_due_diligence_review", label_of("enhanced_due_diligence_review", reply)).edge.target == target


def test_a_registry_error_or_mismatch_takes_the_hard_reject_edge():
    """A failed lookup and a name mismatch both route to the rejection; a matching active record clears the registry."""
    clear = {
        "id_registry_lookup.registry_id_number": "57-1",
        "id_registry_lookup.registered_name": "Arturo Conroy",
        "kyc_intake_agent.full_name": "Arturo Conroy",
        "kyc_intake_agent.id_number": "57-1",
        "id_registry_lookup.id_status": "ACTIVE",
    }
    assert route("look_up_id_registry", label_of("look_up_id_registry", **clear)).edge.label == "registry_clear"
    assert route("look_up_id_registry", label_of("look_up_id_registry", is_error=True, **clear)).edge.label == "hard_reject"
    mismatch = {**clear, "kyc_intake_agent.full_name": "Someone Else"}
    assert route("look_up_id_registry", label_of("look_up_id_registry", **mismatch)).edge.label == "hard_reject"


def test_a_label_that_names_a_target_routes_to_that_target():
    """A condition may return a target node id instead of an edge label."""
    decision = route("assess_kyc_risk", lambda: "compliance_approval_review")
    assert decision.edge.label == "standard_review" and decision.reason == "target 'compliance_approval_review'"


def test_a_label_that_matches_no_edge_ends_the_run_and_reports_it():
    """An unknown label on a node with only labelled edges ends the run and says which label failed."""
    decision = route("assess_kyc_risk", lambda: "escalate")
    assert (decision.key, decision.edge, decision.reason) == (END_ROUTE, None, "no matching edge")
    assert decision.errors == ["condition returned 'escalate', which matches no outgoing edge"]


def test_a_condition_that_returns_none_ends_a_node_without_a_default_edge():
    """A label function that returns None (a failing condition) finds no default edge and ends without a recorded error."""
    decision = route("create_active_account", lambda: None)
    assert (decision.key, decision.reason, decision.errors) == (END_ROUTE, "no matching edge", [])


def test_a_default_labelled_edge_to_the_end_finishes_the_run():
    """A label node falls back to its default edge when the label matches nothing; a default edge to the end is the end route."""
    graph = small_graph(Edge(source="a", target="b", label="go"), Edge(source="a", target=END, label="default"), routing="label")
    decision = select_edge(graph, graph.node("a"), EMPTY, label_fn=lambda: "anything")
    assert (decision.key, decision.edge.target, decision.reason) == (END_ROUTE, END, "default edge")
    assert decision.errors == ["condition returned 'anything', which matches no outgoing edge"]


def test_an_only_labelled_edge_is_taken_when_the_label_does_not_match():
    """A label node with a single labelled edge takes it when the label matches nothing."""
    graph = small_graph(Edge(source="a", target="b", label="go"), routing="label")
    decision = select_edge(graph, graph.node("a"), EMPTY, label_fn=lambda: "other")
    assert (decision.key, decision.edge.target, decision.reason) == ("e0", "b", "only edge")


def test_a_label_node_without_a_label_function_falls_back_to_edge_rules():
    """Without a condition function a label node takes its first unconditional edge."""
    decision = route("assess_kyc_risk")
    assert (decision.key, decision.edge.label, decision.reason) == ("e0", "reject", "first unconditional edge")


def test_a_node_without_outgoing_edges_ends():
    """A node with nothing after it ends the run."""
    graph = small_graph(Edge(source="a", target="b"))
    assert select_edge(graph, graph.node("b"), EMPTY).reason == "no outgoing edge"


# condition routing (canvas order)


def conditional_graph() -> Graph:
    return small_graph(
        Edge(source="a", target="b", condition="score > 5"),
        Edge(source="a", target="c"),
        Edge(source="a", target=END, condition="done == true"),
    )


def test_the_first_true_condition_wins_over_the_plain_edge():
    """The first edge whose condition holds is chosen before any plain edge."""
    graph = conditional_graph()
    decision = select_edge(graph, graph.node("a"), scope_from({"score": 7}))
    assert (decision.key, decision.edge.target, decision.reason) == ("e0", "b", "condition 'score > 5'")


def test_a_true_condition_on_an_edge_to_the_end_finishes():
    """A later true condition on the edge to the end is taken as the end route."""
    graph = conditional_graph()
    decision = select_edge(graph, graph.node("a"), scope_from({"score": 1, "done": True}))
    assert (decision.key, decision.edge.target) == (END_ROUTE, END)


def test_the_first_plain_edge_is_taken_when_no_condition_holds():
    """With every condition false the first unconditional edge is chosen."""
    graph = conditional_graph()
    decision = select_edge(graph, graph.node("a"), scope_from({"score": 1, "done": False}))
    assert (decision.key, decision.edge.target, decision.reason) == ("e1", "c", "first unconditional edge")


def test_a_failing_condition_is_reported_and_routing_continues():
    """A condition that cannot be evaluated counts as false and its error is kept for the trace."""
    graph = small_graph(Edge(source="a", target="b", condition="score > 5 && ok"), Edge(source="a", target="c"))
    decision = select_edge(graph, graph.node("a"), scope_from({"score": 9, "ok": True}))
    assert decision.edge.target == "c"
    assert decision.errors == ["'score > 5 && ok': use 'and' / 'or' instead of '&&' / '||'"]


def test_compat_mode_reads_the_source_platform_spelling():
    """Compat mode accepts the source platform's words (AND, true) where strict mode does not need them."""
    graph = small_graph(Edge(source="a", target="b", condition="score > 5 AND ok == true"), Edge(source="a", target="c"))
    decision = select_edge(graph, graph.node("a"), scope_from({"score": 9, "ok": True}), "compat")
    assert decision.edge.target == "b" and not decision.errors


def test_the_first_target_is_taken_when_every_edge_is_conditional_and_false():
    """With only false conditions the first target is chosen as the last resort."""
    graph = small_graph(Edge(source="a", target="b", condition="x == 1"), Edge(source="a", target="c", condition="x == 2"))
    decision = select_edge(graph, graph.node("a"), scope_from({"x": 3}))
    assert (decision.key, decision.edge.target, decision.reason) == ("e0", "b", "first target")


def test_a_scope_reads_variables_before_custom_state():
    """The routing scope looks names up in the variables first, then in custom state."""
    scope = scope_from({"x": 1}, {"x": 2, "y": 3})
    assert (scope.lookup("x"), scope.lookup("y")) == ((True, 1), (True, 3))
    assert scope_from({}).custom_state == {}


# routing to a named target (supervisors)


def test_routing_to_a_named_target_picks_its_edge():
    """A supervisor's choice maps to the edge that leads to that node."""
    node = GRAPH.node("assess_kyc_risk")
    decision = edge_for_target(GRAPH, node, "enhanced_due_diligence_review")
    assert (decision.key, decision.edge.label, decision.errors) == ("e1", "edd_review", [])
    assert decision.reason == "chose enhanced_due_diligence_review"


def test_finishing_takes_the_edge_to_the_end_when_there_is_one():
    """No target means finish: the edge to the end when the node has one, else no edge."""
    done = edge_for_target(GRAPH, GRAPH.node("summarize_opened_account"), None)
    assert (done.key, done.edge.target, done.reason) == (END_ROUTE, END, "finish")
    bare = edge_for_target(GRAPH, GRAPH.node("assess_kyc_risk"), None)
    assert (bare.key, bare.edge, bare.reason) == (END_ROUTE, None, "finish")


def test_an_unknown_target_ends_the_run_with_an_error():
    """A target that is not an outgoing node ends the run and reports why."""
    decision = edge_for_target(GRAPH, GRAPH.node("assess_kyc_risk"), "create_active_account")
    assert (decision.key, decision.edge) == (END_ROUTE, None)
    assert decision.errors == ["'create_active_account' is not an outgoing target"]


def test_the_allowed_targets_leave_out_the_end():
    """A node's allowed targets are its outgoing nodes without the end marker."""
    assert allowed_targets(GRAPH, GRAPH.node("summarize_opened_account")) == []
    registry = allowed_targets(GRAPH, GRAPH.node("look_up_id_registry"))
    assert registry == ["return_rejection_or_stop_decision", "retrieve_country_risk_context"]


# router providers


def router_input(**kw) -> RouterInput:
    values = {"node_id": "supervisor", "allowed": ["b", "c"], "turn": 1, "max_turns": 3, "variables": {}, **kw}
    return RouterInput(**values)


def test_both_router_providers_are_registered_under_their_names():
    """The router kind offers llm_structured, which needs the model, and rules, which does not."""
    assert REGISTRY.resolve("router", "llm_structured") is LlmStructuredRouter
    assert REGISTRY.resolve("router", "rules") is RulesRouter
    assert LlmStructuredRouter.requires_model and not RulesRouter.requires_model


def test_the_structured_router_asks_the_model_for_an_allowed_node_or_finish():
    """The schema offers the allowed nodes plus FINISH and a message; the rules router asks for no schema."""
    schema = LlmStructuredRouter().output_schema(["b", "c"])
    assert schema == {"next": {"type": "enum", "values": ["b", "c", "FINISH"]}, "message": {"type": "string"}}
    assert RulesRouter().output_schema(["b"]) is None


@pytest.mark.parametrize(
    ("answer", "next_node", "reason"),
    [
        ({"next": "b", "message": "go"}, "b", "go"),
        ('{"next": "c", "message": ""}', "c", ""),
        ('Sure:\n```json\n{"next": "b", "message": "fenced"}\n```', "b", "fenced"),
        ({"next": "finish", "message": "all done"}, None, "all done"),
        ({"next": "__end__"}, None, "finish"),
        ({"next": "z"}, "__invalid__", "model chose 'z', which is not in ['b', 'c']"),
        ("no json here", "__invalid__", "model chose '', which is not in ['b', 'c']"),
        (None, "__invalid__", "model chose '', which is not in ['b', 'c']"),
    ],
)
async def test_the_structured_router_reads_the_model_answer(answer, next_node, reason):
    """Plain, fenced or dict answers pick an allowed node, finish words end, and anything else is the invalid sentinel."""
    choice = await LlmStructuredRouter().choose(router_input(model_output=answer))
    assert (choice.next_node, choice.reason) == (next_node, reason)


def test_lenient_json_ignores_arrays_and_broken_objects():
    """A JSON array or a broken object in the answer yields an empty dict."""
    assert _lenient_json("[1, 2]") == {}
    assert _lenient_json("text {not json} more") == {}
    assert _lenient_json('prefix {"next": "b"} suffix') == {"next": "b"}


RULES = [
    {"when": "kyc_risk_agent.kyc_risk == 'HIGH'", "next": "FINISH"},
    {"when": "score > 5", "next": "b"},
]


@pytest.mark.parametrize(
    ("variables", "settings", "next_node", "reason"),
    [
        ({"kyc_risk_agent.kyc_risk": "HIGH", "score": 9}, {"rules": RULES}, None, "rule: kyc_risk_agent.kyc_risk == 'HIGH'"),
        ({"kyc_risk_agent.kyc_risk": "LOW", "score": 9}, {"rules": RULES}, "b", "rule: score > 5"),
        ({"kyc_risk_agent.kyc_risk": "LOW", "score": 1}, {"rules": RULES, "default": "c"}, "c", "default"),
        ({"score": 1}, {"rules": RULES}, None, "default"),
        ({}, {"rules": [{"next": "b"}]}, None, "default"),
        ({}, {}, None, "default"),
    ],
)
async def test_the_rules_router_takes_the_first_true_rule_else_the_default(variables, settings, next_node, reason):
    """The first rule whose condition holds wins; a FINISH target, no rule or a rule without ``when`` falls to the default."""
    choice = await RulesRouter().choose(router_input(variables=variables, settings=settings))
    assert (choice.next_node, choice.reason) == (next_node, reason)


async def test_the_rules_router_default_comes_from_its_provider_settings():
    """Without a default in the node's settings the provider's own ``default`` setting applies."""
    choice = await RulesRouter({"default": "c"}).choose(router_input(settings={"rules": []}))
    assert (choice.next_node, choice.reason) == ("c", "default")
    assert RulesRouter().settings.default == "FINISH" and "FINISH" in FINISH_WORDS
