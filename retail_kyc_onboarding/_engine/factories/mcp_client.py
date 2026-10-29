"""mcp: real MCP client (streamable HTTP, SSE, stdio). One short session per call keeps the provider stateless."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

from ..datatypes import CallContext, ToolDef, ToolResult
from ..errors import ToolError
from ..trace.context import outbound_headers
from .api import ToolHandle
from .base import ProviderSettings
from .mcp import McpFactory
from .registry import register

log = logging.getLogger("flow.mcp")


class McpToolHandle(ToolHandle):
    """One tool of a remote MCP server, exposed as a local ToolHandle."""
    def __init__(self, definition: ToolDef, remote_name: str, schema: dict[str, Any], factory: McpClientFactory, server: ToolDef):
        super().__init__(definition)
        self.remote_name, self.schema, self.f, self.server = remote_name, schema, factory, server

    def json_schema(self) -> dict[str, Any]:
        """Return the schema the server advertised, or the base handle's schema when it sent none."""
        return self.schema or super().json_schema()

    async def invoke(self, args: dict[str, Any], call: CallContext) -> ToolResult:
        """Call the remote tool in a fresh MCP session.

        Failures come back as an unsuccessful ToolResult instead of an exception, so one broken server does not abort the run.
        """
        try:
            async with self.f.session(self.server, call) as session:
                res = await session.call_tool(self.remote_name, args)
        except Exception as exc:
            return ToolResult(False, error=f"MCP call failed: {type(exc).__name__}: {exc}")
        text = "\n".join(getattr(c, "text", "") or "" for c in (res.content or []) if getattr(c, "type", "") == "text")
        data = getattr(res, "structuredContent", None)
        if res.isError:
            return ToolResult(False, text=text, error=text or "MCP tool reported an error")
        return ToolResult(True, text=text, data=data if data is not None else _maybe_json(text))


def _maybe_json(text: str) -> Any:
    import json

    try:
        return json.loads(text)
    except ValueError:
        return None


@register
class McpClientFactory(McpFactory):
    """Tools from MCP servers over streamable HTTP, SSE or stdio, using one short session per call so no connection state is held."""
    name = "mcp_client"

    class Settings(ProviderSettings):
        allow_hosts: list[str] = []
        required: bool = False
        timeout: float = 30

    def _check_host(self, url: str) -> None:
        if self.settings.allow_hosts and (urlparse(url).hostname or "") not in self.settings.allow_hosts:
            raise ToolError(f"MCP host '{urlparse(url).hostname}' is not in allow_hosts")

    async def _headers(self, server: ToolDef, call: CallContext | None) -> dict[str, str]:
        cfg = server.config
        headers: dict[str, str] = {}
        for h in cfg.get("headers") or []:
            if h.get("active", True) is False:
                continue
            headers[h["name"]] = await self.ctx.provider("secret").get(h["secret"]) if h.get("secret") else h.get("value", "")
        auth = cfg.get("authorization") or {}
        if auth.get("token_secret"):
            headers["Authorization"] = f"Bearer {await self.ctx.provider('secret').get(auth['token_secret'])}"
        if call is not None:
            headers.update(dict((await self.ctx.provider("credentials").get(f"mcp:{server.name}", call.principal, None)).headers))
        return {**headers, **outbound_headers()}

    @asynccontextmanager
    async def session(self, server: ToolDef, call: CallContext | None = None):
        """Open an initialised MCP client session to ``server`` and close it on exit.

        The transport comes from the server config and defaults to streamable HTTP. ``call`` supplies the principal for per-target credentials; None skips them.
        Raises ToolError when a URL host is outside ``allow_hosts``.
        """
        from mcp import ClientSession

        cfg = server.config
        transport = str(cfg.get("transport", "streamable_http")).lower().replace("-", "_")
        if transport == "stdio":
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            params = StdioServerParameters(command=cfg["command"], args=list(cfg.get("args") or []), env=cfg.get("env") or None)
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                yield session
            return
        url = str(cfg["url"])
        self._check_host(url)
        headers = await self._headers(server, call)
        if transport == "sse":
            from mcp.client.sse import sse_client

            async with (
                sse_client(url, headers=headers, timeout=self.settings.timeout) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                yield session
        else:
            from mcp.client.streamable_http import streamablehttp_client

            async with (
                streamablehttp_client(url, headers=headers, timeout=self.settings.timeout) as (read, write, _),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                yield session

    async def build(self, tool: ToolDef) -> list[ToolHandle]:
        """Connect to the server and return a handle per tool it lists, limited to ``selected_tools`` when set.

        An unreachable server yields no handles and a warning, unless ``required`` is set, in which case the error propagates.
        """
        server_name = tool.config.get("server_name") or tool.name
        wanted = set(tool.config.get("selected_tools") or [])
        try:
            async with self.session(tool) as session:
                listed = (await session.list_tools()).tools
        except Exception as exc:
            if self.settings.required:
                raise
            log.warning("MCP server %s is not reachable: %s", server_name, exc)
            return []
        handles: list[ToolHandle] = []
        for t in listed:
            prefixed = f"{server_name}_{t.name}"
            if wanted and prefixed not in wanted and t.name not in wanted:
                continue
            definition = ToolDef(
                prefixed, "mcp", t.description or "", config=tool.config, version=tool.version, approval=_approval_for(tool, t.name)
            )
            handles.append(McpToolHandle(definition, t.name, dict(t.inputSchema or {}), self, tool))
        return handles


def _approval_for(server: ToolDef, remote_name: str) -> dict[str, Any]:
    per_tool = (server.approval or {}).get("tools", {})
    return dict(per_tool.get(remote_name) or (server.approval or {}).get("default") or {})
