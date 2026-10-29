"""The typed content of an export, and the only engine module its generated content files import.

An export's workflow, prompts, models, tools and conditions are Python constants built from these types, so they are checked by
mypy and completed by an IDE, and they are validated when the package is imported, before a model sees them.
``digest`` is the one content hash: the exporter stores it on each constant and the engine recomputes it to tell an edited or
overridden entity from the exported one.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .datatypes import ModelSpec, PromptSpec, ToolDef
from .export.layout import FORMAT_VERSION, ExportRef, SourceInfo, TargetInfo
from .model import (
    END,
    START,
    AgentConfig,
    ApprovalSettings,
    BranchConfig,
    Edge,
    ExternalAgentConfig,
    FollowupConfig,
    Graph,
    InterruptConfig,
    InterruptCriteria,
    InterruptSide,
    IterationConfig,
    Node,
    NodeKind,
    Policy,
    StructuredOutput,
    SubflowConfig,
    SupervisorConfig,
    ToolNodeConfig,
    VariableSpec,
)

__all__ = [
    "END",
    "START",
    "AgentConfig",
    "ApprovalSettings",
    "BranchConfig",
    "Condition",
    "Edge",
    "ExportContent",
    "ExportIdentity",
    "ExportRef",
    "ExternalAgentConfig",
    "FollowupConfig",
    "Graph",
    "GuardrailSpec",
    "InterruptConfig",
    "InterruptCriteria",
    "InterruptSide",
    "IterationConfig",
    "ModelSpec",
    "Node",
    "NodeConfig",
    "NodeKind",
    "Policy",
    "Prompt",
    "SecretUse",
    "SourceInfo",
    "StructuredOutput",
    "SubflowConfig",
    "SupervisorConfig",
    "TargetInfo",
    "ToolDef",
    "ToolNodeConfig",
    "VariableSpec",
    "digest",
    "edge",
    "node",
]

Prompt = PromptSpec

NodeConfig = (
    AgentConfig | ToolNodeConfig | SupervisorConfig | BranchConfig | FollowupConfig | SubflowConfig | IterationConfig | ExternalAgentConfig
)

_KIND_OF: dict[type[BaseModel], NodeKind] = {
    AgentConfig: NodeKind.AGENT,
    ToolNodeConfig: NodeKind.TOOL,
    SupervisorConfig: NodeKind.SUPERVISOR,
    BranchConfig: NodeKind.BRANCH,
    FollowupConfig: NodeKind.FOLLOWUP,
    SubflowConfig: NodeKind.SUBFLOW,
    IterationConfig: NodeKind.ITERATION,
    ExternalAgentConfig: NodeKind.EXTERNAL_AGENT,
}

CONFIG_CLASS: dict[NodeKind, type[BaseModel]] = {kind: cls for cls, kind in _KIND_OF.items()}  # node kind -> its config class

TOOL_KINDS = {"api": "apis", "mcp": "mcp", "rag": "rag"}  # ToolDef.type -> entity kind in the manifest


class ExportIdentity(BaseModel):
    """What an export is and where it came from. The build artifacts of the manifest (file hashes, engine digest) are not part of it."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    format: int = FORMAT_VERSION
    export: ExportRef
    source: SourceInfo
    target: TargetInfo = Field(default_factory=TargetInfo)
    core_requires: str = ""
    runtime_requirements: dict[str, Any] = Field(default_factory=dict)
    exclusions: list[str] = Field(default_factory=list)
    recovered: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class Condition:
    """A generated branch condition: the checked source of a module whose ``route(variables)`` returns one of ``labels``."""

    id: str
    source: str
    labels: tuple[str, ...] = ()
    sha256: str = ""


@dataclass(frozen=True)
class GuardrailSpec:
    """A guardrail definition as data, for a guardrail provider to interpret."""

    id: str
    version: str = ""
    name: str = ""
    config: Mapping[str, Any] = field(default_factory=dict)
    sha256: str = ""


@dataclass(frozen=True)
class SecretUse:
    """A logical secret name the export needs, never its value."""

    name: str
    kind: str = "secret"
    used_by: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExportContent:
    """One unit of an export (the root flow or a pinned child flow) with everything it refers to, keyed by entity id."""

    identity: ExportIdentity
    graph: Graph
    version: str = ""  # the unit's version: the export version for the root, the pinned child version (``v4``) for a child
    prompts: Mapping[str, Prompt] = field(default_factory=dict)
    models: Mapping[str, ModelSpec] = field(default_factory=dict)
    tools: Mapping[str, ToolDef] = field(default_factory=dict)
    conditions: Mapping[str, Condition] = field(default_factory=dict)
    guardrails: Mapping[str, GuardrailSpec] = field(default_factory=dict)
    secrets: tuple[SecretUse, ...] = ()
    children: Mapping[str, ExportContent] = field(default_factory=dict)
    graph_sha256: str = ""  # digest of ``graph`` computed by the exporter

    @property
    def entities(self) -> dict[str, dict[str, str]]:
        """Entity kind -> id -> version, in the shape ``manifest.json`` records it. Kinds without entities are left out."""
        out: dict[str, dict[str, str]] = {
            "prompts": {k: p.version for k, p in sorted(self.prompts.items())},
            "models": {k: m.version for k, m in sorted(self.models.items())},
            "guardrails": {k: g.version for k, g in sorted(self.guardrails.items())},
        }
        for k, t in sorted(self.tools.items()):
            out.setdefault(TOOL_KINDS.get(t.type, t.type), {})[k] = t.version
        return {kind: ids for kind, ids in sorted(out.items()) if ids}

    def all_units(self) -> list[ExportContent]:
        """This unit and every child unit below it, depth first, children sorted by id."""
        return [self, *(u for _, c in sorted(self.children.items()) for u in c.all_units())]


def node(
    id: str,
    config: NodeConfig,
    *,
    name: str = "",
    policy: Policy | None = None,
    routing: Literal["edges", "label"] = "edges",
    condition_id: str | None = None,
    source_id: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> Node:
    """A graph node whose kind follows from the type of ``config``.

    Only the config fields that were set are kept, so a node reads back exactly as it was written. Keys the config classes do not
    declare (newer source fields) go in ``extra``.
    """
    try:
        kind = _KIND_OF[type(config)]
    except KeyError as exc:
        raise TypeError(f"node '{id}': {type(config).__name__} is not a node config") from exc
    data = config.model_dump(mode="json", exclude_unset=True)
    if isinstance(config, IterationConfig) and "body" in config.model_fields_set:
        data["body"] = [b.model_dump(mode="json") for b in config.body]  # a body node is whole, like a graph node
    data.update(extra or {})
    return Node(
        id=id,
        kind=kind,
        name=name,
        config=data,
        policy=policy or Policy(),
        routing=routing,
        condition_id=condition_id,
        source_id=source_id,
    )


def edge(source: str, target: str, *, label: str | None = None, condition: str | None = None) -> Edge:
    """A graph edge; ``START`` and ``END`` mark the entry and the exit."""
    return Edge(source=source, target=target, label=label, condition=condition)


def _plain(obj: Any) -> Any:
    """``obj`` as JSON data, without any ``sha256`` field, so a digest never covers itself."""
    if isinstance(obj, BaseModel):
        return _plain(obj.model_dump(mode="json", exclude={"sha256"} if "sha256" in type(obj).model_fields else None))
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _plain(getattr(obj, f.name)) for f in dataclasses.fields(obj) if f.name != "sha256"}
    if isinstance(obj, Mapping):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_plain(v) for v in obj]
    if obj is None or isinstance(obj, str | int | float | bool):
        return obj
    raise TypeError(f"digest: {type(obj).__name__} is not content data")


def digest(obj: Any) -> str:
    """The canonical content hash of a constant (``sha256:<hex>``): sorted keys, compact JSON, its own ``sha256`` field left out."""
    text = json.dumps(_plain(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
