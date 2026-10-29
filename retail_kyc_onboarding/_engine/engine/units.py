"""A content unit is one graph with everything it refers to, in memory: the export itself, or one bundled child IFLOW."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..app import Overrides
from ..content import Condition, ExportContent, Prompt, digest
from ..datatypes import ModelSpec, ToolDef
from ..errors import ConfigError, ExportIntegrityError
from ..export.layout import CHILDREN_DIR, Manifest
from ..model import Graph

if TYPE_CHECKING:
    from google.adk.models.base_llm import BaseLlm

    from ..factories.api import ToolHandle


def sha256_file(path: Path) -> str:
    """Hash a file as ``sha256:<hex>``, the form used in the manifest."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def manifest_for(content: ExportContent) -> Manifest:
    """The manifest view of a unit, built from its content (no file hashes: content is not read from files)."""
    i = content.identity
    return Manifest(
        format=i.format,
        export=i.export,
        source=i.source,
        target=i.target,
        core_requires=i.core_requires,
        entities=content.entities,
        runtime_requirements=dict(i.runtime_requirements),
        exclusions=list(i.exclusions),
        recovered=list(i.recovered),
        children={k: c.version for k, c in sorted(content.children.items())},
    )


@dataclass
class ContentUnit:
    """One unit with the overrides applied: its graph, prompts, models, tools, conditions and child units.

    ``modified`` lists what differs from the exported content: ``prompt:<id> (edited)`` when a constant no longer matches its
    digest, ``model:<id> (override)`` when a client replaced it.
    """

    content: ExportContent
    scope: str  # "" for the export, "children/<id>" for a child (nested: "children/<a>/children/<b>")
    manifest: Manifest
    graph: Graph
    prompts: dict[str, Prompt] = field(default_factory=dict)
    models: dict[str, ModelSpec] = field(default_factory=dict)
    llms: dict[str, BaseLlm] = field(default_factory=dict)  # model overrides that already are ADK models
    tool_defs: dict[str, ToolDef] = field(default_factory=dict)
    mcp_defs: dict[str, ToolDef] = field(default_factory=dict)
    rag_defs: dict[str, ToolDef] = field(default_factory=dict)
    tool_handles: dict[str, ToolHandle] = field(default_factory=dict)  # tool overrides that already are handles
    conditions: dict[str, Condition] = field(default_factory=dict)
    children: dict[str, ContentUnit] = field(default_factory=dict)
    modified: list[str] = field(default_factory=list)

    @classmethod
    def from_content(
        cls,
        content: ExportContent,
        overrides: Overrides | None = None,
        scope: str = "",
        *,
        manifest: Manifest | None = None,
        child_manifests: dict[str, Manifest] | None = None,
    ) -> ContentUnit:
        """Build the unit of ``content`` and of every child, with ``overrides`` applied.

        ``manifest`` (and ``child_manifests``, by scope) replace the manifest view built from the content.

        Raises:
            ConfigError: An override names an entity the unit does not have.
        """
        from google.adk.models.base_llm import BaseLlm

        from ..factories.api import ToolHandle

        ov = overrides or Overrides()
        where = f"{scope}/" if scope else ""
        _check_ids("model", ov.models, content.models, scope)
        _check_ids("prompt", ov.prompts, content.prompts, scope)
        _check_ids("tool", ov.tools, content.tools, scope)
        _check_ids("child", ov.children, content.children, scope)
        modified: list[str] = []

        def edited(kind: str, eid: str, obj: Any) -> None:
            sha = getattr(obj, "sha256", "")
            if sha and digest(obj) != sha:
                modified.append(f"{where}{kind}:{eid} (edited)")

        prompts: dict[str, Prompt] = {}
        for pid, p in sorted(content.prompts.items()):
            edited("prompt", pid, p)
            new = ov.prompts.get(pid)
            if new is not None:
                modified.append(f"{where}prompt:{pid} (override)")
                p = replace(p, text=new, meta={**p.meta, "modified": True}) if isinstance(new, str) else new
            prompts[pid] = p
        models: dict[str, ModelSpec] = {}
        llms: dict[str, BaseLlm] = {}
        for mid, m in sorted(content.models.items()):
            edited("model", mid, m)
            new_m = ov.models.get(mid)
            if new_m is not None:
                modified.append(f"{where}model:{mid} (override)")
                if isinstance(new_m, BaseLlm):
                    llms[mid] = new_m
                    continue
                m = new_m
            models[mid] = m
        defs: dict[str, dict[str, ToolDef]] = {"api": {}, "mcp": {}, "rag": {}}
        handles: dict[str, ToolHandle] = {}
        for tid, t in sorted(content.tools.items()):
            edited("tool", tid, t)
            new_t = ov.tools.get(tid)
            if new_t is not None:
                modified.append(f"{where}tool:{tid} (override)")
                if isinstance(new_t, ToolHandle):
                    handles[tid] = new_t
                    continue
                t = new_t
            defs.setdefault(t.type, {})[tid] = t
        for cid, c in sorted(content.conditions.items()):
            edited("condition", cid, c)
        if content.graph_sha256 and digest(content.graph) != content.graph_sha256:
            modified.append(f"{where}graph (edited)")
        unit = cls(
            content=content,
            scope=scope,
            manifest=manifest or manifest_for(content),
            graph=content.graph,
            prompts=prompts,
            models=models,
            llms=llms,
            tool_defs=defs["api"],
            mcp_defs=defs["mcp"],
            rag_defs=defs["rag"],
            tool_handles=handles,
            conditions=dict(content.conditions),
            modified=modified,
        )
        for cid, child in sorted(content.children.items()):
            cscope = f"{where}{CHILDREN_DIR}/{cid}"
            unit.children[cid] = cls.from_content(
                child,
                ov.children.get(cid),
                cscope,
                manifest=(child_manifests or {}).get(cscope),
                child_manifests=child_manifests,
            )
        return unit

    @property
    def modified_entities(self) -> list[str]:
        """What differs from the exported content, in this unit and its children."""
        return [*self.modified, *(m for c in self.children.values() for m in c.modified_entities)]

    def child(self, child_id: str) -> ContentUnit:
        """The bundled child unit with this id.

        Raises:
            ExportIntegrityError: The child is not bundled in this export.
        """
        try:
            return self.children[child_id]
        except KeyError as exc:
            raise ExportIntegrityError(f"child IFLOW '{child_id}' is not bundled in this export") from exc

    def all_units(self) -> list[ContentUnit]:
        """This unit followed by all of its descendants."""
        out = [self]
        for c in self.children.values():
            out.extend(c.all_units())
        return out

    def condition_source(self, condition_id: str) -> str:
        """The source of a condition module.

        Raises:
            ExportIntegrityError: The unit has no such condition.
        """
        try:
            return self.conditions[condition_id].source
        except KeyError as exc:
            raise ExportIntegrityError(f"condition '{condition_id}' is not part of this export") from exc


def _check_ids(kind: str, given: Any, known: Any, scope: str) -> None:
    unknown = sorted(set(given) - set(known))
    if unknown:
        where = f" of {scope}" if scope else ""
        raise ConfigError(f"unknown {kind} id(s){where} in overrides: {unknown}. Known: {sorted(known)}")
