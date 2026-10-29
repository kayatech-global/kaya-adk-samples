"""secret: a secret value by logical name. Exports hold names only; the client binds each name to a store."""

from __future__ import annotations

import asyncio
import re
import time
from abc import abstractmethod
from pathlib import Path
from typing import Any

import httpx

from ..errors import ConfigError, KayaAdkError
from .base import HealthStatus, Provider, ProviderSettings
from .registry import register


class SecretNotFound(KayaAdkError):
    """Raised when a provider cannot supply the named secret. The message names the secret, never a value."""
    def __init__(self, name: str, provider: str):
        super().__init__(f"secret '{name}' was not found by provider '{provider}'")


class SecretFactory(Provider):
    """Port of the ``secret`` kind: resolves a logical secret name to its value."""
    kind = "secret"

    @abstractmethod
    async def get(self, name: str) -> str:
        """Return the value for the logical ``name``; raise SecretNotFound when the store has none."""

    async def get_optional(self, name: str) -> str | None:
        """Return the value for ``name``, or None when SecretNotFound is raised."""
        try:
            return await self.get(name)
        except SecretNotFound:
            return None


@register
class EnvSecrets(SecretFactory):
    """Read secrets from environment variables: ``prefix`` plus the upper-cased name with non-alphanumerics as ``_``, then the raw name."""
    name = "env"

    class Settings(ProviderSettings):
        prefix: str = ""

    async def get(self, name: str) -> str:
        """Return the environment value for ``name``; raise SecretNotFound when neither variable is set."""
        key = self.settings.prefix + re.sub(r"[^A-Za-z0-9]", "_", name).upper()
        value = self.ctx.env.get(key) or self.ctx.env.get(name)
        if value is None:
            raise SecretNotFound(name, self.name)
        return value


@register
class FileSecrets(SecretFactory):
    """Read secrets from files in ``directory``, one file per secret name."""
    name = "file"

    class Settings(ProviderSettings):
        directory: str

    async def get(self, name: str) -> str:
        """Return the stripped file content.

        Raises SecretNotFound for a missing file, and for a name containing ``/`` or ``..`` so a name cannot leave ``directory``.
        """
        if "/" in name or ".." in name:
            raise SecretNotFound(name, self.name)
        path = Path(self.settings.directory) / name
        if not path.is_file():
            raise SecretNotFound(name, self.name)
        return path.read_text().strip()

    async def health(self) -> HealthStatus:
        """Healthy when ``directory`` exists."""
        ok = Path(self.settings.directory).is_dir()
        return HealthStatus(ok, "" if ok else f"directory {self.settings.directory} does not exist")


@register
class MockSecrets(SecretFactory):
    """Serve secrets from the ``values`` setting. For tests."""
    name = "mock"

    class Settings(ProviderSettings):
        values: dict[str, str] = {}

    async def get(self, name: str) -> str:
        """Return ``values[name]``; raise SecretNotFound when absent."""
        try:
            return self.settings.values[name]
        except KeyError as exc:
            raise SecretNotFound(name, self.name) from exc


@register
class GoogleSecretManagerSecrets(SecretFactory):
    """Google Secret Manager. A logical name maps to ``projects/<project>/secrets/<name>/versions/<version>``.

    ``names`` maps logical names to secret ids when they differ. Values are cached for ``cache_seconds``.
    """

    name = "google_secret_manager"

    class Settings(ProviderSettings):
        project: str
        version: str = "latest"
        prefix: str = ""
        names: dict[str, str] = {}
        cache_seconds: float = 300

    def __init__(self, settings: Any = None, ctx: Any = None):
        super().__init__(settings, ctx)
        self._client: Any = None
        self._cache: dict[str, tuple[str, float]] = {}

    def _client_obj(self) -> Any:
        if self._client is None:
            try:
                from google.cloud import secretmanager
            except ImportError as exc:
                raise ConfigError(
                    "provider 'google_secret_manager' needs google-cloud-secret-manager: pip install google-cloud-secret-manager"
                ) from exc
            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def resource(self, name: str) -> str:
        """Return the full Secret Manager resource name for the logical ``name``."""
        secret_id = self.settings.names.get(name) or self.settings.prefix + re.sub(r"[^A-Za-z0-9_\-]", "-", name)
        return f"projects/{self.settings.project}/secrets/{secret_id}/versions/{self.settings.version}"

    async def get(self, name: str) -> str:
        """Return the secret value, served from a cache for ``cache_seconds``.

        Raises SecretNotFound for any lookup failure, with only the exception type in the message so credentials never leak.
        """
        cached = self._cache.get(name)
        if cached and cached[1] > time.time():
            return cached[0]
        resource = self.resource(name)
        try:
            response = await asyncio.to_thread(self._client_obj().access_secret_version, request={"name": resource})
        except ConfigError:
            raise
        except Exception as exc:  # NotFound, PermissionDenied, transport errors: never include the value or credentials
            raise SecretNotFound(name, f"{self.name} ({type(exc).__name__})") from exc
        value = response.payload.data.decode("utf-8")
        self._cache[name] = (value, time.time() + self.settings.cache_seconds)
        return value

    async def health(self) -> HealthStatus:
        """Healthy when the client library is installed and a client can be created."""
        try:
            self._client_obj()
            return HealthStatus(True)
        except ConfigError as exc:
            return HealthStatus(False, str(exc))
        except Exception as exc:
            return HealthStatus(False, f"cannot create Secret Manager client: {type(exc).__name__}")


@register
class HashicorpVaultSecrets(SecretFactory):
    """HashiCorp Vault KV v2 over HTTP. Logical name -> secret path; the key is the last path segment."""

    name = "hashicorp_vault"

    class Settings(ProviderSettings):
        address: str
        mount: str = "secret"
        token: str = ""
        token_env: str = "VAULT_TOKEN"
        namespace: str = ""
        paths: dict[str, str] = {}  # logical name -> "path/to/secret:key" (key defaults to the last segment)
        prefix: str = ""
        cache_seconds: float = 60
        timeout: float = 10

    transport: Any = None

    def __init__(self, settings: Any = None, ctx: Any = None):
        super().__init__(settings, ctx)
        self._cache: dict[str, tuple[str, float]] = {}

    def _token(self) -> str:
        token = self.settings.token or self.ctx.env.get(self.settings.token_env, "")
        if not token:
            raise ConfigError(f"provider 'hashicorp_vault' needs a token (setting 'token' or env {self.settings.token_env})")
        return token

    async def get(self, name: str) -> str:
        """Return the KV v2 value for ``name``, cached for ``cache_seconds``.

        Raises SecretNotFound on a transport error, a non-200 status or a missing key, and ConfigError when no token is configured.
        """
        cached = self._cache.get(name)
        if cached and cached[1] > time.time():
            return cached[0]
        target = self.settings.paths.get(name) or (self.settings.prefix + name)
        path, _, key = target.partition(":")
        key = key or path.rsplit("/", 1)[-1]
        headers = {"X-Vault-Token": self._token()}
        if self.settings.namespace:
            headers["X-Vault-Namespace"] = self.settings.namespace
        url = f"{self.settings.address.rstrip('/')}/v1/{self.settings.mount}/data/{path}"
        try:
            async with httpx.AsyncClient(timeout=self.settings.timeout, transport=self.transport) as client:
                resp = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise SecretNotFound(name, f"{self.name} ({type(exc).__name__})") from exc
        if resp.status_code != 200:
            raise SecretNotFound(name, f"{self.name} (HTTP {resp.status_code})")
        try:
            body = resp.json()
            data = (body.get("data") or {}).get("data") or {}
        except (ValueError, AttributeError) as exc:  # a proxy's HTML page, or JSON that is not Vault's object
            raise SecretNotFound(name, f"{self.name} (unexpected response)") from exc
        if not isinstance(data, dict) or key not in data:
            raise SecretNotFound(name, f"{self.name} (key '{key}' missing)")
        value = str(data[key])
        self._cache[name] = (value, time.time() + self.settings.cache_seconds)
        return value

    async def health(self) -> HealthStatus:
        """Healthy when a token is available. Makes no request."""
        try:
            self._token()
        except ConfigError as exc:
            return HealthStatus(False, str(exc))
        return HealthStatus(True)
