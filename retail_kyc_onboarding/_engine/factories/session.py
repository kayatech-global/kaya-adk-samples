"""session: the ADK session service (where run state and pauses live)."""

from __future__ import annotations

from abc import abstractmethod
from typing import Any

from google.adk.sessions import BaseSessionService, InMemorySessionService

from ..errors import ConfigError
from .base import Provider, ProviderSettings
from .registry import register

MEMORY_URIS = ("", "memory", "memory://")


def check_storage_uri(uri: str) -> str:
    """Validate a session storage URI (``memory://`` or a SQLAlchemy async URL) and return it trimmed.

    Raises:
        ConfigError: The URI names a scheme the engine cannot use, with the form to use instead.
    """
    uri = uri.strip()
    if uri in MEMORY_URIS:
        return "memory://"
    scheme = uri.split("://", 1)[0].lower() if "://" in uri else ""
    if scheme == "sqlite":
        raise ConfigError("session storage 'sqlite://...' is ADK's own SQLite format; use 'sqlite+aiosqlite:///<file>' instead")
    if scheme == "agentengine":
        raise ConfigError("session storage 'agentengine://' is not built in; add a session provider for it (EXTENDING.md)")
    if not scheme or "+" not in scheme:
        raise ConfigError(
            f"session storage '{scheme or uri}' is not supported: use 'memory://' or a SQLAlchemy async URL such as "
            "'sqlite+aiosqlite:///runs.db' or 'postgresql+asyncpg://user@host/db'"
        )
    return uri


class SessionFactory(Provider):
    """Port of the ``session`` kind: supplies the ADK session service that holds run state."""
    kind = "session"

    @abstractmethod
    def service(self) -> BaseSessionService:
        """Return the ADK session service.

        Implementers must return the same long-lived service on every call so runs and resumes share state.
        """


@register
class MemorySessions(SessionFactory):
    """Process-local sessions. State and paused runs are lost on restart."""
    name = "memory"

    def __init__(self, settings: Any = None, ctx: Any = None):
        super().__init__(settings, ctx)
        self._svc = InMemorySessionService()

    def service(self) -> BaseSessionService:
        """Return the in-memory session service."""
        return self._svc


@register
class SqlAlchemySessions(SessionFactory):
    """Durable sessions: a paused run survives a restart. url e.g. sqlite+aiosqlite:///runs.db or postgresql+asyncpg://..."""

    name = "sqlalchemy"

    class Settings(ProviderSettings):
        url: str

    def __init__(self, settings: Any = None, ctx: Any = None):
        super().__init__(settings, ctx)
        self._svc: BaseSessionService | None = None

    def service(self) -> BaseSessionService:
        """Return the database-backed session service, created on first use so the driver is imported only when needed."""
        if self._svc is None:
            from google.adk.sessions import DatabaseSessionService

            self._svc = DatabaseSessionService(self.settings.url)
        return self._svc


@register
class UriSessions(SessionFactory):
    """Sessions chosen by one URI: ``memory://`` (default) or a SQLAlchemy async URL for durable runs.

    ``default_providers`` takes it from ``FLOW_SESSION_URI``, the same value ``adk web --session_service_uri`` takes.
    """

    name = "uri"

    class Settings(ProviderSettings):
        uri: str = "memory://"

    def __init__(self, settings: Any = None, ctx: Any = None):
        super().__init__(settings, ctx)
        self.uri = check_storage_uri(self.settings.uri)
        inner: SessionFactory = (
            MemorySessions(None, ctx) if self.uri == "memory://" else SqlAlchemySessions({"url": self.uri}, ctx)
        )
        self._inner = inner

    def service(self) -> BaseSessionService:
        """Return the in-memory or database session service the URI names."""
        return self._inner.service()

    def describe(self) -> dict[str, Any]:
        """Name the storage kind without the URI, which can hold a password."""
        return {**super().describe(), "storage": "memory" if self.uri == "memory://" else self.uri.split("://", 1)[0]}
