"""Error types. Messages name the kind, provider and setting so a client can fix them without reading code."""

from __future__ import annotations


class KayaAdkError(Exception):
    """Base class for every error raised by the core."""


class ConfigError(KayaAdkError):
    """A setting, a provider or an override is invalid."""


class ProviderNotFound(ConfigError):
    """No provider is registered for a factory kind; the message lists the known providers."""

    def __init__(self, kind: str, provider: str, known: list[str] | None = None):
        hint = f" Known providers: {', '.join(sorted(known))}." if known else ""
        super().__init__(f"No provider '{provider}' for factory kind '{kind}'.{hint}")
        self.kind, self.provider = kind, provider


class ProviderHealthError(KayaAdkError):
    """A provider failed its health check at startup or on request."""

    def __init__(self, kind: str, provider: str, detail: str):
        super().__init__(f"Provider '{provider}' for '{kind}' failed its health check: {detail}")
        self.kind, self.provider = kind, provider


class ExportIntegrityError(KayaAdkError):
    """An export file does not match its manifest hash, or the manifest is invalid."""


class CoreVersionError(KayaAdkError):
    """The export requires a different core version."""


class ConditionError(KayaAdkError):
    """An edge or branch condition cannot be parsed or uses a forbidden construct."""


class PolicyDenied(KayaAdkError):
    """The authorization provider denied an action on a resource."""

    def __init__(self, action: str, resource: str, reason: str = ""):
        super().__init__(f"Denied: {action} on {resource}" + (f" ({reason})" if reason else ""))
        self.action, self.resource, self.reason = action, resource, reason


class ToolError(KayaAdkError):
    """A tool call failed. Never contains credentials."""


class AuthenticationError(KayaAdkError):
    """The caller could not be authenticated."""


class RunError(KayaAdkError):
    """A run could not start or continue."""



class RunNotFound(RunError):
    """No session or task with this id exists for this export."""
