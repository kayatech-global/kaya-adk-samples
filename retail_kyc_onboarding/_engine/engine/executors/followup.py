"""Follow-up node: ask the user, wait, optionally validate the reply with a model, then continue.

When the plan names a worker agent, that agent writes the question first (its prompt, the conversation and the task, with
its own before-interrupt and structured output), as Kaya dispatches a follow-up step to its worker.
"""

from __future__ import annotations

from typing import Any

from ...model import Node
from ...model.nodes import FollowupConfig
from ..plan import AgentSpec, NodePlan, Pause, Phase, PhaseResult
from ..render import render, stringify
from ..state import OUTPUTS, RunState
from ..step import StepCtx
from ..units import ContentUnit
from . import agent_node
from .common import enter, finish, readable, safe_name


def build(engine: Any, unit: ContentUnit, node: Node) -> NodePlan:
    """Plan a follow-up node: ask the user a question and use the reply as a variable.

    Pauses on the question (the ``ask`` phase). With validation configured, a model judges the reply and an invalid one loops back to ask again with a note.
    """
    cfg = FollowupConfig.model_validate(node.config)
    validation = cfg.validation or None
    writer = None
    if cfg.agent is not None:
        worker = node.model_copy(update={"config": cfg.agent.model_dump(mode="json", exclude_unset=True)})
        writer = agent_node.build(engine, unit, worker, then_ask=True)

    async def ask(sc: StepCtx) -> PhaseResult:
        if sc.reply is None and writer is None:
            await enter(sc)
        run = sc.state.run()
        notes = dict(run.get("followup_note", {}))
        note = notes.pop(node.id, "")
        sc.state.update_run(followup_note=notes)
        if writer is not None:
            message = readable(str(sc.state.raw.get(OUTPUTS, {}).get(node.id) or "") or sc.node_input)
            if not note:  # the question joins the conversation once, not again when an invalid reply is asked again
                sc.state.add_message("step", message, node.title)
        else:
            message = render(cfg.message or cfg.task or f"Please answer to continue ({node.title}).", sc.variables()).text
        if note:
            message = f"{note}\n\n{message}"
        n = sc.state.counter(f"pause:{node.id}") + 1
        sc.state.set_counter(f"pause:{node.id}", n)
        return PhaseResult(output=message, pause=Pause("input", f"{node.id}:ask:{n}", message, {"node": node.id, "kind": "followup"}))

    async def receive(sc: StepCtx) -> PhaseResult:
        reply = stringify(sc.reply if sc.reply is not None else sc.node_input)
        sc.state.set_variable(cfg.output_variable or f"{node.id}_reply", reply)
        sc.state.add_message("user", reply)
        sc.tracer.emit("followup.reply", node=node.id, reply=reply[:2000])
        if not validation:
            return finish(sc, reply)
        return PhaseResult(output=reply)

    phases: list[Phase] = [*(writer.phases if writer else []), Phase("ask", ask), Phase("receive", receive)]
    loops: dict[str, dict[str, str]] = dict(writer.loops) if writer else {}
    if validation:

        async def instruction(readonly_ctx: Any) -> str:
            variables = RunState(readonly_ctx.state).variables()
            base = render((await engine.prompt(unit, validation["prompt"])).text if validation.get("prompt") else "", variables).text
            task = render(str(validation.get("task", "")), variables).text
            return f'{base}\n\nValidate the user\'s reply. {task}\nAnswer JSON: {{"valid": true|false, "message": <what is wrong, if invalid>}}'.strip()

        async def judge(sc: StepCtx) -> PhaseResult:
            from ...factories.router import _lenient_json

            out = _lenient_json(sc.node_input)
            reply = stringify(sc.state.variables().get(cfg.output_variable or f"{node.id}_reply", ""))
            if out.get("valid", True):
                return finish(sc, reply)
            notes = dict(sc.state.run().get("followup_note", {}))
            notes[node.id] = str(out.get("message", "That answer is not valid."))
            sc.state.update_run(followup_note=notes)
            sc.tracer.emit("followup.invalid", node=node.id, message=notes[node.id])
            return PhaseResult(output=reply, route="retry")

        phases.append(
            Phase(
                "validate",
                agent=AgentSpec(
                    name=safe_name(node.id),
                    model_ref=validation.get("model", ""),
                    instruction=instruction,
                    output_schema={"valid": "bool", "message": "str"},
                    lenient=True,
                    scope=unit.scope,
                ),
            )
        )
        phases.append(Phase("judge", judge))
        loops["judge"] = {"retry": "ask"}
    pauses = {**(writer.pauses if writer else {}), "ask": "receive"}
    return NodePlan(node, phases, pauses=pauses, loops=loops, side=list(writer.side) if writer else [])
