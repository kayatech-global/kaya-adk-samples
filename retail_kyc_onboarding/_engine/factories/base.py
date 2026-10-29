"""Provider base class and build context shared by every factory kind."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError

from ..errors import ConfigError


class ProviderSettings(BaseModel):
    """Base for provider settings. Unknown keys are rejected so a typo fails at start-up."""

    model_config = ConfigDict(extra="forbid")


@dataclass
class HealthStatus:
    """Result of a provider health check: ``ok`` and, when not ok, a human-readable ``detail``."""
    ok: bool = True
    detail: str = ""


@dataclass
class BuildContext:
    """What a provider may use while it is being built. Earlier-built providers are reachable by kind."""

    export_root: Path = field(default_factory=Path.cwd)
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    built: dict[str, Provider] = field(default_factory=dict)
    profile: str = "development"
    manifest: Any = None  # export Manifest when the export is loaded, else None

    units: dict[str, Any] = field(default_factory=dict)  # scope -> Manifest (children have their own)
    content_units: dict[str, Any] = field(default_factory=dict)  # scope -> ContentUnit, overrides applied

    def unit_manifest(self, scope: str = "") -> Any:
        """Return the manifest of the unit named ``scope``, or the root manifest when ``scope`` is empty."""
        return self.units.get(scope) if scope else self.manifest

    def provider(self, kind: str) -> Any:
        """Return the already-built provider of ``kind``; raise ConfigError when it is not built yet."""
        try:
            return self.built[kind]
        except KeyError as exc:
            raise ConfigError(f"factory kind '{kind}' is not built yet; check the build order") from exc


class Provider:
    """One implementation of one factory kind.

    Subclasses set ``kind`` and ``name`` and may declare ``Settings``. Nothing network-bound runs in
    ``__init__``; use ``health`` for that.
    """

    kind: ClassVar[str] = ""
    name: ClassVar[str] = ""
    Settings: ClassVar[type[ProviderSettings]] = ProviderSettings

    def __init__(self, settings: Mapping[str, Any] | ProviderSettings | None = None, ctx: BuildContext | None = None):
        self.ctx = ctx or BuildContext()
        self.settings: Any
        if isinstance(settings, self.Settings):
            self.settings = settings
            return
        try:
            self.settings = self.Settings.model_validate(dict(settings or {}))
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            raise ConfigError(f"Invalid settings for factory '{self.kind}' provider '{self.name}': {problems}") from exc

    def bind(self, ctx: BuildContext) -> None:
        """Attach a provider built in code to the engine it serves. A provider serves one engine only.

        Raises:
            ConfigError: The provider is already bound to another engine.
        """
        bound = getattr(self, "_bound", None)
        if bound is not None and bound is not ctx:
            raise ConfigError(
                f"factory '{self.kind}' provider '{self.name}' already serves another engine; build new providers for each engine"
            )
        self.ctx, self._bound = ctx, ctx

    async def health(self) -> HealthStatus:
        """Check that the provider can work. The default reports healthy.

        Override for providers that need a key or a network; ``__init__`` must stay free of such work.
        """
        return HealthStatus(True)

    def describe(self) -> dict[str, Any]:
        """Safe description for the trace: never includes secret values."""
        return {"kind": self.kind, "provider": self.name, "class": f"{type(self).__module__}.{type(self).__qualname__}"}

    def members(self) -> list[Provider]:
        """The providers behind this one: itself, or the wrapped ones of a routing provider."""
        return [self]

    async def aclose(self) -> None:
        """Release resources held by the provider. The default does nothing."""
        return None
