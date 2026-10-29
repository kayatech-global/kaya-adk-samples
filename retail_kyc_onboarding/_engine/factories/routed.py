"""Per-entity routing: one prompt, model or tool served by another provider than the rest of its kind.

``runtime.yaml`` builds these when a profile lists ``content:`` routes. Each wraps the provider of the kind and a map of entity id
to provider, and sends a call to the provider the entity is routed to; everything else goes to the kind's own provider.
"""

from __future__ import annotations

from typing import Any

from ..datatypes import ModelSpec, PromptSpec, ToolDef
from .api import ApiFactory, ToolHandle
from .base import BuildContext, HealthStatus, Provider
from .mcp import McpFactory
from .model import BaseLlm, ModelFactory
from .prompt import PromptFactory


class RoutedMixin:
    """The shared part: the kind's provider, the routes, and binding, health, description and closing of all of them."""

    kind: str
    default: Provider
    routes: dict[str, Provider]

    def _setup(self, default: Provider, routes: dict[str, Provider]) -> None:
        self.default, self.routes = default, dict(routes)

    def members(self) -> list[Provider]:
        """The kind's own provider and every routed one."""
        return [self.default, *self.routes.values()]

    def pick(self, entity_id: str) -> Any:
        """The provider that serves ``entity_id``."""
        return self.routes.get(entity_id, self.default)

    def bind(self, ctx: BuildContext) -> None:  # type: ignore[misc]
        """Bind the wrapped providers to the engine."""
        for member in self.members():
            member.bind(ctx)
        self.ctx = ctx

    async def health(self) -> HealthStatus:
        """Healthy when every wrapped provider is; the detail names the ones that are not."""
        bad = []
        for member in self.members():
            status = await member.health()
            if not status.ok:
                bad.append(f"{member.name}: {status.detail}")
        return HealthStatus(not bad, "; ".join(bad))

    def describe(self) -> dict[str, Any]:
        """The kind's provider and, per entity id, the provider it is routed to."""
        return {
            "kind": self.kind,
            "provider": "routed",
            "default": self.default.describe(),
            "routes": {eid: p.describe() for eid, p in self.routes.items()},
        }

    async def aclose(self) -> None:
        """Close every wrapped provider."""
        for member in self.members():
            await member.aclose()


class RoutedPrompts(RoutedMixin, PromptFactory):
    """Prompts, routed by prompt id."""

    name = "routed"

    def __init__(self, default: PromptFactory, routes: dict[str, Provider]):
        super().__init__()
        self._setup(default, routes)

    async def get(self, prompt_id: str, version: str | None = None, scope: str = "") -> PromptSpec:
        """The prompt from the provider routed for ``prompt_id``."""
        return await self.pick(prompt_id).get(prompt_id, version, scope)  # type: ignore[no-any-return]


class RoutedModels(RoutedMixin, ModelFactory):
    """Models, routed by model entity id."""

    name = "routed"

    def __init__(self, default: ModelFactory, routes: dict[str, Provider]):
        super().__init__()
        self._setup(default, routes)

    async def create(self, spec: ModelSpec) -> BaseLlm:
        """The model from the provider routed for ``spec.id``."""
        return await self.pick(spec.id).create(spec)  # type: ignore[no-any-return]

    async def ready(self, spec: ModelSpec) -> bool:
        """Whether the provider routed for ``spec.id`` can serve it."""
        return bool(await self.pick(spec.id).ready(spec))


class RoutedApi(RoutedMixin, ApiFactory):
    """API tools, routed by tool name."""

    name = "routed"

    def __init__(self, default: ApiFactory, routes: dict[str, Provider]):
        super().__init__()
        self._setup(default, routes)

    def build(self, tool: ToolDef) -> ToolHandle:
        """The handle from the provider routed for ``tool.name``."""
        return self.pick(tool.name).build(tool)  # type: ignore[no-any-return]


class RoutedMcp(RoutedMixin, McpFactory):
    """MCP servers, routed by server tool name."""

    name = "routed"

    def __init__(self, default: McpFactory, routes: dict[str, Provider]):
        super().__init__()
        self._setup(default, routes)

    async def build(self, tool: ToolDef) -> list[ToolHandle]:
        """The handles from the provider routed for the server ``tool.name``."""
        return await self.pick(tool.name).build(tool)  # type: ignore[no-any-return]


ROUTED = {"prompt": RoutedPrompts, "model": RoutedModels, "api": RoutedApi, "mcp": RoutedMcp}
