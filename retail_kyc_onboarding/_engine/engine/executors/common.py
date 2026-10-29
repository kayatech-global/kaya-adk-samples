"""Helpers shared by node executors."""

from __future__ import annotations

import json
import re
from typing import Any

from ...model import NodeKind
from ...model.nodes import InterruptSide
from ..plan import END_ROUTE, Pause, PhaseResult
from ..render import render, stringify
from ..routing import RouteDecision, select_edge
from ..step import StepCtx


def phase_description(node: Any, phase: str) -> str:
    """What an ADK agent for one phase of a step is, in the step's own words. Shown in ADK traces (``gen_ai.agent.description``) and by tooling."""
    return f"{node.title} ({phase} phase of the {node.kind.value} step)"


def safe_name(value: str) -> str:
    """Turn an id into a valid ADK agent name, prefixing ``n_`` when it would start with a digit."""
    out = re.sub(r"[^A-Za-z0-9_]", "_", value)
    return out if out and not out[0].isdigit() else f"n_{out}"


async def enter(sc: StepCtx) -> None:
    """Authorize and record the start of a node. Raises PolicyDenied on deny."""
    sc.tracer.emit("step.start", node=sc.node.id, title=sc.node.title, kind=sc.node.kind.value, input=sc.input_text()[:2000])
    count = sc.state.counter(f"exec:{sc.node.id}") + 1
    sc.state.set_counter(f"exec:{sc.node.id}", count)
    await sc.engine.guard.require(
        sc.principal,
        "node.enter",
        {"id": sc.node.id, "kind": sc.node.kind.value, "export": sc.engine.unit.manifest.export.id},
        tracer=sc.tracer,
    )


def _compare(left: Any, op: str, right: str) -> bool:
    """Numeric comparison first, lexicographic otherwise (VARIABLE_CONDITION)."""
    a: Any
    b: Any
    try:
        a, b = float(left), float(right)
    except (TypeError, ValueError):
        a, b = str(left), str(right)
    return {"==": a == b, "!=": a != b, "<": a < b, ">": a > b, ">=": a >= b, "<=": a <= b}[op]


async def gate_triggered(sc: StepCtx, side: InterruptSide, output_text: str = "", model_ref: str = "") -> bool:
    """Interrupt criteria. A failing criterion obeys its on_error setting (default: interrupt, fail closed)."""
    if not side.enabled:
        return False
    c = side.criteria
    if c is None or c.type == "ALWAYS":
        return True
    try:
        if c.type in ("OUTPUT_CONTAINS", "OUTPUT_NOT_CONTAINS"):
            pattern = c.value or ""
            hit = bool(re.search(pattern, output_text, re.I)) if c.use_regex else pattern.lower() in output_text.lower()
            return hit if c.type == "OUTPUT_CONTAINS" else not hit
        if c.type == "VARIABLE_CONDITION":
            return _compare(sc.variables().get(c.variable_name or ""), c.operator or "==", c.threshold or "")
        if c.type == "EXECUTION_COUNT":
            return sc.state.counter(f"exec:{sc.node.id}") >= int(c.count or 1)
        if c.type == "LLM_EVALUATION":
            return await llm_judge(sc, c.llm_prompt or "", output_text or sc.input_text(), model_ref)
        sc.tracer.emit("interrupt.unsupported", node=sc.node.id, criteria=c.type)
        return c.on_error == "interrupt"
    except Exception as exc:
        sc.tracer.emit("interrupt.error", node=sc.node.id, criteria=c.type, error=f"{type(exc).__name__}: {exc}")
        return c.on_error == "interrupt"


async def llm_judge(sc: StepCtx, question: str, text: str, model_ref: str = "") -> bool:
    """Ask a model a yes/no question about ``text`` (LLM_EVALUATION). The node's own model is used, else the export's first model."""
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types

    ref = model_ref or next(iter(sc.engine.unit.manifest.entities.get("models", {})), "")
    if not ref:
        raise RuntimeError("no model is available to evaluate the interrupt criterion")
    llm = await sc.engine.model_for(sc.unit, ref)
    request = LlmRequest(
        model=llm.model,
        contents=[
            types.Content(
                role="user", parts=[types.Part(text=f"{question}\n\nAnswer with the single word yes or no.\n\nText:\n{text[:8000]}")]
            )
        ],
    )
    answer = ""
    async for response in llm.generate_content_async(request, stream=False):
        answer += "".join(p.text or "" for p in (response.content.parts if response.content else []))
    sc.tracer.emit("interrupt.llm_evaluation", node=sc.node.id, answer=answer[:50])
    return answer.strip().lower().startswith(("yes", "true"))


def is_stop(reply: Any) -> bool:
    """The built-in 'Stop' quick reply ends the run."""
    return stringify(reply).strip().lower() == "stop"


# Kaya's affirmative replies to a review: any other reply to a before-interrupt stops the run, as in Kaya
PROCEED_REPLIES = frozenset(
    {"yes", "y", "sure", "absolutely", "definitely", "of course", "certainly", "indeed", "affirmative", "correct", "go ahead"}
    | {"proceed", "approved", "approve", "accept", "accepted", "continue"}
)


def declines(reply: Any, side: InterruptSide) -> bool:
    """Whether a reply to a before-interrupt stops the step: a configured quick reply counts by its value, free text
    proceeds only when it is one of Kaya's affirmative replies."""
    text = stringify(reply).strip().lower()
    for action in side.actions or []:
        if text in (str(action.get("value", "")).strip().lower(), str(action.get("label", "")).strip().lower()):
            return text not in PROCEED_REPLIES
    return text not in PROCEED_REPLIES


_SMALL_WORDS = {"a", "an", "and", "as", "at", "by", "for", "in", "of", "on", "or", "the", "to"}


def _label(key: str) -> str:
    words = key.replace("_", " ").replace("-", " ").split()
    out = [w.upper() if len(w) <= 3 and w not in _SMALL_WORDS else w for w in words]
    return " ".join(out)[:1].upper() + " ".join(out)[1:]


def readable(answer: Any) -> str:
    """A structured answer as text a person can read: prose first, then ``Label: value`` lines, then bulleted lists.
    Anything that is not a JSON object is returned as it is."""
    data = answer
    if isinstance(answer, str):
        try:
            data = json.loads(answer)
        except ValueError:
            return answer
    if not isinstance(data, dict):
        return stringify(answer)
    paragraphs, lines, lists = [], [], []
    for key, value in data.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, list):
            lists.append(f"{_label(key)}:\n" + "\n".join(f"- {stringify(v)}" for v in value))
        elif isinstance(value, str) and len(value.split()) > 8:  # prose, not a short value
            paragraphs.append(value.strip())
        else:
            lines.append(f"{_label(key)}: {stringify(value)}")
    return "\n\n".join(p for p in ["\n\n".join(paragraphs), "\n".join(lines), "\n\n".join(lists)] if p)


def with_conversation(sc: StepCtx, task: str) -> str:
    """A plan step's input: the conversation so far (as Kaya hands a worker the run's messages), then the step's task.
    The task joins the conversation for the steps after this one."""
    lines = []
    for m in sc.state.conversation():
        who = {"user": "User", "task": f"Task ({m.get('name') or 'step'})"}.get(m["role"], m.get("name") or "Step")
        lines.append(f"{who}: {m['text']}")
    sc.state.add_message("task", task, sc.node.title)
    if not lines:
        return task
    return "Conversation so far:\n" + "\n\n".join(lines) + f"\n\nCurrent task:\n{task}"


def reply_hint(side: InterruptSide) -> str:
    """What a reply to a before-interrupt does, from its quick replies and Kaya's rule (see ``declines``)."""
    actions = [str(a.get("label") or a.get("value") or "") for a in side.actions or [] if a.get("label") or a.get("value")]
    if not actions:
        return 'Reply "Proceed" to continue; any other reply stops the run.'
    go = " or ".join(f'"{a}"' for a in actions if not declines(a, side))
    stop = " or ".join(f'"{a}"' for a in actions if declines(a, side))
    parts = [*([f"{go} to continue"] if go else []), *([f"{stop} to stop the run"] if stop else [])]
    return "Reply " + ", or ".join(parts) + "."


def gate_pause(sc: StepCtx, side: InterruptSide, where: str, output_text: str = "") -> Pause:
    """Build the input Pause for an interrupt, with a per-node counter in the id so each pause is distinct.

    A before-interrupt names the step it guards and says which replies continue, so the person knows what is asked.
    """
    message = render(side.message or f"Input needed at step '{sc.node.title}'.", sc.variables()).text
    if where == "before":
        message = f"{message}\n\nNext step: {sc.node.title}. {reply_hint(side)}"
    n = sc.state.counter(f"pause:{sc.node.id}") + 1
    sc.state.set_counter(f"pause:{sc.node.id}", n)
    return Pause("input", f"{sc.node.id}:{where}:{n}", message, {"node": sc.node.id, "where": where, "output": output_text[:4000]})


def _as_result(output: Any) -> Any:
    if isinstance(output, str) and output.strip()[:1] in "{[":
        try:
            return json.loads(output)
        except ValueError:
            return output
    return output


def decide_route(sc: StepCtx, result: Any = None) -> RouteDecision:
    """Pick the outgoing edge for a finished node and record why. ``result`` feeds plan condition functions."""
    fn = sc.engine.route_fn(sc.unit, sc.node)
    label_fn = None
    if fn is not None:
        out = _as_result(result if result is not None else sc.node_input)
        is_error = bool(sc.variables().get(f"{sc.node.id}_is_error", False))

        def label_fn() -> str | None:
            try:
                value = fn(out, is_error, sc.variables())
            except Exception as exc:  # a failing condition never crashes the run; the trace says why and routing fails closed
                sc.tracer.emit("condition.error", node=sc.node.id, error=f"{type(exc).__name__}: {exc}")
                return None
            return None if value is None else str(value)

    decision = select_edge(sc.unit.graph, sc.node, sc.scope(), sc.engine.conditions_mode, label_fn=label_fn)
    for err in decision.errors:
        sc.tracer.emit("condition.error", node=sc.node.id, error=err)
    sc.tracer.emit("route", node=sc.node.id, chosen=decision.edge.target if decision.edge else END_ROUTE, reason=decision.reason)
    return decision


def finish(sc: StepCtx, output: Any, *, route: bool = True, is_error: bool = False) -> PhaseResult:
    """Common tail of a node: remember the output, then route (unless this phase is not the last)."""
    text = stringify(output)
    sc.state.set_output(sc.node.id, text)
    sc.state.add_transcript(sc.node.id, text)
    if sc.node.kind != NodeKind.FOLLOWUP:  # a follow-up's output is the person's reply, recorded as theirs
        sc.state.add_message("step", text, sc.node.title)
    sc.tracer.emit("step.end", node=sc.node.id, kind=sc.node.kind.value, output=text[:4000], is_error=is_error)
    if not route:
        return PhaseResult(output=output)
    sc.state.set_variable(f"{sc.node.id}_is_error", is_error)
    decision = decide_route(sc, output)
    return PhaseResult(output=output, route=decision.key)


def halt_phase(node_id: str) -> Any:
    """Terminal phase reached when a person answers a review with the built-in 'Stop' reply."""
    from ..plan import Phase

    async def halt(sc: StepCtx) -> PhaseResult:
        sc.tracer.emit("step.end", node=node_id, kind=sc.node.kind.value, output="stopped by user", is_error=False)
        sc.state.set_output(node_id, "Stopped by the user.")
        return PhaseResult(output="Stopped by the user.")

    return Phase("halt", halt)
