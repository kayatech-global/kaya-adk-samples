"""Parity of decisions between a recorded run (for example from the source platform) and a run of the export.

Parity is defined on decisions, not on wording: the order of steps, the tool calls, the routes taken and the variables.
Recorded format (JSON):
    {"steps": ["a", "b"], "tools": [{"node": "b", "tool": "t", "args": {...}}], "routes": {"a": "b"}, "variables": {"x": 1}}
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def summarize(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Reduce trace records to the steps, tool calls and routes of the top-level run."""
    recs = [r for r in records if not r.get("scope")]
    return {
        "steps": [r["node"] for r in recs if r.get("event") == "step.start"],
        "tools": [
            {"node": r.get("node"), "tool": r.get("tool"), "args": r.get("args", {})}
            for r in recs
            if r.get("event") == "tool.call" and not r.get("denied")
        ],
        "routes": {r["node"]: r.get("chosen") for r in recs if r.get("event") == "route"},
    }


def compare(recorded: dict[str, Any], records: Iterable[dict[str, Any]], variables: dict[str, Any] | None = None) -> list[str]:
    """Return one message per difference between a recorded run and the export's records; empty means parity."""
    actual = summarize(records)
    diffs: list[str] = []
    if "steps" in recorded and recorded["steps"] != actual["steps"]:
        diffs.append(f"steps differ: recorded {recorded['steps']} vs export {actual['steps']}")
    if "tools" in recorded:
        want = [(t["node"], t["tool"], t.get("args", {})) for t in recorded["tools"]]
        got = [(t["node"], t["tool"], t["args"]) for t in actual["tools"]]
        if want != got:
            diffs.append(f"tool calls differ: recorded {want} vs export {got}")
    for node, target in (recorded.get("routes") or {}).items():
        if actual["routes"].get(node) != target:
            diffs.append(f"route at '{node}': recorded '{target}' vs export '{actual['routes'].get(node)}'")
    for name, value in (recorded.get("variables") or {}).items():
        if variables is None or variables.get(name) != value:
            diffs.append(f"variable '{name}': recorded {value!r} vs export {None if variables is None else variables.get(name)!r}")
    return diffs
