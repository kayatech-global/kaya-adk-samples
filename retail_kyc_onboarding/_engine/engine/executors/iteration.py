"""Iteration node: run a body (usually a Sub-IFLOW or an agent) once per item, sequentially or in parallel.

Each item runs as a run of its own in a derived session, so items cannot see each other's state. The body may not wait for a person:
there is nobody to ask once many items run at the same time.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import uuid
from typing import Any

from ...model import END, START, Edge, Graph, Node
from ...model.nodes import IterationConfig
from ...runtime.port import SESSION_USER
from ..engine import iteration_body
from ..plan import NodePlan, Phase, PhaseResult
from ..step import StepCtx
from ..units import ContentUnit
from .common import enter, finish

_REF = re.compile(r"^\s*(?:\{\{\s*Variable:([\w.]+)\s*\}\}|Variable:([\w.]+)|@([\w.]+)|\{([\w.]+)\})\s*$")


def resolve_items(cfg: IterationConfig, variables: dict[str, Any]) -> list[Any]:
    """Resolve the iteration input to a list: a literal list, a variable reference, or JSON text.

    Raises:
        ValueError: The input does not resolve to a list.
    """
    raw = cfg.array_input
    if isinstance(raw, list):
        return raw
    m = _REF.match(raw or "")
    value: Any = raw
    if m:
        name = next(g for g in m.groups() if g)
        value = variables.get(name)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise ValueError(f"the iteration input is not a list: {value[:80]!r}") from exc
    if not isinstance(value, list):
        raise ValueError(f"the iteration input '{raw}' is not a list (got {type(value).__name__})")
    return value


def body_graph(node: Node, body: list[Node]) -> Graph:
    """Wrap the body nodes in a linear graph, start to end, so they run as a unit of their own."""
    edges = [Edge(source=START, target=body[0].id)]
    edges += [Edge(source=a.id, target=b.id) for a, b in zip(body, body[1:], strict=False)]
    edges.append(Edge(source=body[-1].id, target=END))
    return Graph(id=f"{node.id}__body", name=f"{node.title} body", nodes=body, edges=edges)


def build(engine: Any, unit: ContentUnit, node: Node) -> NodePlan:
    """Plan an iteration node: run the body once per item and collect the outcomes in order.

    Never pauses. A body that needs a person fails that item, because many items may run at once.
    """
    cfg = IterationConfig.model_validate(node.config)
    body = iteration_body(node)
    body_unit = dataclasses.replace(unit, graph=body_graph(node, body)) if body else unit

    async def run(sc: StepCtx) -> PhaseResult:
        await enter(sc)
        if not body:
            return finish(sc, json.dumps({"error": "the iteration has no body"}), is_error=True)
        variables = sc.variables()
        try:
            items = resolve_items(cfg, variables)
        except ValueError as exc:
            sc.tracer.emit("iteration.error", node=node.id, error=str(exc))
            return finish(sc, json.dumps({"error": str(exc)}), is_error=True)
        counter = sc.state.counter(f"exec:{node.id}")
        limit = asyncio.Semaphore(max(1, cfg.max_workers) if cfg.parallel_execution else 1)
        stop = asyncio.Event()
        sc.tracer.emit("iteration.start", node=node.id, items=len(items), parallel=cfg.parallel_execution)

        async def one(idx: int, item: Any) -> dict[str, Any]:
            async with limit:
                if stop.is_set():
                    return {"index": idx, "status": "skipped", "output": None}
                session = str(uuid.uuid5(uuid.NAMESPACE_URL, f"flow-iteration:{sc.session_id}:{node.id}:{counter}:{idx}"))
                child_vars = {**variables, cfg.item_variable_name: item, cfg.index_variable_name: idx}
                result = await sc.engine.runtime.run_unit(
                    body_unit,
                    session_id=session,
                    user_id=SESSION_USER,
                    message=item if isinstance(item, str) else json.dumps(item, default=str),
                    variables=child_vars,
                    principal=sc.principal,
                    meta={"run_id": sc.state.run().get("run_id", ""), "lineage": list(sc.state.run().get("lineage", []))},
                )
                if result.status == "done":
                    return {"index": idx, "status": "completed", "output": _parsed(result.output)}
                err = result.error or "the iteration body asked for a person; bodies cannot wait"
                if not cfg.continue_on_error:
                    stop.set()
                return {"index": idx, "status": "failed", "error": err, "output": None}

        outcomes = await asyncio.gather(*(one(i, it) for i, it in enumerate(items)))
        outcomes = sorted(outcomes, key=lambda o: o["index"])
        failed = [o for o in outcomes if o["status"] == "failed"]
        sc.state.set_variables(
            {
                f"{node.id}_results": [o.get("output") for o in outcomes],
                f"{node.id}_errors": len(failed),
                f"{node.id}_output": json.dumps(outcomes, default=str),
            }
        )
        sc.tracer.emit("iteration.end", node=node.id, items=len(items), failed=len(failed))
        is_error = bool(failed) and (not cfg.continue_on_error or len(failed) == len(items))
        return finish(sc, json.dumps(outcomes, default=str), is_error=is_error)

    return NodePlan(node, [Phase("run", run)])


def _parsed(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text
