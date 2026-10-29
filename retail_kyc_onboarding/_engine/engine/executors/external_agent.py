"""External agent node: call a partner agent over A2A. The partner may ask a question (input-required); that pauses this run."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from ...errors import PolicyDenied
from ...factories.a2a_client import A2aClient
from ...factories.api import AuthHeaders
from ...model import Node
from ...model.nodes import ExternalAgentConfig
from ..plan import NodePlan, Pause, Phase, PhaseResult
from ..render import render, stringify
from ..step import StepCtx
from ..units import ContentUnit
from .common import enter, finish

_MESSAGE_PLACEHOLDER = re.compile(r"\{\{Variable:kaya\.message\}\}|\bVariable:kaya\.message\b|(?<![A-Za-z0-9])@kaya\.message\b")


def compose_message(template: str, incoming: str, variables: dict[str, Any]) -> str:
    """Kaya's one A2A text part: the rendered template, with the incoming text at ``{{Variable:kaya.message}}`` or appended."""
    resolved = render(template, {**variables, "kaya.message": incoming}).text.strip() if template else ""
    if not resolved:
        return incoming
    if _MESSAGE_PLACEHOLDER.search(template) or not incoming:
        return resolved
    return f"{resolved}\n\n{incoming}"


def build(engine: Any, unit: ContentUnit, node: Node) -> NodePlan:
    """Plan an external agent node: send the prompt to a partner agent over A2A and use its reply.

    Pauses for a person when the partner replies input-required or auth-required; the answer is sent back on the same A2A task.
    """
    cfg = ExternalAgentConfig.model_validate(node.config.get("external_agent_config") or node.config)
    auth = AuthHeaders(engine.factories, cfg.authorization, f"external agent '{node.id}'")
    prefix = cfg.output_prefix or node.title.lower().replace(" ", "_")

    async def run(sc: StepCtx) -> PhaseResult:
        waiting = dict(sc.state.run().get("a2a", {})).get(node.id)
        if sc.reply is None or not waiting:
            await enter(sc)
            waiting = None
        await sc.engine.guard.require(sc.principal, "a2a.call", {"id": cfg.url, "node": node.id}, tracer=sc.tracer)
        variables = sc.variables()
        if waiting:
            text = stringify(sc.reply)
        else:
            incoming = render(cfg.task, variables).text if cfg.task else sc.input_text() or sc.state.last()
            if cfg.task:
                sc.state.add_message("task", incoming, node.title)
            text = compose_message(cfg.prompt_template, incoming, variables)
        call = sc.call(cfg.url)
        attempt = 0
        while True:
            try:
                headers = {
                    **await auth.headers(call),
                    **dict((await sc.engine.factories.credentials.get(f"a2a:{node.id}", sc.principal, None)).headers),
                }
                client = A2aClient(
                    cfg.url,
                    headers,
                    timeout=cfg.timeout_ms / 1000,
                    poll_interval=cfg.polling_interval_ms / 1000,
                    poll_attempts=cfg.poll_attempts,
                    transport=getattr(sc.engine, "a2a_transport", None),
                )
                waiting_ids = waiting or {}
                reply = await client.send(text, context_id=waiting_ids.get("context_id"), task_id=waiting_ids.get("task_id"))
                break
            except PolicyDenied:
                raise
            except Exception as exc:
                sc.tracer.emit("a2a.error", node=node.id, error=f"{type(exc).__name__}: {exc}", attempt=attempt + 1)
                if attempt >= cfg.retry_attempts:
                    sc.state.set_variables({f"{prefix}.response": "", f"{prefix}.status": "failed", f"{prefix}.artifacts": []})
                    return finish(sc, f"The partner agent could not be reached: {type(exc).__name__}", is_error=True)
                await asyncio.sleep(min(cfg.retry_max_wait, cfg.retry_min_wait * 2**attempt))
                attempt += 1
        sc.tracer.emit("a2a.reply", node=node.id, state=reply.state, text=reply.text[:2000])
        outputs = {"response": reply.text, "status": reply.state, "artifacts": list(reply.raw.get("artifacts") or [])}
        sc.state.set_variables({f"{prefix}.{k}": v for k, v in outputs.items()})
        a2a = dict(sc.state.run().get("a2a", {}))
        if reply.state in {"input-required", "auth-required"}:
            a2a[node.id] = {"context_id": reply.context_id, "task_id": reply.task_id}
            sc.state.update_run(a2a=a2a)
            n = sc.state.counter(f"pause:{node.id}") + 1
            sc.state.set_counter(f"pause:{node.id}", n)
            return PhaseResult(
                output=reply.text,
                pause=Pause(
                    "input",
                    f"{node.id}:a2a:{n}",
                    reply.text or "The partner agent needs more information.",
                    {"node": node.id, "partner": cfg.url},
                ),
            )
        a2a.pop(node.id, None)
        sc.state.update_run(a2a=a2a)
        return finish(sc, reply.text, is_error=reply.state != "completed")

    return NodePlan(node, [Phase("run", run)], pauses={"run": "run"})
