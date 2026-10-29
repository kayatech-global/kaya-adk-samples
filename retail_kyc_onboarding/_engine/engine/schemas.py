"""Structured-output field specs to pydantic models (the shape ADK's output_schema expects)."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, create_model

_SCALARS: dict[str, type] = {
    "str": str,
    "string": str,
    "int": int,
    "integer": int,
    "float": float,
    "number": float,
    "bool": bool,
    "boolean": bool,
}

# Open-ended types have no item or property schema, which OpenAI strict structured output (ADK's LiteLlm always sends
# strict) rejects. Like Kaya, the model is asked for them as JSON text and the text is decoded afterwards.
_OPEN = {"dict", "dictionary", "object", "list", "array", "any"}


def _type_name(spec: Any) -> str:
    return str(spec.get("type", "string") if isinstance(spec, dict) else spec).lower().strip()


def _field(spec: Any) -> Any:
    name = _type_name(spec)
    if isinstance(spec, dict) and name == "enum":
        return (Literal[tuple(spec["values"])], ...)
    if name in _OPEN:
        example = '["a", "b"]' if name in ("list", "array") else '{"key": "value"}'
        return (str, Field(..., description=f"A JSON-encoded {name} as a string, for example '{example}'."))
    return (_SCALARS.get(name, str), ...)


def fields_to_model(name: str, fields: dict[str, Any]) -> type[BaseModel]:
    """Build a pydantic model from a field spec for ADK's output_schema.

    Every field is required; an unknown type falls back to str; open-ended types (array, object, any) are JSON text.
    """
    safe = "".join(c if c.isalnum() else "_" for c in name) or "Output"
    return create_model(safe, **{k: _field(v) for k, v in fields.items()})  # type: ignore[call-overload]


def decode_open_fields(fields: dict[str, Any], out: dict[str, Any]) -> dict[str, Any]:
    """The answer with its JSON-text fields decoded back to the declared open type; text that is not JSON stays as it is."""
    decoded = dict(out)
    for k, spec in fields.items():
        value = decoded.get(k)
        if _type_name(spec) in _OPEN and isinstance(value, str):
            try:
                decoded[k] = json.loads(value)
            except ValueError:
                pass
    return decoded
