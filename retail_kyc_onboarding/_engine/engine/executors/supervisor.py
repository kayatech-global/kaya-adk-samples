"""Supervisor (decision node): a generic router. The core owns the loop; a RouterStrategy picks the next node."""

from __future__ import annotations

from typing import Any

from ...factories.registry import REGISTRY
from ...factories.router import RouterInput, RouterStrategy
from ...model import Node
from ...model.nodes import SupervisorConfig
from ..plan import END_ROUTE, AgentSpec, NodePlan, Phase, PhaseResult
from ..render import render
from ..routing import allowed_targets, edge_for_target
from ..state import RunState
from ..step import StepCtx
from ..units import ContentUnit
from .common import enter, safe_name

ROUTING_INSTRUCTIONS = (
    "You are a supervisor. Decide which step runs next based on the request and the progress so far. "
    "Choose exactly one of the allowed next steps, or FINISH when the request is fully handled. "
    'Allowed next steps: {allowed}. Reply with JSON: {{"next": <step or FINISH>, "message": <short note for the user>}}.'
)


def strategy_for(engine: Any, cfg: SupervisorConfig) -> RouterStrategy:
    """The router strategy for a node: the engine's default, or the one the node names (cached on the engine)."""
    current = engine.factories.router
    if cfg.strategy in (current.name, "") or not cfg.strategy:
        return current
    cache = engine.__dict__.setdefault("_router_cache", {})
    if cfg.strategy not in cache:
        cache[cfg.strategy] = REGISTRY.resolve("router", cfg.strategy)(cfg.strategy_settings, engine.factories.router.ctx)
    return cache[cfg.strategy]


def build(engine: Any, unit: ContentUnit, node: Node) -> NodePlan:
    """Plan a supervisor node: pick the next node with a RouterStrategy, within a turn limit and the guard.

    Never pauses. A model-based strategy adds an agent phase between prepare and decide.
    """
    cfg = SupervisorConfig.model_validate(node.config.get("supervisor_config", {}) | node.config)
    strategy = strategy_for(engine, cfg)
    allowed = allowed_targets(unit.graph, node)

    async def prep(sc: StepCtx) -> PhaseResult:
        await enter(sc)
        user_turns = int(sc.state.run().get("user_turns", 1))
        marker_key, count_key = f"{node.id}__supervisor_marker", f"{node.id}__supervisor_turn_count"
        if user_turns > sc.state.counter(marker_key):
            sc.state.set_counter(marker_key, user_turns)
            sc.state.set_counter(count_key, 0)
        sc.state.set_counter(count_key, sc.state.counter(count_key) + 1)
        lines = [f"User request: {sc.state.run().get('user_message', '')}"]
        progress = sc.state.transcript()
        if progress:
            lines.append("Progress so far:")
            lines += [f"- {p['node']}: {p['text'][:500]}" for p in progress[-12:]]
        return PhaseResult(output="\n".join(lines))

    async def instruction(readonly_ctx: Any) -> str:
        variables = RunState(readonly_ctx.state).variables()
        base = render((await engine.prompt(unit, cfg.prompt)).text if cfg.prompt else "", variables).text
        return (base + "\n\n" if base else "") + ROUTING_INSTRUCTIONS.format(allowed=", ".join(allowed) or "(none)")

    async def decide(sc: StepCtx) -> PhaseResult:
        count = sc.state.counter(f"{node.id}__supervisor_turn_count")
        choice = await strategy.choose(
            RouterInput(
                node.id,
                allowed,
                count,
                cfg.max_turns,
                sc.variables(),
                sc.node_input if strategy.requires_model else None,
                {"rules": cfg.rules, **cfg.strategy_settings},
            )
        )
        target, reason = choice.next_node, choice.reason
        forced = False
        if target == "__invalid__":
            target, forced = cfg.fallback, True
        elif count > cfg.max_turns and target is not None:
            target, forced, reason = None, True, f"turn limit {cfg.max_turns} reached"
        if target is not None and target not in allowed:
            reason, target, forced = f"'{target}' is not an allowed next step", None, True
        decision = await sc.engine.guard.check(
            sc.principal, "route.choose", {"id": node.id, "target": target or "FINISH"}, tracer=sc.tracer
        )
        if decision.effect == "deny":
            target, forced, reason = None, True, f"route denied: {decision.reason}"
        route = edge_for_target(unit.graph, node, target)
        sc.state.set_variable(f"{node.id}_message", choice.reason if not reason else reason)
        sc.tracer.emit(
            "route",
            node=node.id,
            supervisor=True,
            strategy=strategy.name,
            chosen=target or "FINISH",
            reason=reason,
            turn=count,
            max_turns=cfg.max_turns,
            forced=forced,
        )
        sc.tracer.emit("step.end", node=node.id, kind="supervisor", output=target or "FINISH", is_error=False)
        sc.state.set_output(node.id, f"supervisor: {target or 'FINISH'}")
        return PhaseResult(output=target or "FINISH", route=route.key if route.key else END_ROUTE)

    phases = [Phase("prep", prep)]
    if strategy.requires_model:
        schema = strategy.output_schema(allowed)
        phases.append(
            Phase(
                "agent",
                agent=AgentSpec(
                    name=safe_name(node.id),
                    model_ref=cfg.model,
                    instruction=instruction,
                    output_schema=schema,
                    lenient=True,
                    scope=unit.scope,
                ),
            )
        )
    phases.append(Phase("decide", decide))
    return NodePlan(node, phases)
