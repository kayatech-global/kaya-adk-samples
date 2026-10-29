"""Tool executor node: runs configured tools without a model, with per-tool approval and data mapping.

Phases: ``gate`` (optional before-interrupt), ``approvals`` (one approval per tool that needs it, looping until all are
answered), ``run``. ``run`` refuses to call a tool whose approval was not recorded, so no path skips the approval.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ...datatypes import Principal, ToolResult
from ...errors import PolicyDenied
from ...model import Node
from ...model.nodes import ToolNodeConfig
from ..plan import NodePlan, Pause, Phase, PhaseResult
from ..render import render
from ..step import StepCtx
from ..templates import apply_output_mapping, parse_request
from ..units import ContentUnit
from .common import declines, enter, finish, gate_pause, gate_triggered, halt_phase

QUEUE = "approval_queue"


def tool_args(cfg: ToolNodeConfig, name: str, sc: StepCtx) -> dict[str, Any]:
    """Arguments for a tool call: the request structure, else the node input as a dict or JSON text, else empty."""
    template = _structure_for(cfg.request_structures, name)
    if template is not None:
        return parse_request(template, sc.variables())
    if isinstance(sc.node_input, dict):
        return dict(sc.node_input)
    text = sc.input_text().strip()
    if text.startswith("{"):
        try:
            return dict(json.loads(text))
        except ValueError:
            return {}
    return {}


def _structure_for(structures: dict[str, Any], tool_name: str) -> Any:
    """Exact name first, then the MCP server prefix (request structures are keyed by server name)."""
    if tool_name in structures:
        return structures[tool_name]
    for key, value in structures.items():
        if tool_name.startswith(key + "_"):
            return value
    return None


def build(engine: Any, unit: ContentUnit, node: Node) -> NodePlan:
    """Plan a tool node: run the configured tools without a model.

    Pauses for approval, one tool at a time, and optionally on a before-interrupt (``gate``). The run phase refuses any tool whose approval is not recorded.
    """
    cfg = ToolNodeConfig.model_validate({**node.config, **(node.config.get("tool_executor_config") or {})})
    handles = engine.tool_defs_for(unit, cfg.tools)
    by_name = {h.name: h for h in handles}

    async def gate(sc: StepCtx) -> PhaseResult:
        await enter(sc)
        if await gate_triggered(sc, cfg.interrupt.before):
            return PhaseResult(output=sc.node_input, pause=gate_pause(sc, cfg.interrupt.before, "before"))
        return PhaseResult(output=sc.node_input)

    def _queue(sc: StepCtx) -> dict[str, Any]:
        return dict(sc.state.run().get(QUEUE, {}).get(node.id) or {})

    def _save_queue(sc: StepCtx, data: dict[str, Any] | None) -> None:
        run = sc.state.run()
        queues = dict(run.get(QUEUE, {}))
        if data is None:
            queues.pop(node.id, None)
        else:
            queues[node.id] = data
        sc.state.update_run(**{QUEUE: queues})

    async def approvals(sc: StepCtx) -> PhaseResult:
        """Decide which tools need approval, then ask for them one at a time (this phase re-enters itself after each answer)."""
        if not cfg.interrupt.before.enabled and sc.reply is None and not _queue(sc):
            await enter(sc)
        state_q = _queue(sc)
        if sc.reply is not None and not state_q and declines(sc.reply, cfg.interrupt.before):
            return PhaseResult(output="Stopped by the user.", route="stop")
        if not state_q:
            needing: list[str] = []
            for h in handles:
                args = tool_args(cfg, h.name, sc)
                decision = await sc.engine.guard.require(
                    sc.principal, "tool.call", {"id": h.name, "type": h.definition.type, "node": node.id}, tracer=sc.tracer
                )
                req = await sc.engine.approvals.requirement(h.definition, args, sc.principal, decision, sc.variables())
                if req.required:
                    needing.append(h.name)
            state_q = {"needing": needing, "approved": [], "open": None}
        if sc.reply is not None and state_q.get("open"):
            reply = sc.reply if isinstance(sc.reply, dict) else {}
            expected = state_q["open"]
            who = reply.get("approver") or {}
            # only a structured answer for the request that is open counts; free text never approves anything
            if reply.get("approval_id") != expected["approval_id"] or "approved" not in reply:
                _save_queue(sc, state_q)
                return PhaseResult(
                    output=sc.node_input,
                    pause=_pause_for(sc, state_q, expected["tool"], expected["args"], expected["approvers"], expected["approval_id"]),
                )
            approved = bool(reply["approved"])
            sc.engine.approvals.resolve(
                sc.state,
                expected["approval_id"],
                approved,
                Principal(who.get("subject", "unknown"), roles=tuple(who.get("roles", ()))),
                str(reply.get("comment", "")),
                sc.tracer,
            )
            state_q["open"] = None
            if not approved:
                _save_queue(sc, None)
                sc.state.set_variable(f"{node.id}_approved", False)
                text = f"Not run: approval for '{expected['tool']}' rejected by {who.get('subject', 'approver')}"
                sc.tracer.emit("tool.skipped", node=node.id, tool=expected["tool"], reason=text)
                sc.state.update_run(skip_run={**sc.state.run().get("skip_run", {}), node.id: True})
                return finish(sc, text, is_error=True)
            state_q["approved"] = [*state_q["approved"], expected["tool"]]
        remaining = [t for t in state_q["needing"] if t not in state_q["approved"]]
        if remaining:
            name = remaining[0]
            h = by_name[name]
            args = tool_args(cfg, name, sc)
            decision = await sc.engine.guard.require(
                sc.principal, "tool.call", {"id": name, "type": h.definition.type, "node": node.id}, tracer=sc.tracer
            )
            req = await sc.engine.approvals.requirement(h.definition, args, sc.principal, decision, sc.variables())
            seq = sc.state.counter(f"approval:{node.id}") + 1
            sc.state.set_counter(f"approval:{node.id}", seq)
            approval_id = hashlib.sha256(f"{sc.session_id}|{node.id}|{name}|{seq}".encode()).hexdigest()[:24]
            result = await sc.engine.approvals.open(
                approval_id=approval_id,
                tool=h.definition,
                args=args,
                requirement=req,
                requester=sc.principal,
                node_id=node.id,
                state=sc.state,
                tracer=sc.tracer,
            )
            state_q["open"] = {"approval_id": approval_id, "tool": name, "args": args, "approvers": dict(req.approvers)}
            _save_queue(sc, state_q)
            if result.answered:
                who = result.approver or sc.principal
                sc.engine.approvals.resolve(sc.state, approval_id, result.approved, who, result.comment, sc.tracer)
                state_q["open"] = None
                if not result.approved:
                    _save_queue(sc, None)
                    sc.state.set_variable(f"{node.id}_approved", False)
                    text = f"Not run: approval for '{name}' rejected by {who.subject}"
                    sc.tracer.emit("tool.skipped", node=node.id, tool=name, reason=text)
                    return finish(sc, text, is_error=True)
                state_q["approved"] = [*state_q["approved"], name]
                _save_queue(sc, state_q)
                return await approvals(sc)  # next tool, if any
            return PhaseResult(output=sc.node_input, pause=_pause_for(sc, state_q, name, args, dict(req.approvers), approval_id))
        _save_queue(sc, {**state_q, "open": None, "complete": True})
        if state_q["needing"]:
            sc.state.set_variable(f"{node.id}_approved", True)
        return PhaseResult(output=sc.node_input)

    def _pause_for(sc: StepCtx, q: dict[str, Any], tool: str, args: dict[str, Any], approvers: dict[str, Any], approval_id: str) -> Pause:
        return Pause(
            "approval",
            approval_id,
            f"Approval needed to run '{tool}'",
            {"approval_id": approval_id, "tool": tool, "args": args, "approvers": approvers, "node": node.id},
        )

    async def run(sc: StepCtx) -> PhaseResult:
        q = _queue(sc)
        _save_queue(sc, None)
        if sc.state.run().get("skip_run", {}).get(node.id):
            return finish(sc, sc.state.last(), is_error=True)
        needing, approved = set(q.get("needing", [])), set(q.get("approved", []))
        if needing - approved:
            # defense in depth: never call a tool whose approval is not on record
            missing = sorted(needing - approved)
            sc.tracer.emit("tool.skipped", node=node.id, reason=f"approval missing for {missing}")
            return finish(sc, f"Not run: approval missing for {', '.join(missing)}", is_error=True)
        if cfg.task:  # as in Kaya, the step's task is part of the conversation later steps read
            sc.state.add_message("task", render(cfg.task, sc.variables()).text, node.title)
        results: list[tuple[str, ToolResult]] = []
        for h in handles:
            args = tool_args(cfg, h.name, sc)
            await sc.engine.guard.require(
                sc.principal, "tool.call", {"id": h.name, "type": h.definition.type, "node": node.id}, tracer=sc.tracer
            )
            try:
                res = await h.invoke(args, sc.call(h.name))
            except PolicyDenied:
                raise
            except Exception as exc:  # a tool failure is data for routing, not a crashed run
                res = ToolResult(False, error=f"{type(exc).__name__}: {exc}")
            sc.tracer.emit(
                "tool.call",
                node=node.id,
                tool=h.name,
                args=args,
                ok=res.ok,
                status=res.status,
                result=res.as_text()[:4000],
                version=h.definition.version,
                approved=h.name in approved,
            )
            results.append((h.name, res))
            sc.state.set_variable(f"{h.name}_response", res.data if res.data is not None else res.text)
            mapping = _structure_for(cfg.output_structures, h.name)
            if mapping and res.ok:
                apply_output_mapping(mapping, res.data if res.data is not None else res.text, sc.state.set_variable)
            if not res.ok:
                break
        failed = any(not r.ok for _, r in results)
        out = results[-1][1].as_text() if len(results) == 1 else json.dumps({n: r.as_text() for n, r in results})
        return finish(sc, out, is_error=failed)

    phases = []
    pauses: dict[str, str] = {}
    if cfg.interrupt.before.enabled:
        phases.append(Phase("gate", gate))
        pauses["gate"] = "approvals"
    phases.append(Phase("approvals", approvals))
    pauses["approvals"] = "approvals"
    phases.append(Phase("run", run))
    loops = {"approvals": {"stop": "halt"}} if cfg.interrupt.before.enabled else {}
    return NodePlan(node, phases, pauses=pauses, loops=loops, side=[halt_phase(node.id)] if loops else [])
