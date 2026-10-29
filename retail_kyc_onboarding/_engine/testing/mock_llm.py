"""Scripted ADK model for tests and offline demos. Deterministic: first matching rule wins."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncGenerator
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import Field


def _last_user_text(req: LlmRequest) -> str:
    for content in reversed(req.contents or []):
        if content.role == "user":
            texts = [p.text for p in (content.parts or []) if getattr(p, "text", None)]
            if texts:
                return "\n".join(texts)
    return ""


def _last_function_response(req: LlmRequest) -> types.FunctionResponse | None:
    contents = req.contents or []
    if not contents:
        return None
    for part in contents[-1].parts or []:
        if getattr(part, "function_response", None):
            return part.function_response
    return None


def _system_text(req: LlmRequest) -> str:
    si = getattr(req.config, "system_instruction", None) if req.config else None
    if si is None:
        return ""
    if isinstance(si, str):
        return si
    parts = getattr(si, "parts", None) or []
    return "\n".join(getattr(p, "text", "") or "" for p in parts)


def _response_schema(req: LlmRequest) -> dict[str, Any] | None:
    """The JSON schema the request asks the answer to follow (ADK sets it from an agent's ``output_schema``), or None."""
    schema = getattr(req.config, "response_schema", None) if req.config else None
    if isinstance(schema, type) and hasattr(schema, "model_json_schema"):
        return schema.model_json_schema()  # type: ignore[no-any-return]
    return schema if isinstance(schema, dict) else None


def _sample(schema: dict[str, Any], defs: dict[str, Any], text: str) -> Any:
    """The smallest value that validates against ``schema``; strings are ``text``."""
    if "$ref" in schema:
        return _sample(defs.get(schema["$ref"].rsplit("/", 1)[-1], {}), defs, text)
    if "enum" in schema:
        return schema["enum"][0]
    for key in ("anyOf", "oneOf", "allOf"):
        options = [o for o in schema.get(key, []) if o.get("type") != "null"]
        if options:
            return _sample(options[0], defs, text)
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        return {name: _sample(sub, defs, text) for name, sub in schema.get("properties", {}).items()}
    return {"array": [], "integer": 0, "number": 0, "boolean": False, "null": None}.get(kind, text)  # type: ignore[arg-type]


class MockLlm(BaseLlm):
    """Rules: {"contains": regex over system+user text, "after_tool": name, "text": str, "json": obj,
    "tool_call": {"name": str, "args": {...}}, "once": bool}. No rule matched: reply ``default``, or, when the request
    carries a response schema, the smallest JSON object that follows it (string fields set to ``default``)."""

    model: str = "mock-model"
    script: list[dict[str, Any]] = Field(default_factory=list)
    default: str = "ok"
    calls: list[dict[str, Any]] = Field(default_factory=list)
    used: list[int] = Field(default_factory=list)

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        """Yield one scripted response: the first matching rule, else ``default``; each request is recorded in ``calls``."""
        user, system = _last_user_text(llm_request), _system_text(llm_request)
        fr = _last_function_response(llm_request)
        tool_names = (
            [d.name for t in (llm_request.config.tools or []) for d in (getattr(t, "function_declarations", None) or [])]
            if llm_request.config
            else []
        )
        self.calls.append(
            {
                "system": system,
                "user": user,
                "tools": tool_names,
                "after_tool": fr.name if fr else None,
                "schema": bool(getattr(llm_request.config, "response_schema", None)) if llm_request.config else False,
            }
        )
        for i, rule in enumerate(self.script):
            if rule.get("once") and i in self.used:
                continue
            if "after_tool" in rule and (fr is None or fr.name != rule["after_tool"]):
                continue
            if "after_tool" not in rule and fr is not None and rule.get("only_fresh", True) and "contains" not in rule:
                continue
            if "contains" in rule and not re.search(rule["contains"], f"{system}\n{user}", re.S | re.I):
                continue
            self.used.append(i)
            yield self._response(rule)
            return
        schema = _response_schema(llm_request)
        text = json.dumps(_sample(schema, schema.get("$defs", {}), self.default)) if schema else self.default
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]), usage_metadata=_usage(user))

    def _response(self, rule: dict[str, Any]) -> LlmResponse:
        if "tool_call" in rule:
            tc = rule["tool_call"]
            part = types.Part(function_call=types.FunctionCall(name=tc["name"], args=tc.get("args", {})))
        elif "json" in rule:
            part = types.Part(text=json.dumps(rule["json"]))
        else:
            part = types.Part(text=str(rule.get("text", self.default)))
        return LlmResponse(content=types.Content(role="model", parts=[part]), usage_metadata=_usage(""))


def _usage(text: str) -> types.GenerateContentResponseUsageMetadata:
    n = max(1, len(text.split()))
    return types.GenerateContentResponseUsageMetadata(prompt_token_count=n, candidates_token_count=3, total_token_count=n + 3)
