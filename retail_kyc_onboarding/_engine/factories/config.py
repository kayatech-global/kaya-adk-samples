"""Factory kinds, their build order and default provider names, and the factory set one engine runs with."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from ..errors import ConfigError, ProviderHealthError
from .base import BuildContext, Provider

# Build order: secrets first, then identity and policy, before anything that calls out.
BUILD_ORDER = [
    "secret",
    "credentials",
    "authn",
    "authz",
    "prompt",
    "model",
    "api",
    "mcp",
    "session",
    "telemetry",
    "adk_plugin",
    "approval_policy",
    "approver_resolver",
    "approval_channel",
    "router",
    "runstore",
    "registration",
    "guardrail",
    "retrieval",
]

DEFAULT_PROVIDERS = {
    "secret": "env",
    "credentials": "none",
    "authn": "anonymous",
    "authz": "allow_all",
    "prompt": "content",
    "model": "litellm",
    "api": "http",
    "mcp": "mcp_client",
    "session": "uri",
    "telemetry": "console",
    "adk_plugin": "none",
    "approval_policy": "by_tool",
    "approver_resolver": "standard",
    "approval_channel": "adk_request_input",
    "router": "llm_structured",
    "runstore": "uri",
    "registration": "noop",
    "guardrail": "none",
    "retrieval": "none",
}


class FactorySet:
    """All providers for one export, created once at start-up."""

    def __init__(self, providers: dict[str, Provider], health: dict[str, str]):
        self.providers = providers
        self.health = health

    def get(self, kind: str) -> Any:
        """Return the built provider for ``kind``.

        Raises:
            ConfigError: That kind was not built.
        """
        try:
            return self.providers[kind]
        except KeyError as exc:
            raise ConfigError(f"factory kind '{kind}' was not built") from exc

    def provider(self, kind: str) -> Any:
        """Same as get(); lets a FactorySet stand in for a BuildContext where providers are looked up by kind."""
        return self.get(kind)

    def __getattr__(self, kind: str) -> Any:
        """Attribute access by factory kind (``factories.model``); names outside BUILD_ORDER raise AttributeError."""
        if kind.startswith("_") or kind not in BUILD_ORDER:
            raise AttributeError(kind)
        return self.get(kind)

    def describe(self) -> list[dict[str, Any]]:
        """Provider descriptions in build order, for the trace."""
        return [self.providers[k].describe() for k in BUILD_ORDER if k in self.providers]

    async def aclose(self) -> None:
        """Close every provider, in reverse creation order."""
        for p in reversed(list(self.providers.values())):
            await p.aclose()

    @classmethod
    async def bind(
        cls, providers: Mapping[str, Provider], ctx: BuildContext | None = None, *, required: set[str] | None = None
    ) -> FactorySet:
        """Bind providers built in code to ``ctx`` in build order and health-check them.

        Every kind must be given. ``required`` lists kinds whose failing health check stops start-up.

        Raises:
            ConfigError: A kind is missing or unknown, or a provider is of the wrong kind or already serves another engine.
            ProviderHealthError: A required provider is unhealthy.
        """
        ctx = ctx or BuildContext()
        required = required or set()
        unknown = set(providers) - set(BUILD_ORDER)
        missing = [k for k in BUILD_ORDER if k not in providers]
        if unknown or missing:
            raise ConfigError(f"providers: unknown kind(s) {sorted(unknown)}, missing kind(s) {missing}")
        health: dict[str, str] = {}
        for kind in BUILD_ORDER:
            provider = providers[kind]
            if provider.kind != kind:
                raise ConfigError(f"providers.{kind} is a '{provider.kind}' provider ({type(provider).__name__})")
            provider.bind(ctx)
            ctx.built[kind] = provider
            status = await provider.health()
            health[kind] = "ok" if status.ok else status.detail or "unhealthy"
            if not status.ok and kind in required:
                raise ProviderHealthError(kind, provider.name, status.detail)
        return cls(dict(ctx.built), health)


def default_env() -> dict[str, str]:
    """Snapshot of the process environment."""
    return dict(os.environ)
