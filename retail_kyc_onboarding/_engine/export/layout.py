"""The manifest of an export: its build and audit record. The exporter writes it; nothing reads it to run."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

MANIFEST = "manifest.json"
CHILDREN_DIR = "children"
FORMAT_VERSION = 1

__all__ = ["MANIFEST", "Manifest"]


class ExportRef(BaseModel):
    """Identity of an export: its id, version and the version it replaced."""

    model_config = ConfigDict(extra="forbid")
    id: str
    version: str
    previous: str | None = None


class SourceInfo(BaseModel):
    """Where an export came from: the source workflow or plan, its status and the platform it was exported from."""

    model_config = ConfigDict(extra="allow")
    kind: str  # iflow | plan
    workflow_id: str = ""
    workflow_version: int | str | None = None
    plan_id: str | None = None
    plan_hash: str | None = None
    approved_status: str | None = None
    workspace_id: str | None = None  # workspace uuid of the source platform; audit data only, not a secret


class TargetInfo(BaseModel):
    """What an export targets: ADK version, runtime adapter, template set and bundle schema."""

    model_config = ConfigDict(extra="forbid")
    adk: str = "2.12.0"
    adapter: str = "adk_v2"
    template_set: str = ""
    bundle_schema: int = 1


class Manifest(BaseModel):
    """The manifest of an export: identity, source, target, entity versions, content digests and per-file hashes."""

    model_config = ConfigDict(extra="forbid")
    format: int = FORMAT_VERSION
    export: ExportRef
    source: SourceInfo
    target: TargetInfo = Field(default_factory=TargetInfo)
    core_requires: str = ""
    content_dir: str = ""  # the content package of the export, relative to the export folder (src/<pkg>/content)
    entities: dict[str, dict[str, str]] = Field(default_factory=dict)  # kind -> id -> active version
    files: dict[str, str] = Field(default_factory=dict)  # path -> "sha256:..."
    runtime_requirements: dict[str, Any] = Field(default_factory=dict)
    exclusions: list[str] = Field(default_factory=list)
    recovered: list[str] = Field(default_factory=list)  # tools the workflow did not define, recovered from the workspace registry
    children: dict[str, str] = Field(default_factory=dict)  # child id -> pinned version
    engine: dict[str, str] = Field(default_factory=dict)  # vendored engine: version, digest, files
    digests: dict[str, str] = Field(default_factory=dict)  # "<kind>/<id>" -> digest of the content constant (audit; never read to run)
