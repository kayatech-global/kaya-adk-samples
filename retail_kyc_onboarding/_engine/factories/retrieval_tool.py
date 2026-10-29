"""A knowledge-base tool backed by the retrieval factory. The source RAG pipeline (retrievers, reranker, HyDE) is not exported:
the client's retrieval provider decides how a query becomes passages."""

from __future__ import annotations

import json
from typing import Any

from ..datatypes import CallContext, ToolDef, ToolResult
from .api import ToolHandle
from .misc import RetrievalFactory


class RetrievalToolHandle(ToolHandle):
    """Tool handle that searches a retrieval store with a free-text query."""

    def __init__(self, definition: ToolDef, retrieval: RetrievalFactory):
        super().__init__(definition)
        self.retrieval = retrieval

    def json_schema(self) -> dict[str, Any]:
        """Fixed schema: one required ``query`` string, whatever the definition declares."""
        return {"type": "object", "properties": {"query": {"type": "string", "description": "what to look for"}}, "required": ["query"]}

    async def invoke(self, args: dict[str, Any], call: CallContext) -> ToolResult:
        """Search the store named by config ``store`` (default: the tool name) for ``top_k`` passages; a blank query or a failed search returns ``ok=False``."""
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult(False, error="a query is required")
        cfg = self.definition.config
        try:
            results = await self.retrieval.search(str(cfg.get("store") or self.definition.name), query, int(cfg.get("top_k", 5)))
        except Exception as exc:
            return ToolResult(False, error=f"retrieval failed: {type(exc).__name__}: {exc}")
        return ToolResult(True, text=json.dumps(results, default=str), data=results)
