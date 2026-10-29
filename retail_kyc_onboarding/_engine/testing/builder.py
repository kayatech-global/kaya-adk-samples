"""Build small exports from Python objects: typed content in memory (``make_content``) and graph dicts (``graph``, ``node``, ``edge``)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..content import Condition, ExportContent, ExportIdentity, Prompt, digest
from ..datatypes import ModelSpec, ToolDef
from ..export.layout import ExportRef, SourceInfo, TargetInfo
from ..model import Graph

DEFAULT_MODEL = {"provider": "openai", "model_name": "gpt-4o-mini", "temperature": 0.0}


def make_content(
    graph: dict[str, Any] | Graph,
    *,
    export_id: str = "test",
    version: str = "export-0001",
    prompts: dict[str, str] | None = None,
    models: dict[str, dict[str, Any]] | None = None,
    apis: dict[str, dict[str, Any]] | None = None,
    mcp: dict[str, dict[str, Any]] | None = None,
    rag: dict[str, dict[str, Any]] | None = None,
    conditions: dict[str, str] | None = None,
    children: dict[str, tuple[str, dict[str, Any]]] | None = None,
    source_kind: str = "iflow",
    core_requires: str = "",
    secrets: list[str] | None = None,
) -> ExportContent:
    """Typed content with every digest computed, as the exporter writes it. Entities are at version ``v1``.

    ``conditions`` maps a condition id (or ``<id>.py``) to its module source; ``children`` maps a child id to
    ``(version, keyword arguments for make_content)``.
    """
    g = graph if isinstance(graph, Graph) else Graph.model_validate(graph)

    def sealed(obj: Any) -> Any:
        return replace(obj, sha256=digest(obj))

    tools: dict[str, ToolDef] = {}
    for kind, given in (("api", apis), ("mcp", mcp), ("rag", rag)):
        for name, spec in (given or {}).items():
            data = {k: v for k, v in spec.items() if k in ToolDef.__dataclass_fields__ and k not in ("name", "type", "version")}
            tools[name] = sealed(ToolDef(name=name, type=kind, version="v1", **data))
    kids = {cid: make_content(**{"export_id": cid, "version": cver, **ckw}) for cid, (cver, ckw) in (children or {}).items()}
    identity = ExportIdentity(
        export=ExportRef(id=export_id, version=version),
        source=SourceInfo(kind=source_kind, workflow_id=export_id),
        target=TargetInfo(adapter="adk_v2", adk="2.12.0"),
        core_requires=core_requires,
        runtime_requirements={"secrets": sorted(secrets or [])},
    )
    conds = {k.removesuffix(".py"): v for k, v in (conditions or {}).items()}
    return ExportContent(
        identity=identity,
        graph=g,
        version=version,
        prompts={pid: sealed(Prompt(pid, "v1", text)) for pid, text in (prompts or {}).items()},
        models={
            mid: sealed(ModelSpec(**{k: v for k, v in spec.items() if k in ModelSpec.__dataclass_fields__} | {"id": mid, "version": "v1"}))
            for mid, spec in (models or {}).items()
        },
        tools=tools,
        conditions={cid: sealed(Condition(cid, src)) for cid, src in conds.items()},
        children=kids,
        graph_sha256=digest(g),
    )


def node(node_id: str, kind: str, **config: Any) -> dict[str, Any]:
    """Build a node dict; ``routing`` and ``condition_id`` are lifted out of the config, and the name defaults to the id."""
    out: dict[str, Any] = {"id": node_id, "kind": kind, "name": config.pop("name", node_id)}
    routing = config.pop("routing", None)
    condition_id = config.pop("condition_id", None)
    out["config"] = config
    if routing:
        out["routing"] = routing
    if condition_id:
        out["condition_id"] = condition_id
    return out


def edge(source: str, target: str, label: str | None = None, condition: str | None = None) -> dict[str, Any]:
    """Build an edge dict."""
    return {"source": source, "target": target, "label": label, "condition": condition}


def graph(
    graph_id: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    variables: list[dict[str, Any]] | None = None,
    source_kind: str = "iflow",
) -> dict[str, Any]:
    """Build a graph dict with ``version`` set to ``1`` and the id as its name."""
    return {
        "id": graph_id,
        "name": graph_id,
        "version": "1",
        "source_kind": source_kind,
        "nodes": nodes,
        "edges": edges,
        "variables": variables or [],
    }
