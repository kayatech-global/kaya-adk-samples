"""Sub-IFLOW node: runs a bundled, version-pinned child IFLOW in a derived session of its own.

The child is a separate run, so it cannot widen the parent's rights: the principal is inherited and every child step is
authorized as usual. A child that pauses for a person pauses the parent; the answer is forwarded to the child session.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any

from ...model import Node
from ...model.nodes import SubflowConfig
from ...runtime.port import SESSION_USER, ResumeInput
from ..plan import NodePlan, Pause, Phase, PhaseResult
from ..render import render, render_structure
from ..step import StepCtx
from ..units import ContentUnit
from .common import enter, finish


def derive_session(parent_session: str, node_id: str, counter: int) -> str:
    """Deterministic child session id for a parent session, node and execution count."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"flow-subflow:{parent_session}:{node_id}:{counter}"))


def build(engine: Any, unit: ContentUnit, node: Node) -> NodePlan:
    """Plan a Sub-IFLOW node: run a bundled child IFLOW and map its output into variables.

    Pauses whenever the child pauses and forwards the answer to the child. In ``async`` mode it defers to build_async.
    """
    cfg = SubflowConfig.model_validate(node.config.get("sub_workflow_config") or node.config)
    if cfg.execution_mode == "async":
        return build_async(engine, unit, node, cfg)

    async def run(sc: StepCtx) -> PhaseResult:
        run_info = sc.state.run()
        pending_child: dict[str, Any] = dict(run_info.get("children", {})).get(node.id) or {}
        resuming = sc.reply is not None and bool(pending_child)
        if not resuming:
            await enter(sc)
            await sc.engine.guard.require(
                sc.principal, "subflow.run", {"id": cfg.child, "version": cfg.child_version, "node": node.id}, tracer=sc.tracer
            )
            lineage = list(run_info.get("lineage", []))
            if cfg.child in lineage or cfg.child == sc.engine.unit.manifest.export.id:
                sc.state.set_variable(f"{node.id}_status", "failed")
                return finish(sc, json.dumps({"error": f"recursive Sub-IFLOW '{cfg.child}' refused"}), is_error=True)
        child_unit = unit.child(cfg.child)
        variables = sc.variables()
        counter = sc.state.counter(f"exec:{node.id}")
        child_session = pending_child["session"] if resuming else derive_session(sc.session_id, node.id, counter)
        message = None
        resume = None
        child_vars: dict[str, Any] = {}
        if resuming:
            reply = sc.reply
            resume = ResumeInput(pending_child["interrupt_id"], pending_child["kind"], _forward(reply, pending_child))
        else:
            message = render(cfg.input_message, variables).text if cfg.input_message else sc.input_text() or "Run"
            if cfg.pass_current_state:
                child_vars.update(variables)
            child_vars.update(render_structure(_as_dict(cfg.initial_input), variables))
        sc.tracer.emit(
            "subflow.start" if not resuming else "subflow.resume",
            node=node.id,
            child=cfg.child,
            child_version=child_unit.manifest.export.version,
            session=child_session,
        )
        result = await sc.engine.runtime.run_unit(
            child_unit,
            session_id=child_session,
            user_id=SESSION_USER,
            message=message,
            variables=child_vars,
            principal=sc.principal,
            resume=resume,
            meta={
                "parent_session": sc.session_id,
                "parent_node": node.id,
                "lineage": [*run_info.get("lineage", []), cfg.child],
                "run_id": run_info.get("run_id", ""),
            },
        )
        children = dict(sc.state.run().get("children", {}))
        if result.status == "paused" and result.pause is not None:
            p = result.pause
            children[node.id] = {"session": child_session, "interrupt_id": p.interrupt_id, "kind": p.kind}
            sc.state.update_run(children=children)
            if p.approval_id and p.approval_record:
                sc.state.set_pending(p.approval_id, p.approval_record)
            n = sc.state.counter(f"pause:{node.id}") + 1
            sc.state.set_counter(f"pause:{node.id}", n)
            return PhaseResult(
                output=p.message,
                pause=Pause(
                    "approval" if p.approval_id else "input",
                    p.approval_id or f"{node.id}:child:{n}",
                    p.message,
                    {**p.payload, "child": cfg.child, "node": node.id},
                ),
            )
        children.pop(node.id, None)
        sc.state.update_run(children=children)
        ok = result.status == "done"
        sc.state.set_variable(f"{node.id}_status", "completed" if ok else "failed")
        if ok:
            _map_output(sc, node, cfg, result.output, result.variables)
        sc.state.set_variable(f"{node.id}_result", result.output if ok else json.dumps({"error": result.error}))
        sc.tracer.emit("subflow.end", node=node.id, child=cfg.child, status=result.status, output=result.output[:2000], error=result.error)
        return finish(sc, result.output if ok else json.dumps({"error": result.error}), is_error=not ok)

    return NodePlan(node, [Phase("run", run)], pauses={"run": "run"})


def _forward(reply: Any, pending: dict[str, Any]) -> Any:
    """The parent's reply becomes the child's response. Approval answers keep their structure."""
    if pending["kind"] == "confirmation":
        return reply if isinstance(reply, dict) else {"confirmed": str(reply).lower() in {"true", "yes", "approve", "approved"}}
    return reply


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            data = json.loads(value)
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}
    return {}


def _map_output(sc: StepCtx, node: Node, cfg: SubflowConfig, reply: str, child_vars: dict[str, Any]) -> None:
    """Output structure: each KEY names a field of the child's answer ("results" or "reply" = the whole answer,
    "variables.<name>" = a child variable); each VALUE names the PARENT variable to write ("Variable:<name>")."""
    structure = _as_dict(cfg.output_structure)
    if not structure:
        return
    answer = _as_dict(reply)
    updates: dict[str, Any] = {}
    for key, target in structure.items():
        name = _token_name(target)
        if not name:
            continue
        if key in {"results", "reply"}:
            updates[name] = answer if answer else reply
        elif key.startswith("variables."):
            updates[name] = child_vars.get(key[len("variables.") :])
        elif key in answer:
            updates[name] = answer[key]
        elif key in child_vars:
            updates[name] = child_vars[key]
    sc.state.set_variables(updates)


def _token_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    m = re.fullmatch(
        r"\s*(?:\{\{\s*Variable:([A-Za-z_]\w*)\s*\}\}|Variable:([A-Za-z_]\w*)|@([A-Za-z_]\w*)|\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))\s*", value
    )
    return next((g for g in m.groups() if g), None) if m else None


# ---------------------------------------------------------------------------------------------------- asynchronous mode


async def _deliver_when_parked(engine: Any, session_id: str, token: str, result: dict[str, Any]) -> None:
    """The child can finish before the parent has recorded its pause; retry until the parent is waiting, then give up loudly."""
    from ...errors import RunError

    host = engine.host
    if host is None:
        return  # no run host: the result stays in the run store for whoever delivers it
    for _ in range(300):
        try:
            await host.deliver(session_id=session_id, token=token, result=result)
            return
        except RunError:
            await asyncio.sleep(0.05)
    engine.tracer.emit("background.undelivered", session=session_id, token=token[:8])


def build_async(engine: Any, unit: ContentUnit, node: Node, cfg: SubflowConfig) -> NodePlan:
    """Start the child in the background and wait for its result. The parent pauses on an internal token (no person answers it)."""

    async def start(sc: StepCtx) -> PhaseResult:
        await enter(sc)
        await sc.engine.guard.require(
            sc.principal, "subflow.run", {"id": cfg.child, "version": cfg.child_version, "node": node.id, "mode": "async"}, tracer=sc.tracer
        )
        lineage = list(sc.state.run().get("lineage", []))
        if cfg.child in lineage:
            return finish(sc, json.dumps({"error": f"recursive Sub-IFLOW '{cfg.child}' refused"}), is_error=True)
        child_unit = unit.child(cfg.child)
        variables = sc.variables()
        child_vars = dict(variables) if cfg.pass_current_state else {}
        child_vars.update(render_structure(_as_dict(cfg.initial_input), variables))
        message = render(cfg.input_message, variables).text if cfg.input_message else sc.input_text() or "Run"
        counter = sc.state.counter(f"exec:{node.id}")
        child_session = derive_session(sc.session_id, node.id, counter)
        token = uuid.uuid4().hex
        run_info = sc.state.run()
        waits = dict(run_info.get("async_waits", {}))
        waits[node.id] = token
        sc.state.update_run(async_waits=waits)
        store = sc.engine.factories.runstore
        await store.put(token, {"status": "running", "child": cfg.child, "session": child_session})
        parent_session, principal, engine_ = sc.session_id, sc.principal, sc.engine
        meta = {
            "parent_session": parent_session,
            "parent_node": node.id,
            "lineage": [*lineage, cfg.child],
            "run_id": run_info.get("run_id", ""),
        }
        sc.tracer.emit(
            "subflow.start",
            node=node.id,
            child=cfg.child,
            child_version=child_unit.manifest.export.version,
            session=child_session,
            mode="async",
        )

        async def background() -> None:
            try:
                res = await engine_.runtime.run_unit(
                    child_unit,
                    session_id=child_session,
                    user_id=SESSION_USER,
                    message=message,
                    variables=child_vars,
                    principal=principal,
                    meta=meta,
                )
                if res.status == "done":
                    data = {"status": "completed", "output": res.output, "variables": res.variables}
                else:
                    reason = res.error or "an asynchronous child cannot wait for a person"
                    data = {"status": "failed", "error": reason, "output": ""}
            except Exception as exc:
                data = {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "output": ""}
            await store.put(token, {**data, "child": cfg.child, "session": child_session})
            await _deliver_when_parked(engine_, parent_session, token, data)

        engine_.spawn(background(), name=f"async-subflow:{node.id}")
        n = sc.state.counter(f"pause:{node.id}") + 1
        sc.state.set_counter(f"pause:{node.id}", n)
        return PhaseResult(
            output="",
            pause=Pause(
                "input",
                f"{node.id}:await:{n}",
                f"Waiting for '{cfg.child}'",
                {"await_token": token, "internal": True, "node": node.id, "child": cfg.child},
            ),
        )

    async def collect(sc: StepCtx) -> PhaseResult:
        waits = dict(sc.state.run().get("async_waits", {}))
        token = waits.get(node.id, "")
        reply = sc.reply if isinstance(sc.reply, dict) else {}
        if not token or reply.get("await_token") != token:
            # not the result we are waiting for (for example a person typed something): keep waiting on the same token
            n = sc.state.counter(f"pause:{node.id}") + 1
            sc.state.set_counter(f"pause:{node.id}", n)
            return PhaseResult(
                output="",
                pause=Pause(
                    "input",
                    f"{node.id}:await:{n}",
                    f"Waiting for '{cfg.child}'",
                    {"await_token": token, "internal": True, "node": node.id, "child": cfg.child},
                ),
            )
        data = await sc.engine.factories.runstore.get(f"delivered:{token}")
        if data is None:  # nothing was delivered by the system: keep waiting
            n = sc.state.counter(f"pause:{node.id}") + 1
            sc.state.set_counter(f"pause:{node.id}", n)
            return PhaseResult(
                output="",
                pause=Pause(
                    "input",
                    f"{node.id}:await:{n}",
                    f"Waiting for '{cfg.child}'",
                    {"await_token": token, "internal": True, "node": node.id, "child": cfg.child},
                ),
            )
        waits.pop(node.id, None)
        sc.state.update_run(async_waits=waits)
        ok = data.get("status") == "completed"
        sc.state.set_variable(f"{node.id}_status", "completed" if ok else "failed")
        text = str(data.get("output", "")) if ok else json.dumps({"error": data.get("error", "failed")})
        if ok:
            _map_output(sc, node, cfg, text, dict(data.get("variables") or {}))
        sc.state.set_variable(f"{node.id}_result", text)
        sc.tracer.emit("subflow.end", node=node.id, child=cfg.child, status=data.get("status"), output=text[:2000], mode="async")
        return finish(sc, text, is_error=not ok)

    return NodePlan(node, [Phase("start", start), Phase("collect", collect)], pauses={"start": "collect", "collect": "collect"})
