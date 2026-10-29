"""Template semantics for tool and Sub-IFLOW data mapping.

* Request structures are JSON text with variable tokens. A token inside a JSON string becomes escaped text; a token at a
  value position becomes that value's JSON.
* Output structures mirror the response body with ``{{Variable:name}}`` tokens at leaves. Keys may end with ``[n]``,
  ``[-n]`` or ``[*]``. A non-object response is exposed under the reserved key ``results``. Mapping never raises.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any

_TOKEN = re.compile(
    r"\{\{\s*Variable:([A-Za-z_]\w*(?:\.\w+)?)\s*\}\}|(?<![\w.])Variable:([A-Za-z_]\w*(?:\.\w+)?)|(?<![\w.@])@([A-Za-z_]\w*(?:\.\w+)?)|\{([A-Za-z_]\w*(?:\.\w+)?)\}"
)
_NAME_OK = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)?$")  # one dotted segment is allowed: node_prefix.leaf
_KEY_SUBSCRIPT = re.compile(r"^(?P<key>.+?)\[(?P<idx>-?\d+|\*)\]$")


def _lookup(variables: Mapping[str, Any], name: str) -> tuple[bool, Any]:
    if name in variables:
        return True, variables[name]
    if "." in name:
        head, tail = name.split(".", 1)
        node = variables.get(head)
        if isinstance(node, Mapping) and tail in node:
            return True, node[tail]
    return False, None


def substitute_json_safe(template: str, variables: Mapping[str, Any]) -> str:
    """Replace tokens so the result stays valid JSON. Unknown tokens are left alone (and will usually fail to parse)."""
    out: list[str] = []
    i, in_string = 0, False
    while i < len(template):
        ch = template[i]
        if in_string:
            if ch == "\\" and i + 1 < len(template):
                out.append(template[i : i + 2])
                i += 2
                continue
            if ch == '"':
                in_string = False
                out.append(ch)
                i += 1
                continue
        elif ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        m = _TOKEN.match(template, i)
        if m:
            name = next(g for g in m.groups() if g)
            found, value = _lookup(variables, name)
            if found:
                if in_string:
                    out.append(
                        json.dumps(
                            value
                            if isinstance(value, str)
                            else json.dumps(value, default=str)
                            if isinstance(value, (dict, list))
                            else str(value)
                        )[1:-1]
                    )
                else:
                    out.append(json.dumps(value, default=str))
                i = m.end()
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def parse_request(template: Any, variables: Mapping[str, Any]) -> dict[str, Any]:
    """Tool arguments from a request structure (JSON text or an already parsed object)."""
    if template is None or template == "":
        return {}
    if isinstance(template, str):
        try:
            data = json.loads(substitute_json_safe(template, variables))
        except ValueError:
            return {}
    else:
        from .render import render_structure

        data = render_structure(template, variables)
    if not isinstance(data, dict):
        return {}
    if "args" in data and isinstance(data["args"], dict) and "query" in data:  # legacy MCP wrapper
        return dict(data["args"])
    return data


def _token_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    m = _TOKEN.fullmatch(value.strip())
    if not m:
        return None
    name = next(g for g in m.groups() if g)
    return name if _NAME_OK.match(name) else None


def _walk(template: Any, data: Any, set_var: Callable[[str, Any], None]) -> None:
    if not isinstance(template, dict):
        return
    for key, sub in template.items():
        idx: str | None = None
        m = _KEY_SUBSCRIPT.match(key)
        if m:
            key, idx = m.group("key"), m.group("idx")
        if not isinstance(data, dict) or key not in data:
            continue
        value = data[key]
        if idx is not None:
            if not isinstance(value, list):
                continue
            if idx == "*":
                items = value
                if isinstance(sub, dict):
                    collected: dict[str, list[Any]] = {}
                    for item in items:
                        _collect(sub, item, collected)
                    for var, vals in collected.items():
                        set_var(var, vals)
                else:
                    name = _token_name(sub)
                    if name:
                        set_var(name, list(items))
                continue
            n = int(idx)
            if not -len(value) <= n < len(value):
                continue
            value = value[n]
        name = _token_name(sub)
        if name is not None:
            set_var(name, value)
        elif isinstance(sub, dict):
            _walk(sub, value, set_var)


def _collect(template: dict[str, Any], item: Any, out: dict[str, list[Any]]) -> None:
    def put(name: str, value: Any) -> None:
        out.setdefault(name, []).append(value)

    _walk(template, item, put)


_JSON_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
_UNQUOTED_TOKEN = re.compile(
    r"\{\{(?:Agent|API|Variable|MCP|VectorRAG|GraphRAG|Metadata|Attribute):[^{}]+\}\}|\b(?:Agent|API|Variable|MCP|VectorRAG|GraphRAG|Metadata|Attribute):[\w.-]+"
)
_STASH = re.compile("\x00(\\d+)\x00")


def normalize_output_template(template: str) -> str:
    """Quote bare variable tokens so the template parses as JSON: ``{"a": {{Variable:x}}}`` and ``{"a": Variable:x}``
    are the forms authors write. Strings that are already quoted are left alone."""
    stash: list[str] = []

    def protect(m: re.Match[str]) -> str:
        stash.append(m.group(0))
        return f"\x00{len(stash) - 1}\x00"

    protected = _JSON_STRING.sub(protect, template)
    quoted = _UNQUOTED_TOKEN.sub(lambda m: json.dumps(m.group(0)), protected)
    return _STASH.sub(lambda m: stash[int(m.group(1))], quoted)


def apply_output_mapping(template: Any, response: Any, set_var: Callable[[str, Any], None]) -> None:
    """Write response values into variables per the template. Fail-open: nothing here raises."""
    try:
        if isinstance(template, str):
            template = json.loads(normalize_output_template(template))
        if not isinstance(template, dict):
            return
        data: Any = response
        if isinstance(response, str):
            try:
                data = json.loads(response)
            except ValueError:
                data = response
        if not isinstance(data, dict):
            data = {"results": data}
        _walk(template, data, set_var)
    except Exception:  # a bad mapping must not break tool execution
        return
