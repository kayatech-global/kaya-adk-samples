"""Registry: kind + provider name to class, from programmatic registration, entry points and built-ins (in that order)."""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import entry_points
from typing import TypeVar

from ..errors import ConfigError, ProviderNotFound
from .base import Provider

ENTRY_POINT_GROUP = "flow_adk.factories"
T = TypeVar("T", bound=Provider)


class Registry:
    """Maps (kind, provider name) to provider classes from built-ins, entry points and programmatic registration."""
    def __init__(self) -> None:
        self._builtin: dict[tuple[str, str], type[Provider]] = {}
        self._extra: dict[tuple[str, str], type[Provider]] = {}
        self._entry_points_loaded = False

    def register(self, cls: type[T]) -> type[T]:
        """Register ``cls`` as a built-in under its ``kind`` and ``name`` and return it, so it works as a decorator.

        Raises ConfigError when either is empty.
        """
        if not cls.kind or not cls.name:
            raise ConfigError(f"{cls.__qualname__} must set 'kind' and 'name' to be registered")
        self._builtin[(cls.kind, cls.name)] = cls
        return cls

    def register_extra(self, cls: type[Provider]) -> None:
        """Programmatic registration for clients and tests. Wins over built-ins."""
        self._extra[(cls.kind, cls.name)] = cls

    def _load_entry_points(self) -> None:
        if self._entry_points_loaded:
            return
        self._entry_points_loaded = True
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            # entry point name: "<kind>.<provider>", value: "package.module:Class"
            kind, _, name = ep.name.partition(".")
            if not name:
                raise ConfigError(f"entry point '{ep.name}' must be named '<kind>.<provider>'")
            self._extra.setdefault((kind, name), ep.load())

    def resolve(self, kind: str, provider: str) -> type[Provider]:
        """Return the provider class for ``kind``.

        ``provider`` is a registered name or a class path ``package.module:Class``. Extra registrations and entry points win
        over built-ins. Raises ProviderNotFound for an unknown name and ConfigError for a provider of another kind.
        """
        self._load_entry_points()
        if ":" in provider:
            return self._by_class_path(kind, provider)
        cls = self._extra.get((kind, provider)) or self._builtin.get((kind, provider))
        if cls is None:
            known = [n for (k, n) in {**self._builtin, **self._extra} if k == kind]
            raise ProviderNotFound(kind, provider, known)
        if cls.kind and cls.kind != kind:
            raise ConfigError(f"provider '{provider}' is a '{cls.kind}' provider, not '{kind}'")
        return cls

    def _by_class_path(self, kind: str, path: str) -> type[Provider]:
        module, _, attr = path.partition(":")
        try:
            cls = getattr(import_module(module), attr)
        except (ImportError, AttributeError) as exc:
            raise ConfigError(f"provider class '{path}' for factory kind '{kind}' cannot be imported: {exc}") from exc
        if not (isinstance(cls, type) and issubclass(cls, Provider)):
            raise ConfigError(f"'{path}' is not a Provider class")
        if cls.kind != kind:
            raise ConfigError(f"provider '{path}' is a '{cls.kind}' provider, not '{kind}'")
        return cls

    def names(self, kind: str) -> list[str]:
        """Return the sorted provider names available for ``kind``, loading entry points first."""
        self._load_entry_points()
        return sorted({n for (k, n) in {**self._builtin, **self._extra} if k == kind})


REGISTRY = Registry()


def register(cls: type[T]) -> type[T]:
    """Class decorator: register ``cls`` in the global REGISTRY."""
    return REGISTRY.register(cls)
