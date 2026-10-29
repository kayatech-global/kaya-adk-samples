"""api: a callable tool for a REST API definition. Path params, query and body are handled as the tool definition says."""

from __future__ import annotations

import asyncio
import re
import time
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from ..datatypes import CallContext, ToolDef, ToolResult
from ..errors import ToolError
from ..trace.context import traced_client
from .base import Provider, ProviderSettings
from .registry import register

_PATH_PARAM = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ToolHandle(ABC):
    """What the engine holds for one tool: a declaration for models and an invoke for execution."""

    def __init__(self, definition: ToolDef):
        self.definition = definition

    @property
    def name(self) -> str:
        """Tool name from the definition."""
        return self.definition.name

    def json_schema(self) -> dict[str, Any]:
        """JSON schema of the tool arguments shown to the model; only ``parameters`` are required, ``optional_parameters`` are not."""
        props: dict[str, Any] = {}
        for pname, spec in {**self.definition.parameters, **self.definition.optional_parameters}.items():
            props[pname] = {"type": _json_type(spec.get("type")), "description": spec.get("description", "")}
        return {"type": "object", "properties": props, "required": list(self.definition.parameters)}

    @abstractmethod
    async def invoke(self, args: dict[str, Any], call: CallContext) -> ToolResult:
        """Execute the tool with model-supplied arguments.

        Args:
            args: Argument values keyed by parameter name.
            call: Identity, variables and idempotency key of the run in progress.

        Returns:
            The outcome. Call failures come back as ``ok=False``; configuration and policy violations may raise ToolError.
        """


def _json_type(t: Any) -> str:
    t = str(t or "string").lower()
    return {
        "str": "string",
        "string": "string",
        "int": "integer",
        "integer": "integer",
        "float": "number",
        "number": "number",
        "bool": "boolean",
        "boolean": "boolean",
        "list": "array",
        "array": "array",
        "dict": "object",
        "object": "object",
    }.get(t, "string")


class ApiFactory(Provider):
    """Factory kind for REST API tools: turns a tool definition into an invokable handle."""

    kind = "api"

    @abstractmethod
    def build(self, tool: ToolDef) -> ToolHandle:
        """Return the handle that executes ``tool``."""


class AuthHeaders:
    """Authorization headers for an outbound call, from the normalized ``authorization`` block of a tool or agent definition.

    Secrets come from the secret factory by logical name. OAuth client-credentials tokens are cached until shortly before they expire.
    """

    def __init__(self, ctx: Any, authorization: dict[str, Any] | None, label: str, forward_sso: bool = False):
        self.ctx, self.auth, self.label, self.forward_sso = ctx, dict(authorization or {}), label, forward_sso
        self._token_cache: dict[str, tuple[str, float]] = {}

    async def _secret(self, name: str) -> str:
        return await self.ctx.provider("secret").get(name)

    async def headers(self, call: CallContext) -> dict[str, str]:
        """Authorization headers for one outbound call; empty for no-auth.

        The ``sso`` type adds the principal token only when ``forward_sso`` is set.

        Raises:
            ToolError: The authorization type is not supported.
        """
        auth = self.auth
        kind = str(auth.get("type", "no-auth")).lower().replace("_", "-")
        if kind in {"no-auth", "none", ""}:
            return {}
        if kind in {"bearer-token", "bearertoken", "bearer"}:
            return {"Authorization": f"{auth.get('header_prefix', 'Bearer')} {await self._secret(auth['token_secret'])}"}
        if kind in {"basic-auth", "basicauth", "basic"}:
            import base64

            user = auth.get("username") or await self._secret(auth["username_secret"])
            pw = await self._secret(auth["password_secret"])
            return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}
        if kind in {"api-key", "apikey"}:
            return {auth.get("header_name", "x-api-key"): await self._secret(auth["key_secret"])}
        if kind == "oauth2":
            return {"Authorization": f"Bearer {await self._oauth_token(auth)}"}
        if kind == "sso":
            token = call.principal.token_ref  # resolved by the credentials provider; nothing forwarded here
            return {"Authorization": f"Bearer {token}"} if token and self.forward_sso else {}
        raise ToolError(f"{self.label}: unsupported authorization type '{kind}'")

    async def _oauth_token(self, auth: dict[str, Any]) -> str:
        cached = self._token_cache.get("cc")
        if cached and cached[1] > time.time() + 30:
            return cached[0]
        data = {
            "grant_type": auth.get("grant_type", "client_credentials"),
            "client_id": auth["client_id"],
            "client_secret": await self._secret(auth["client_secret_secret"]),
        }
        for key in ("scope", "audience"):
            if auth.get(key):
                data[key] = auth[key]
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(auth["token_url"], data=data)
        if resp.status_code >= 400:
            raise ToolError(f"{self.label}: token endpoint returned HTTP {resp.status_code}")
        body = resp.json()
        self._token_cache["cc"] = (body["access_token"], time.time() + float(body.get("expires_in", 300)))
        return body["access_token"]


class HttpToolHandle(ToolHandle):
    """Runs a tool over HTTP: fills path parameters, splits query and body by method, applies headers and authorization, retries 5xx."""

    def __init__(self, definition: ToolDef, factory: HttpApiFactory):
        super().__init__(definition)
        self.f = factory
        self._auth = AuthHeaders(
            factory.ctx, definition.config.get("authorization"), f"tool '{definition.name}'", factory.settings.forward_sso
        )

    async def _secret(self, name: str) -> str:
        return await self.f.ctx.provider("secret").get(name)

    async def _auth_headers(self, call: CallContext) -> dict[str, str]:
        return await self._auth.headers(call)

    async def invoke(self, args: dict[str, Any], call: CallContext) -> ToolResult:
        """Call the configured URL and wrap the response.

        A missing or unsafe path parameter returns ``ok=False``. Only 5xx responses and transport errors are retried (``retry_attempts``); 4xx is returned as is.

        Raises:
            ToolError: The URL host is not in ``allow_hosts``, or the authorization type is unsupported.
        """
        cfg = self.definition.config
        method = str(cfg.get("method", "GET")).upper()
        url_template = str(cfg["url"])
        self.f.check_host(url_template)
        defaults: dict[str, Any] = {}
        for k, v in (cfg.get("default_api_parameters") or {}).items():
            if not isinstance(v, dict):
                continue
            if v.get("variable") and v["variable"] in call.variables:  # a default bound to a workflow variable
                defaults[k] = call.variables[v["variable"]]
            elif v.get("value") is not None:
                defaults[k] = v["value"]
        merged = {**defaults, **args}
        path_names = set(_PATH_PARAM.findall(url_template))
        url = url_template
        for pn in path_names:
            if pn not in merged:
                return ToolResult(False, error=f"missing path parameter '{pn}'", status=None)
            value = str(merged[pn])
            if value in {".", ".."}:
                return ToolResult(False, error=f"path parameter '{pn}' is not a valid path segment")
            url = url.replace("{" + pn + "}", quote(value, safe=""))  # one segment: no '/', '?' or '#' can escape it
        self.f.check_host(url)
        rest = {k: v for k, v in merged.items() if k not in path_names}
        query: dict[str, Any] = dict(cfg.get("default_query_parameters") or {})
        body: Any = None
        if method in {"POST", "PUT", "PATCH"}:
            qnames = set(cfg.get("query_param_names") or [])
            query.update({k: v for k, v in rest.items() if k in qnames})
            payload = {k: v for k, v in rest.items() if k not in qnames}
            body = payload
        else:
            query.update(rest)
        headers: dict[str, str] = {}
        for h in cfg.get("headers") or []:
            if h.get("active", True) is False:
                continue
            value = await self._secret(h["secret"]) if h.get("secret") else h.get("value", "")
            headers[h["name"]] = value
        headers.update(await self._auth_headers(call))
        headers.update(dict((await self.f.ctx.provider("credentials").get(f"tool:{self.name}", call.principal, None)).headers))
        if call.idempotency_key and method in {"POST", "PUT", "PATCH"}:
            headers.setdefault("Idempotency-Key", call.idempotency_key)
        kwargs: dict[str, Any] = {"params": {k: _q(v) for k, v in query.items()}, "headers": headers}
        if body is not None:
            if cfg.get("body_mode") == "raw" and "body" in merged:
                kwargs["content"] = str(merged["body"])
                headers.setdefault("Content-Type", cfg.get("raw_content_type", "text/plain"))
            else:
                kwargs["json"] = body
        attempts = int(cfg.get("retry_attempts", 0)) + 1
        timeout = float(cfg.get("timeout", self.f.settings.default_timeout))
        last: ToolResult | None = None
        for attempt in range(attempts):
            try:
                async with traced_client(timeout=timeout, transport=self.f.transport) as client:
                    resp = await client.request(method, url, **kwargs)
                text = resp.text
                try:
                    data = resp.json()
                except ValueError:
                    data = None
                last = ToolResult(
                    resp.status_code < 400,
                    text=text,
                    data=data,
                    status=resp.status_code,
                    error="" if resp.status_code < 400 else f"HTTP {resp.status_code}: {text[:500]}",
                )
                if resp.status_code < 500:
                    return last
            except httpx.HTTPError as exc:
                last = ToolResult(False, error=f"{type(exc).__name__}: {exc}")
            if attempt + 1 < attempts:
                await asyncio.sleep(min(2.0, 0.2 * 2**attempt))
        return last or ToolResult(False, error="no attempt made")


def _q(v: Any) -> Any:
    return str(v).lower() if isinstance(v, bool) else v


@register
class HttpApiFactory(ApiFactory):
    """Provider that runs API tools over HTTP, optionally limited to ``allow_hosts``."""

    name = "http"

    class Settings(ProviderSettings):
        default_timeout: float = 30
        allow_hosts: list[str] = []
        forward_sso: bool = False

    transport: httpx.AsyncBaseTransport | None = None  # tests inject httpx.MockTransport

    def check_host(self, url: str) -> None:
        """Raise ToolError when ``allow_hosts`` is set and the URL host is not in it; no-op when it is empty."""
        if not self.settings.allow_hosts:
            return
        host = urlparse(url).hostname or ""
        if host not in self.settings.allow_hosts:
            raise ToolError(f"host '{host}' is not in the api allow_hosts list")

    def build(self, tool: ToolDef) -> ToolHandle:
        """Return an HttpToolHandle for ``tool``."""
        return HttpToolHandle(tool, self)


class MockToolHandle(ToolHandle):
    """Test handle that records every call and answers from a canned value or a callable."""

    def __init__(self, definition: ToolDef, responder: Any):
        super().__init__(definition)
        self.responder = responder
        self.calls: list[dict[str, Any]] = []

    async def invoke(self, args: dict[str, Any], call: CallContext) -> ToolResult:
        """Record ``args`` and answer: a ToolResult is passed through, anything else is wrapped as a successful result."""
        self.calls.append(dict(args))
        out = self.responder(args) if callable(self.responder) else self.responder
        if isinstance(out, ToolResult):
            return out
        import json

        return ToolResult(True, text=json.dumps(out) if isinstance(out, (dict, list)) else str(out), data=out)


@register
class MockApiFactory(ApiFactory):
    """Test provider: every tool answers from ``responses`` by tool name, defaulting to ``{"ok": True}``."""

    name = "mock"

    class Settings(ProviderSettings):
        responses: dict[str, Any] = {}

    def __init__(self, settings: Any = None, ctx: Any = None):
        super().__init__(settings, ctx)
        self.handles: dict[str, MockToolHandle] = {}

    def build(self, tool: ToolDef) -> ToolHandle:
        """Return a MockToolHandle and keep it in ``handles`` by tool name so tests can inspect its calls."""
        h = MockToolHandle(tool, self.settings.responses.get(tool.name, {"ok": True}))
        self.handles[tool.name] = h
        return h
