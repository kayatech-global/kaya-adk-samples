"""mcp: tools served by an MCP server. Real transports are in factories/mcp_client.py."""

from __future__ import annotations

from abc import abstractmethod
from typing import Any

from ..datatypes import ToolDef
from .api import MockToolHandle, ToolHandle
from .base import Provider, ProviderSettings
from .registry import register


class McpFactory(Provider):
    """Factory kind for tools served by an MCP server."""

    kind = "mcp"

    @abstractmethod
    async def build(self, tool: ToolDef) -> list[ToolHandle]:
        """Handles for the tools of one server definition (ToolDef.config: url, transport, selected_tools)."""


@register
class MockMcpFactory(McpFactory):
    """Test provider: one mock handle per selected tool, answering from ``responses``."""

    name = "mock"

    class Settings(ProviderSettings):
        responses: dict[str, Any] = {}

    async def build(self, tool: ToolDef) -> list[ToolHandle]:
        """Return a mock handle for each name in ``selected_tools``, defaulting to the tool's own name."""
        names = list(tool.config.get("selected_tools") or [tool.name])
        return [
            MockToolHandle(
                ToolDef(n, "mcp", tool.description, tool.parameters, config=tool.config), self.settings.responses.get(n, {"ok": True})
            )
            for n in names
        ]
