"""Agent node: an ADK LlmAgent phase between a prepare phase and a finish phase."""

from __future__ import annotations

import json
from typing import Any

from ...model import Node
from ...model.nodes import AgentConfig
from ..plan import AgentSpec, NodePlan, Phase, PhaseResult
from ..render import render, stringify
from ..schemas import decode_open_fields
from ..state import RunState
from ..step import StepCtx
from ..units import ContentUnit
from .common import declines, enter, finish, gate_pause, gate_triggered, halt_phase, is_stop, safe_name, with_conversation


class _Blocked(Exception):
    pass


async def _redact(value: Any, checker: Any, ids: list[str]) -> Any:
    """Run the output guardrail over every string anywhere in a structured answer; one blocked string blocks the whole answer."""
    if isinstance(value, str):
        part = await checker.check_output(value, ids)
        if not part.allowed:
            raise _Blocked(part.reason or "blocked")
        return part.text
    if isinstance(value, dict):
        return {k: await _redact(v, checker, ids) for k, v in value.items()}
    if isinstance(value, list):
        return [await _redact(v, checker, ids) for v in value]
    return value


def build(engine: Any, unit: ContentUnit, node: Node, *, then_ask: bool = False) -> NodePlan:
    """Plan an agent node: an LLM call between a prepare and a post phase.

    Pauses for a person only when the interrupt setting is on: before the model (the ``gate`` phase) or after it, so the
    reply can add feedback.
    A reply to the gate that is not affirmative ends the run, as in Kaya. With ``then_ask`` the node does not route after the
    post phase: a follow-up step appends its own phases that show the answer to the user and wait for the reply.
    """
    cfg = AgentConfig.model_validate(node.config)
    handles = engine.tool_defs_for(unit, cfg.tools)

    async def instruction(readonly_ctx: Any) -> str:
        variables = RunState(readonly_ctx.state).variables()
        text = render((await engine.prompt(unit, cfg.prompt)).text if cfg.prompt else "", variables).text
        if cfg.structured_output.enabled and cfg.structured_output.instructions:
            text += "\n\n" + render(cfg.structured_output.instructions, variables).text
        return text

    schema = dict(cfg.structured_output.output_schema) if cfg.structured_output.enabled and cfg.structured_output.output_schema else None

    async def pre_gate(sc: StepCtx) -> PhaseResult:
        await enter(sc)
        if await gate_triggered(sc, cfg.interrupt.before, model_ref=cfg.model):
            return PhaseResult(output=sc.node_input, pause=gate_pause(sc, cfg.interrupt.before, "before"))
        return PhaseResult(output=sc.node_input)

    async def prep(sc: StepCtx) -> PhaseResult:
        if not cfg.interrupt.before.enabled:
            await enter(sc)
        if sc.reply is not None and declines(sc.reply, cfg.interrupt.before):
            return PhaseResult(output="Stopped by the user.", route="stop")
        variables = sc.variables()
        if cfg.task:
            text = with_conversation(sc, render(cfg.task, variables).text)
        else:
            text = sc.input_text() or sc.state.last()
        manual = [h for h in handles if h.definition.manual_execution]
        for h in manual:
            margs = {p: variables[p] for p in h.definition.parameters if p in variables}
            decision = await sc.engine.guard.check(
                sc.principal, "tool.call", {"id": h.name, "type": h.definition.type, "node": node.id}, tracer=sc.tracer
            )
            if decision.effect == "deny":
                text += f"\n\nResult of {h.name}: not allowed ({decision.reason or 'denied by policy'})"
                continue
            req = await sc.engine.approvals.requirement(h.definition, margs, sc.principal, decision, variables)
            if req.required:  # a pre-step cannot wait for an approver, so a tool that needs approval is not run here
                sc.tracer.emit("tool.skipped", node=node.id, tool=h.name, reason="manual tool needs approval")
                text += f"\n\nResult of {h.name}: not run, approval required"
                continue
            res = await h.invoke(margs, sc.call(h.name))
            sc.tracer.emit("tool.call", node=node.id, tool=h.name, manual=True, ok=res.ok, status=res.status)
            text += f"\n\nResult of {h.name}: {res.as_text()[:4000]}"
        if cfg.guardrails:
            checked = await sc.engine.factories.guardrail.check_input(text, cfg.guardrails)
            sc.tracer.emit("guardrail.input", node=node.id, allowed=checked.allowed, reason=checked.reason, guardrails=cfg.guardrails)
            if not checked.allowed:
                sc.state.update_run(guardrail={**sc.state.run().get("guardrail", {}), node.id: checked.reason or "blocked"})
                return PhaseResult(output=text, route="blocked")
            text = checked.text
        return PhaseResult(output=text)

    async def post(sc: StepCtx) -> PhaseResult:
        out = sc.node_input
        if schema and isinstance(out, dict):
            out = decode_open_fields(schema, out)
        text = json.dumps(out, sort_keys=True, default=str) if isinstance(out, dict) else stringify(out)
        if cfg.guardrails:
            # the guardrail runs first and everything below is derived from what it lets through: redaction reaches variables too
            checker = sc.engine.factories.guardrail
            checked = await checker.check_output(text, cfg.guardrails)
            reason, allowed = checked.reason, checked.allowed
            if allowed and isinstance(out, (dict, list)):
                try:
                    out = await _redact(out, checker, cfg.guardrails)
                    text = json.dumps(out, sort_keys=True, default=str)
                except _Blocked as blocked:
                    allowed, reason = False, str(blocked)
            elif allowed:
                out, text = checked.text, checked.text
            sc.tracer.emit("guardrail.output", node=node.id, allowed=allowed, reason=reason, guardrails=cfg.guardrails)
            if not allowed:
                sc.state.update_run(guardrail={**sc.state.run().get("guardrail", {}), node.id: reason or "blocked"})
                return PhaseResult(output=text, route="blocked")
        updates: dict[str, Any] = {}
        if schema and isinstance(out, dict):
            prefix = cfg.structured_output.variable_prefix or node.id
            for k, v in out.items():
                updates[f"{prefix}.{k}"] = v
        if cfg.expose_output_as_variable:
            updates[cfg.output_variable or f"{node.id}_output"] = text
        sc.state.set_variables(updates)
        if updates:
            sc.tracer.emit("variables.set", node=node.id, names=sorted(updates))
        if cfg.interrupt.after.enabled:
            sc.state.set_output(node.id, text)
            if await gate_triggered(sc, cfg.interrupt.after, text, cfg.model):
                return PhaseResult(output=out, pause=gate_pause(sc, cfg.interrupt.after, "after", text))
            return PhaseResult(output=out)
        return finish(sc, text, route=not then_ask)

    async def after(sc: StepCtx) -> PhaseResult:
        if sc.reply is not None and stringify(sc.reply).strip():
            sc.state.set_variable(f"{node.id}_feedback", stringify(sc.reply))
        sc.node_input = sc.state.raw.get("flow_outputs", {}).get(node.id, "")
        if sc.reply is not None and is_stop(sc.reply):
            sc.state.set_variable(f"{node.id}_is_error", False)
            sc.tracer.emit("step.end", node=node.id, kind="agent", output=sc.node_input[:2000], is_error=False, stopped=True)
            return PhaseResult(output=sc.node_input, route="__end__")
        return finish(sc, sc.node_input)

    spec = AgentSpec(
        name=safe_name(node.id), model_ref=cfg.model, instruction=instruction, tools=handles, output_schema=schema, scope=unit.scope
    )
    phases: list[Phase] = []
    pauses: dict[str, str] = {}
    if cfg.interrupt.before.enabled:
        phases.append(Phase("gate", pre_gate))
        pauses["gate"] = "prep"
    phases.append(Phase("prep", prep))
    phases.append(Phase("agent", agent=spec))
    phases.append(Phase("post", post))
    if cfg.interrupt.after.enabled:
        phases.append(Phase("after", after))
        pauses["post"] = "after"
    loops: dict[str, dict[str, str]] = {}
    side: list[Phase] = []
    if cfg.interrupt.before.enabled:
        loops.setdefault("prep", {})["stop"] = "halt"
        side.append(halt_phase(node.id))
    if cfg.guardrails:

        async def blocked(sc: StepCtx) -> PhaseResult:
            reason = sc.state.run().get("guardrail", {}).get(node.id, "blocked")
            sc.state.set_variable(f"{node.id}_guardrail_blocked", True)
            return finish(sc, f"Blocked by guardrail: {reason}", is_error=True)

        loops.setdefault("prep", {})["blocked"] = "blocked"
        loops.setdefault("post", {})["blocked"] = "blocked"
        side.append(Phase("blocked", blocked, exits=True))
    return NodePlan(node, phases, pauses, loops, side)
