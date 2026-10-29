"""ADK v2 adapter: builds a google.adk Workflow from a unit's graph and runs it with pause/resume."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from google.adk import Context, Event, Runner
from google.adk.agents.llm_agent import LlmAgent
from google.adk.apps.app import App, ResumabilityConfig
from google.adk.events.event_actions import EventActions
from google.adk.events.request_input import RequestInput
from google.adk.workflow import START, FunctionNode, RetryConfig, Workflow
from google.genai import types

from ...datatypes import ModelSpec, Principal
from ...engine.executors.common import phase_description, safe_name
from ...engine.plan import NodePlan, Phase
from ...engine.schemas import fields_to_model
from ...engine.state import RunState
from ...engine.step import StepCtx
from ...engine.units import ContentUnit
from ...errors import PolicyDenied
from ...factories.model import CURRENT_PRINCIPAL, resolve_model
from ..adk_plugin import KayaTracePlugin
from ..adk_tools import KayaAdkTool
from ..port import SESSION_USER, PauseInfo, ResumeInput, RuntimePort, UnitResult

PAUSE_ROUTE = "__pause__"
NEXT_ROUTE = "next"


def _reachable(graph: Any) -> set[str]:
    seen: set[str] = set()
    frontier = list(graph.start_targets())
    while frontier:
        nid = frontier.pop()
        if nid in seen or nid == "__END__":
            continue
        seen.add(nid)
        frontier.extend(e.target for e in graph.outgoing(nid))
    return seen


def _text_of(value: Any) -> Any:
    if isinstance(value, types.Content):
        return "\n".join(p.text for p in (value.parts or []) if getattr(p, "text", None))
    return value


def _tag(scope: str) -> str:
    return "" if not scope else "c" + hashlib.sha1(scope.encode()).hexdigest()[:6] + "_"  # noqa: S324 (naming only)


class AdkV2Runtime(RuntimePort):
    """RuntimePort for ADK 2.x, built on a google.adk Workflow with native pause and resume."""
    def __init__(self, engine: Any):
        super().__init__(engine)
        self._workflows: dict[str, Workflow] = {}

    # ------------------------------------------------------------------ building

    async def workflow(self, unit: ContentUnit) -> Workflow:
        """The Workflow for a unit's graph, built once per unit and graph."""
        key = f"{unit.scope}|{unit.graph.id}"  # an iteration body is a graph of its own inside the same content unit
        if key not in self._workflows:
            self._workflows[key] = await self._build(unit)
        return self._workflows[key]

    async def _build(self, unit: ContentUnit) -> Workflow:
        engine, graph, tag = self.engine, unit.graph, _tag(unit.scope)
        live = _reachable(graph)  # ADK rejects nodes no path reaches; older exports may still carry some
        plans: dict[str, NodePlan] = {n.id: engine.plan_for(unit, n) for n in graph.nodes if n.id in live}
        adk: dict[tuple[str, str], Any] = {}
        waits: dict[tuple[str, str], Any] = {}

        for node in (n for n in graph.nodes if n.id in live):
            plan = plans[node.id]
            for phase in [*plan.phases, *plan.side]:
                name = f"{tag}{safe_name(node.id)}__{phase.name}"
                adk[(node.id, phase.name)] = await self._make_node(unit, plan, phase, name)
                if phase.name in plan.pauses:
                    waits[(node.id, phase.name)] = FunctionNode(func=_wait_fn(), name=f"{name}__wait")

        edges: list[Any] = []
        for edge in graph.edges:
            if edge.source == "__START__":
                edges.append((START, adk[(edge.target, plans[edge.target].entry)]))
                break
        for node in (n for n in graph.nodes if n.id in live):
            plan = plans[node.id]
            outgoing = graph.outgoing(node.id)
            for i, phase in enumerate([*plan.phases, *plan.side]):
                src = adk[(node.id, phase.name)]
                is_side = i >= len(plan.phases)
                if is_side and not phase.exits:
                    continue  # a terminal side phase (halt) has no outgoing edges
                last = i == len(plan.phases) - 1 or is_side
                routing: dict[str, Any] = {}
                if phase.name in plan.pauses:
                    wait = waits[(node.id, phase.name)]
                    routing[PAUSE_ROUTE] = wait
                    edges.append((wait, adk[(node.id, plan.pauses[phase.name])]))
                for label, target_phase in plan.loops.get(phase.name, {}).items():
                    routing[label] = adk[(node.id, target_phase)]
                if not last:
                    nxt = adk[(node.id, plan.phases[i + 1].name)]
                    if routing:
                        routing[NEXT_ROUTE] = nxt
                    else:
                        edges.append((src, nxt))
                        continue
                else:
                    for idx, e in enumerate(outgoing):
                        if e.target != "__END__":
                            routing[f"e{idx}"] = adk[(e.target, plans[e.target].entry)]
                if routing:
                    edges.append((src, routing))
        return Workflow(name=f"{tag}{safe_name(unit.graph.id or 'export')}", description=unit.graph.name or "", edges=edges)

    async def _make_node(self, unit: ContentUnit, plan: NodePlan, phase: Phase, name: str) -> Any:
        engine = self.engine
        if phase.agent is not None:
            spec = phase.agent
            model = await engine.model_for(unit, spec.model_ref)
            found = resolve_model(unit, spec.model_ref)
            temperature = found.temperature if isinstance(found, ModelSpec) else None  # a client's own model keeps its own settings
            max_tokens = found.max_tokens if isinstance(found, ModelSpec) else None
            gen_cfg = types.GenerateContentConfig(
                **({"response_mime_type": "application/json"} if spec.lenient and spec.output_schema else {}),
                **({"temperature": temperature} if temperature is not None else {}),
                **({"max_output_tokens": max_tokens} if max_tokens else {}),
            )
            agent = LlmAgent(
                name=name,
                description=phase_description(plan.node, phase.name),
                model=model,
                instruction=spec.instruction,
                include_contents="none",
                tools=[KayaAdkTool(h, engine, unit, plan.node.id) for h in spec.tools],
                output_schema=fields_to_model(name, spec.output_schema) if spec.output_schema and not spec.lenient else None,
                generate_content_config=gen_cfg,
            )
            engine.agent_index[name] = (unit, plan.node)
            return agent
        policy = plan.node.policy
        retry = RetryConfig(max_attempts=policy.retry_attempts + 1) if policy.retry_attempts > 0 else None
        return FunctionNode(func=_phase_fn(engine, unit, plan, phase), name=name, retry_config=retry, timeout=policy.timeout_seconds)

    # ------------------------------------------------------------------ running

    async def run_unit(
        self,
        unit: ContentUnit,
        *,
        session_id: str,
        user_id: str,
        message: str | None,
        variables: dict[str, Any],
        principal: Principal,
        resume: ResumeInput | None = None,
        meta: dict[str, Any] | None = None,
    ) -> UnitResult:
        """Run or resume a unit in a session through an ADK Runner.

        A denied or failed run returns status error; a pause is reported from the open input or confirmation request.
        """
        engine, meta = self.engine, meta or {}
        CURRENT_PRINCIPAL.set(principal)
        svc = engine.factories.session.service()
        wf = await self.workflow(unit)
        session = await svc.get_session(app_name=self.app_name, user_id=user_id, session_id=session_id)
        run_id = meta.get("run_id") or str(uuid.uuid4())
        if session is None:
            initial = {v.name: v.default for v in unit.graph.variables if v.default is not None}
            initial.update(variables)
            session = await svc.create_session(
                app_name=self.app_name,
                user_id=user_id,
                session_id=session_id,
                state={
                    "flow_vars": initial,
                    "flow_run": {
                        "session_id": session_id,
                        "run_id": run_id,
                        "principal": principal.to_state(),
                        "user_turns": 0,
                        "user_message": message or "",
                        "lineage": list(meta.get("lineage", [])),
                        "counters": {},
                        "pending": {},
                        "parent_session": meta.get("parent_session"),
                        "unit": unit.scope,
                    },
                },
            )
        if resume is None:
            run = RunState(dict(session.state)).run()
            delta: dict[str, Any] = {}
            vars_now = dict(session.state.get("flow_vars") or {})
            vars_now.update(variables)
            delta["flow_vars"] = vars_now
            run.update(
                user_turns=int(run.get("user_turns", 0)) + 1,
                user_message=message or run.get("user_message", ""),
                run_id=run_id,
                run_counter=int(run.get("run_counter", 0)) + 1,
                principal=principal.to_state(),
            )
            delta["flow_run"] = run
            await svc.append_event(session, Event(author="flow", actions=EventActions(state_delta=delta)))
        tracer = engine.tracer.bind(run_id=run_id, session=session_id, scope=unit.scope or None)
        tracer.emit(
            "run.start" if resume is None else "run.resume",
            unit=unit.graph.id,
            message=(message or "")[:2000],
            principal=principal.public(),
        )
        app = App(
            name=self.app_name,
            root_agent=wf,
            plugins=[KayaTracePlugin(engine), *engine.adk_plugins],
            resumability_config=resumability(),
        )
        runner = Runner(app=app, session_service=svc)
        content = _content_for(message, resume)
        pause: PauseInfo | None = None
        try:
            async for ev in runner.run_async(user_id=user_id, session_id=session_id, new_message=content):
                found = _pause_from_event(ev)
                if found is not None:
                    pause = found
        except PolicyDenied as exc:
            tracer.emit("run.denied", error=str(exc))
            return UnitResult("error", error=str(exc), session_id=session_id)
        except Exception as exc:
            tracer.emit("run.error", error=f"{type(exc).__name__}: {exc}")
            return UnitResult("error", error=f"{type(exc).__name__}: {exc}", session_id=session_id)
        final = await svc.get_session(app_name=self.app_name, user_id=user_id, session_id=session_id)
        state = RunState(dict(final.state)) if final else RunState({})
        if pause is not None:
            pause = self._enrich(pause, state)
            tracer.emit(
                "run.paused", kind=pause.kind, interrupt_id=pause.interrupt_id, message=pause.message, approval_id=pause.approval_id
            )
            return UnitResult("paused", state.last(), state.variables(), pause, session_id=session_id)
        tracer.emit("run.done", output=state.last()[:4000])
        return UnitResult("done", state.last(), state.variables(), session_id=session_id)

    @staticmethod
    def _enrich(pause: PauseInfo, state: RunState) -> PauseInfo:
        pending = state.pending()
        approval_id = pause.approval_id or (pause.interrupt_id if pause.interrupt_id in pending else None)
        if approval_id and approval_id in pending:
            pause.approval_id, pause.approval_record = approval_id, pending[approval_id]
        return pause

    async def pending_pause(self, unit: ContentUnit, session_id: str, user_id: str) -> PauseInfo | None:
        """The newest unanswered input or confirmation request in the session, or None."""
        s = await self.engine.factories.session.service().get_session(app_name=self.app_name, user_id=user_id, session_id=session_id)
        if s is None:
            return None
        open_requests: dict[str, PauseInfo] = {}
        for ev in s.events:
            for part in (ev.content.parts if ev.content else []) or []:
                if getattr(part, "function_response", None) and part.function_response.id in open_requests:
                    open_requests.pop(part.function_response.id)
            found = _pause_from_event(ev)
            if found is not None:
                open_requests[found.interrupt_id] = found
        if not open_requests:
            return None
        return self._enrich(list(open_requests.values())[-1], RunState(dict(s.state)))

    async def pending_sessions(self, unit: ContentUnit, limit: int = 200) -> list[tuple[str, PauseInfo, dict[str, Any]]]:
        """Sessions waiting for an answer, as (session id, pause, run info), scanning at most ``limit`` sessions."""
        svc = self.engine.factories.session.service()
        listed = await svc.list_sessions(app_name=self.app_name, user_id=SESSION_USER)
        out: list[tuple[str, PauseInfo, dict[str, Any]]] = []
        for meta in list(listed.sessions)[:limit]:
            pause = await self.pending_pause(unit, meta.id, SESSION_USER)
            if pause is not None:
                s = await svc.get_session(app_name=self.app_name, user_id=SESSION_USER, session_id=meta.id)
                out.append((meta.id, pause, RunState(dict(s.state)).run() if s else {}))
        return out

    async def session_state(self, unit: ContentUnit, session_id: str, user_id: str) -> dict[str, Any] | None:
        """The session's state, or None when it does not exist."""
        s = await self.engine.factories.session.service().get_session(app_name=self.app_name, user_id=user_id, session_id=session_id)
        return dict(s.state) if s else None


def resumability() -> ResumabilityConfig:
    """ADK's resumability, on: a paused run continues from the invocation that paused, which every pause here needs."""
    return ResumabilityConfig(is_resumable=True)


def _content_for(message: str | None, resume: ResumeInput | None) -> types.Content:
    if resume is None:
        return types.Content(role="user", parts=[types.Part(text=message or "")])
    name = "adk_request_input" if resume.kind == "input" else "adk_request_confirmation"
    if isinstance(resume.response, dict):
        response = resume.response
    else:  # ADK json-parses a "result" string on resume ("1.50" -> 1.5, "null" -> None); encoded, the text comes back exact
        text = resume.response if isinstance(resume.response, str) else json.dumps(resume.response)
        response = {"result": json.dumps(text)}
    return types.Content(
        role="user", parts=[types.Part(function_response=types.FunctionResponse(id=resume.interrupt_id, name=name, response=response))]
    )


def _pause_from_event(ev: Any) -> PauseInfo | None:
    if not ev.content or not ev.content.parts:
        return None
    for part in ev.content.parts:
        fc = getattr(part, "function_call", None)
        if fc is None:
            continue
        args = dict(fc.args or {})
        if fc.name == "adk_request_input":
            payload = args.get("payload") or {}
            return PauseInfo("input", fc.id, str(args.get("message", "")), payload if isinstance(payload, dict) else {"value": payload})
        if fc.name == "adk_request_confirmation":
            conf = args.get("toolConfirmation") or args.get("tool_confirmation") or {}
            payload = conf.get("payload") or {}
            return PauseInfo(
                "confirmation",
                fc.id,
                str(conf.get("hint", "")),
                payload if isinstance(payload, dict) else {},
                approval_id=(payload or {}).get("approval_id"),
            )
    return None


def _wait_fn() -> Any:
    async def wait(node_input: Any, ctx: Context) -> Any:
        aw = RunState(ctx.state).run().get("awaiting") or {}
        return RequestInput(interrupt_id=aw.get("interrupt_id", "input"), message=aw.get("message", ""), payload=aw.get("payload"))

    return wait


def _phase_fn(engine: Any, unit: ContentUnit, plan: NodePlan, phase: Phase) -> Any:
    node = plan.node
    routed = phase.name in plan.pauses or phase.name in plan.loops
    is_last = plan.phases[-1].name == phase.name

    async def run_phase(node_input: Any, ctx: Context) -> Any:
        state = RunState(ctx.state)
        if state.raw.get("flow_vars") is None:  # started by ADK tooling rather than the run host: seed what the host would have
            state.raw["flow_vars"] = {v.name: v.default for v in unit.graph.variables if v.default is not None}
        typed = "".join(p.text or "" for p in ((ctx.user_content.parts or []) if ctx.user_content else []))
        if typed.strip() and state.run().get("conversation_turn") != ctx.invocation_id:  # a new message, not a resume
            state.add_message("user", typed)
            state.update_run(conversation_turn=ctx.invocation_id)
        run = state.run()
        tracer = engine.tracer.bind(run_id=run.get("run_id"), session=run.get("session_id"), scope=unit.scope or None)
        value = _text_of(node_input)
        reply = None
        aw = run.get("awaiting")
        if aw and aw.get("node") == node.id and aw.get("resume") == phase.name:
            reply = value
            state.update_run(awaiting=None)
        sc = StepCtx(engine, unit, node, state, value, tracer, reply)
        res = await phase.fn(sc)  # type: ignore[misc]
        if res.pause is not None:
            p = res.pause
            state.update_run(
                awaiting={
                    "node": node.id,
                    "phase": phase.name,
                    "resume": plan.pauses[phase.name],
                    "interrupt_id": p.interrupt_id,
                    "message": p.message,
                    "payload": p.payload,
                    "kind": p.kind,
                }
            )
            return Event(output=res.output, route=PAUSE_ROUTE)
        route = res.route
        if route is None and routed and not is_last:
            route = NEXT_ROUTE
        if route is not None:
            return Event(output=res.output if res.output is not None else "", route=route)
        return res.output if res.output is not None else ""

    return run_phase
