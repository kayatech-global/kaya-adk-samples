"""Placeholder rendering. Placeholder forms: {{Variable:name}}, Variable:name, @name and {name}.

A name is a letter or underscore followed by word characters, with at most one dot. Rendering is done here, not by
ADK's own templating, because names may contain a dot. Unresolved placeholders are left as written and reported.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

_NAME = r"[A-Za-z_]\w*(?:\.\w+)?"
_PATTERNS = [
    re.compile(r"\{\{\s*Variable:(" + _NAME + r")\s*\}\}"),
    re.compile(r"(?<![\w.])Variable:(" + _NAME + r")"),
    re.compile(r"(?<![\w.@])@(" + _NAME + r")"),
    re.compile(r"\{(" + _NAME + r")\}"),
]


@dataclass
class Rendered:
    """Rendered text with the variable names that were substituted and those left unresolved."""
    text: str
    used: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def stringify(value: Any) -> str:
    """Convert a value to text: JSON for containers, lowercase true/false for booleans, empty for None."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, default=str, sort_keys=True)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def render(text: str, variables: Mapping[str, Any]) -> Rendered:
    """Substitute variable placeholders in text.

    Unresolved placeholders stay as written and are listed in ``missing``.
    """
    used: list[str] = []
    missing: list[str] = []

    def lookup(name: str) -> tuple[bool, Any]:
        if name in variables:
            return True, variables[name]
        if "." in name:
            head, tail = name.split(".", 1)
            node = variables.get(head)
            if isinstance(node, Mapping) and tail in node:
                return True, node[tail]
        return False, None

    out = text
    for pat in _PATTERNS:

        def repl(m: re.Match[str]) -> str:
            name = m.group(1)
            found, value = lookup(name)
            if not found:
                if name not in missing:
                    missing.append(name)
                return m.group(0)
            if name not in used:
                used.append(name)
            return stringify(value)

        out = pat.sub(repl, out)
    return Rendered(out, used, missing)


def render_structure(value: Any, variables: Mapping[str, Any]) -> Any:
    """Render every string inside a JSON-like structure. A string that is exactly one placeholder keeps its value type."""
    if isinstance(value, str):
        for pat in _PATTERNS[:2]:
            m = pat.fullmatch(value.strip())
            if m and m.group(1) in variables:
                return variables[m.group(1)]
        return render(value, variables).text
    if isinstance(value, dict):
        return {k: render_structure(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [render_structure(v, variables) for v in value]
    return value
