"""Provider profiles: ``runtime.yaml`` names, per mode, which provider serves each factory kind.

Precedence, highest first: providers set in ``settings.build_providers()`` after the profile is loaded, the selected profile, the
built-in defaults. A file that is missing means the defaults, so an export without ``runtime.yaml`` runs as before.
"""

from __future__ import annotations

import dataclasses
import importlib
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .app import FlowSettings, Overrides, Providers, default_providers
from .datatypes import ModelSpec
from .errors import ConfigError
from .factories import builtin  # noqa: F401  (registers the built-in providers)
from .factories.base import Provider
from .factories.config import BUILD_ORDER
from .factories.registry import REGISTRY
from .factories.routed import ROUTED

DEFAULT_MODE = "default"
ARC_PROVIDER = "arc"  # the profile `config` goes to every provider with this name
ALL = "all"  # `factories: {all: arc}`: every kind that has a provider of that name uses it
CONTENT_KINDS = ("prompts", "models", "tools")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_REF = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")


class FactoryChoice(BaseModel):
    """One factory kind in a profile: a provider name and the settings it is given."""

    model_config = ConfigDict(extra="allow")

    provider: str

    def settings(self) -> dict[str, Any]:
        """The settings given beside ``provider``."""
        return dict(self.model_extra or {})


class Profile(BaseModel):
    """A mode: an optional parent, settings shared by every ``arc`` provider, and the provider chosen per kind."""

    model_config = ConfigDict(extra="forbid")

    extends: str | None = None
    config: dict[str, Any] | None = None
    factories: dict[str, str | FactoryChoice] | None = None
    content: dict[str, dict[str, dict[str, Any] | None]] | None = None  # kind (prompts, models, tools) -> entity id -> route and overrides


class ProfileFile(BaseModel):
    """The whole ``runtime.yaml``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    mode: str = DEFAULT_MODE
    plugins: list[str] = Field(default_factory=list)
    provider_profile: dict[str, Profile] = Field(alias="provider-profile", default_factory=dict)


class ResolvedProfile(BaseModel):
    """The selected mode with its parents merged and every ``${env:...}`` filled in."""

    mode: str
    config: dict[str, Any] = {}
    factories: dict[str, FactoryChoice] = {}
    content: dict[str, dict[str, dict[str, Any]]] = {}
    plugins: list[str] = []


def interpolate(value: Any, env: Mapping[str, str], where: str) -> Any:
    """Replace ``${env:NAME}`` and ``${env:NAME:-default}`` in every string of ``value``.

    Raises:
        ConfigError: A variable is unset and has no default; the message names the variable and the YAML path.
    """
    if isinstance(value, str):

        def fill(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            found = env.get(name)
            if found not in (None, ""):
                return found  # type: ignore[return-value]
            if default is not None:
                return default
            raise ConfigError(f"{where}: environment variable {name} is not set (write ${{env:{name}:-<default>}} to give it a default)")

        return _REF.sub(fill, value)
    if isinstance(value, dict):
        return {k: interpolate(v, env, f"{where}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate(v, env, f"{where}[{i}]") for i, v in enumerate(value)]
    return value


def read_file(path: Path) -> ProfileFile:
    """Parse ``runtime.yaml`` without resolving any variable, so a profile that is not selected never fails.

    Raises:
        ConfigError: The YAML is invalid, a key is unknown, or a factory kind is not one of ``BUILD_ORDER``.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        file = ProfileFile.model_validate(raw)
    except (yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"{path.name} is not valid: {exc}") from exc
    for name, profile in file.provider_profile.items():
        for kind in profile.factories or {}:
            if kind not in BUILD_ORDER and kind != ALL:
                raise ConfigError(f"{path.name}: provider-profile.{name}.factories: unknown kind '{kind}'. Kinds: {', '.join(BUILD_ORDER)}, {ALL}")
        for kind in profile.content or {}:
            if kind not in CONTENT_KINDS:
                raise ConfigError(f"{path.name}: provider-profile.{name}.content: unknown kind '{kind}'. Kinds: {', '.join(CONTENT_KINDS)}")
    return file


def _chain(file: ProfileFile, mode: str, path: str) -> list[tuple[str, Profile]]:
    """The profile and its parents, parents first. A profile without ``extends`` extends ``default``."""
    chain: list[tuple[str, Profile]] = []
    seen: set[str] = set()
    name: str | None = mode
    while name is not None:
        if name in seen:
            raise ConfigError(f"{path}: provider-profile '{name}' extends itself through {' -> '.join([*seen, name])}")
        seen.add(name)
        profile = file.provider_profile.get(name)
        if profile is None:
            if name == DEFAULT_MODE:
                break  # the built-in defaults stand in for a missing default profile
            raise ConfigError(f"{path}: unknown mode '{name}'. Profiles: {', '.join(sorted(file.provider_profile)) or 'none'}")
        chain.append((name, profile))
        name = profile.extends if profile.extends is not None else (DEFAULT_MODE if name != DEFAULT_MODE else None)
    return list(reversed(chain))


def load_profile(path: Path, mode: str = "", env: Mapping[str, str] | None = None) -> ResolvedProfile:
    """Read ``path`` and resolve one mode: ``mode`` if given, else the file's ``mode``, else ``default``.

    Raises:
        ConfigError: The mode or a kind is unknown, a variable is unset or a factory entry is malformed.
    """
    env = env if env is not None else os.environ
    file = read_file(path)
    selected = mode or str(interpolate(file.mode, env, f"{path.name}: mode")) or DEFAULT_MODE
    plugins = [str(interpolate(m, env, f"{path.name}: plugins")) for m in file.plugins]
    for module in plugins:  # their @register classes must exist before `all:` and the factory names are resolved
        try:
            importlib.import_module(module)
        except ImportError as exc:
            raise ConfigError(f"{path.name}: plugins: cannot import '{module}': {exc}") from exc
    config: dict[str, Any] = {}
    factories: dict[str, FactoryChoice] = {}
    content: dict[str, dict[str, dict[str, Any]]] = {}
    for name, profile in _chain(file, selected, path.name):
        where = f"{path.name}: provider-profile.{name}"
        config.update(interpolate(profile.config or {}, env, f"{where}.config"))
        entries = dict(profile.factories or {})
        everywhere = entries.pop(ALL, None)
        if everywhere is not None:  # a name applies to every kind that has such a provider; the lines below override it
            wanted = interpolate(everywhere if isinstance(everywhere, str) else everywhere.provider, env, f"{where}.factories.{ALL}")
            for kind in BUILD_ORDER:
                if wanted in REGISTRY.names(kind):
                    factories[kind] = FactoryChoice(provider=wanted)
        for kind, choice in entries.items():
            filled = interpolate(choice.model_dump() if isinstance(choice, FactoryChoice) else choice, env, f"{where}.factories.{kind}")
            factories[kind] = FactoryChoice.model_validate(filled if isinstance(filled, dict) else {"provider": filled})
        for kind, by_id in (profile.content or {}).items():
            for entity_id, entry in by_id.items():
                filled = interpolate(entry or {}, env, f"{where}.content.{kind}.{entity_id}")
                content.setdefault(kind, {}).setdefault(entity_id, {}).update(filled)
    return ResolvedProfile(mode=selected, config=config, factories=factories, content=content, plugins=plugins)


def _build(kind: str, choice: FactoryChoice, config: Mapping[str, Any], current: Provider | None) -> Provider:
    """The provider ``choice`` names. The default instance stays when the name and settings are the default's."""
    settings = choice.settings()
    if choice.provider == ARC_PROVIDER:
        settings = {**config, **settings}
    if current is not None and choice.provider == current.name and not settings:
        return current
    cls = REGISTRY.resolve(kind, choice.provider)
    return cls(settings)


PROMPT_FIELDS = {"text", "file", "version"}
TOOL_FIELDS = {"description", "config", "approval", "manual_execution", "version"}
MODEL_FIELDS = {f.name for f in dataclasses.fields(ModelSpec)} - {"id", "sha256"}


def _split(entry: dict[str, Any], where: str) -> tuple[FactoryChoice | None, dict[str, Any]]:
    """The provider an entity is routed to (``None`` for none) and its field overrides.

    ``provider`` is a name, a class path or a mapping with ``provider`` and settings; ``settings`` beside a name holds the settings.
    """
    fields = dict(entry)
    route, settings = fields.pop("provider", None), fields.pop("settings", None)
    if route is None:
        if settings is not None:
            raise ConfigError(f"{where}: `settings` needs a `provider` beside it")
        return None, fields
    if isinstance(route, dict):
        if settings is not None:
            raise ConfigError(f"{where}: give the settings either in `provider` or in `settings`, not both")
        return FactoryChoice.model_validate(route), fields
    if settings is not None and not isinstance(settings, dict):
        raise ConfigError(f"{where}: `settings` must be a mapping")
    return FactoryChoice.model_validate({**(settings or {}), "provider": route}), fields


def _unknown(where: str, what: str, given: set[str], valid: set[str]) -> ConfigError:
    return ConfigError(f"{where}: unknown {what} {sorted(given - valid)}. Valid: {', '.join(sorted(valid))}")


def _overrides_and_routes(
    resolved: ProfileFile | ResolvedProfile, content: Any, root: Path, file_name: str
) -> tuple[Overrides, dict[str, dict[str, FactoryChoice]]]:
    """The entity overrides and the per-entity provider routes of the profile's ``content:``, checked against the export's ids."""
    models: dict[str, Any] = {}
    prompts: dict[str, Any] = {}
    tools: dict[str, Any] = {}
    routes: dict[str, dict[str, FactoryChoice]] = {}
    known = {"prompts": content.prompts, "models": content.models, "tools": content.tools}
    for kind, by_id in resolved.content.items():
        for entity_id, entry in by_id.items():
            where = f"{file_name}: content.{kind}.{entity_id}"
            if entity_id not in known[kind]:
                raise ConfigError(f"{where}: unknown {kind[:-1]} id. Known: {', '.join(sorted(known[kind]))}")
            choice, fields = _split(entry, where)
            if choice is not None:
                route_kind = {"prompts": "prompt", "models": "model", "tools": content.tools[entity_id].type if kind == "tools" else ""}[kind]
                routes.setdefault(route_kind, {})[entity_id] = choice
            if not fields:
                continue
            base = known[kind][entity_id]
            if kind == "prompts":
                if not fields.keys() <= PROMPT_FIELDS:
                    raise _unknown(where, "field", set(fields), PROMPT_FIELDS)
                text = (root / fields["file"]).read_text(encoding="utf-8") if "file" in fields else fields.get("text", base.text)
                prompts[entity_id] = dataclasses.replace(base, text=text, version=fields.get("version", base.version), sha256="", meta={**base.meta, "modified": True})
            elif kind == "models":
                if not fields.keys() <= MODEL_FIELDS:
                    raise _unknown(where, "field", set(fields), MODEL_FIELDS)
                if "fallbacks" in fields:
                    fields["fallbacks"] = tuple(fields["fallbacks"])
                models[entity_id] = dataclasses.replace(base, **fields)
            else:
                if not fields.keys() <= TOOL_FIELDS:
                    raise _unknown(where, "field", set(fields), TOOL_FIELDS)
                patch = {k: v for k, v in fields.items() if k != "config" and k != "approval"}
                tools[entity_id] = dataclasses.replace(
                    base, config={**base.config, **fields.get("config", {})}, approval={**base.approval, **fields.get("approval", {})}, **patch
                )
    return Overrides(models=models, prompts=prompts, tools=tools), routes


def profile_providers(
    settings: FlowSettings, path: Path | str | None = None, env: Mapping[str, str] | None = None, content: Any = None
) -> Providers:
    """The providers for this run: the built-in defaults with the selected ``runtime.yaml`` profile applied.

    ``FLOW_PROVIDER_MODE`` (``settings.provider_mode``) wins over the file's ``mode``. Without the file the defaults are returned;
    asking for another mode without the file is an error. With ``content`` (the export's ``ExportContent``) the profile's
    ``content:`` routes and field overrides are applied too: routes become routing providers, overrides land in ``providers.overrides``.

    Raises:
        ConfigError: See ``load_profile``, plus a mode that needs a file which is missing, an unknown entity id or field, or
            settings a provider does not accept.
    """
    file = Path(path) if path else PACKAGE_ROOT / settings.runtime_file
    providers = default_providers(settings)
    if not file.is_file():
        if settings.provider_mode not in ("", DEFAULT_MODE):
            raise ConfigError(f"FLOW_PROVIDER_MODE={settings.provider_mode} needs {file}, which does not exist")
        providers.mode = DEFAULT_MODE
        return providers
    resolved = load_profile(file, settings.provider_mode, env)
    for kind, choice in resolved.factories.items():
        setattr(providers, kind, _build(kind, choice, resolved.config, getattr(providers, kind, None)))
    providers.mode = resolved.mode
    if content is not None and resolved.content:
        providers.overrides, routes = _overrides_and_routes(resolved, content, file.parent, file.name)
        for kind, by_id in routes.items():
            members = {eid: _build(kind, choice, resolved.config, None) for eid, choice in by_id.items()}
            setattr(providers, kind, ROUTED[kind](getattr(providers, kind), members))
    return providers
