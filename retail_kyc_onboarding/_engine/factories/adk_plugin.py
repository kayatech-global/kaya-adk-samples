"""The ``adk_plugin`` factory kind: extra Google ADK plugins that run inside every run of the export.

Purpose
    ADK plugins are the supported way to add behaviour around a whole run: before and after every agent, model call and tool call
    (policy checks, guardrails, audit, Model Armor, cost accounting). A platform that hosts the export usually has such a plugin.
    This kind lets it be handed over in code instead of patching the engine.

Place in the flow
    ``Engine.start`` asks the provider for the plugins once, and keeps them in ``engine.adk_plugins``. The ADK runtime adapter
    builds the run's ``App`` with ``[KayaTracePlugin, *client plugins]``. The trace plugin always comes first
    and cannot be removed: it is what writes the step records. ADK runs plugins in list order and stops at the first one that returns a value.

Contract of a provider
    ``plugins(engine)`` returns a list of ``google.adk.plugins.base_plugin.BasePlugin`` instances with unique, non-empty names (ADK refuses
    duplicates). It is called once at start-up. It must not make network calls.

Configuration
    runtime.yaml: ``adk_plugin: {provider: static, plugins: ["my_package.audit:AuditPlugin"]}`` (each class built with no arguments)
    settings.py:  ``p.adk_plugin = StaticAdkPlugins([AuditPlugin(), ModelArmorPlugin()])``

Security
    A plugin sees prompts, answers and tool arguments. Treat it like any code that runs with the export's authority. Plugins written for
    this export import the engine as ``<package>._engine`` (see the generated EXTENDING.md), never under a top-level name.

Extending
    Subclass ``AdkPluginFactory`` for another source of plugins (a registry service, for example) and run ``check_adk_plugin_provider``
    from the engine's ``testing.contract`` module against it.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping
from importlib import import_module
from typing import TYPE_CHECKING, Any

from pydantic import Field

from ..errors import ConfigError
from .base import Provider, ProviderSettings
from .registry import register

if TYPE_CHECKING:
    from ..engine.engine import Engine

RESERVED_NAMES = {"flow_trace"}  # the engine's own plugin


class AdkPluginFactory(Provider):
    """Port of the ``adk_plugin`` kind: supplies extra ADK plugins that run in every run, after the engine's own trace plugin."""
    kind = "adk_plugin"

    @abstractmethod
    async def plugins(self, engine: Engine) -> list[Any]:
        """The ADK plugins to run after ``KayaTracePlugin``, in order."""


@register
class NoAdkPlugins(AdkPluginFactory):
    """Default: no extra plugins."""

    name = "none"

    async def plugins(self, engine: Engine) -> list[Any]:
        """Return an empty list."""
        return []


@register
class StaticAdkPlugins(AdkPluginFactory):
    """A fixed list of ADK plugins: instances handed over in code, or class paths named in ``runtime.yaml``."""

    name = "static"

    class Settings(ProviderSettings):
        plugins: list[str] = Field(default_factory=list)  # "package.module:Class" of ADK plugins, each built with no arguments

    def __init__(self, plugins: list[Any] | Mapping[str, Any] | ProviderSettings | None = None, ctx: Any = None):
        if isinstance(plugins, list):  # instances, from code
            super().__init__(None, ctx)
            self._plugins = list(plugins)
        else:  # settings, from runtime.yaml
            super().__init__(plugins, ctx)
            self._plugins = [_instance(path) for path in self.settings.plugins]

    async def plugins(self, engine: Engine) -> list[Any]:
        """Return the plugins in order.

        Raises ConfigError when a plugin is not an ADK ``BasePlugin`` or its name is empty, duplicated or reserved.
        """
        from google.adk.plugins.base_plugin import BasePlugin

        seen: set[str] = set(RESERVED_NAMES)
        for plugin in self._plugins:
            if not isinstance(plugin, BasePlugin):
                raise ConfigError(f"adk plugin {plugin!r} is not a google.adk BasePlugin instance")
            if not plugin.name or plugin.name in seen:
                raise ConfigError(f"adk plugin '{plugin.name}' has an empty or already used name (ADK requires unique names)")
            seen.add(plugin.name)
        return list(self._plugins)

    def describe(self) -> dict[str, Any]:
        """Add the plugin names to the base description."""
        return {**super().describe(), "plugins": [p.name for p in self._plugins]}


def _instance(path: str) -> Any:
    """An instance of the plugin class at ``package.module:Class``."""
    module, _, attr = path.partition(":")
    try:
        return getattr(import_module(module), attr)()
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        raise ConfigError(f"adk plugin '{path}' cannot be built: {exc}") from exc
