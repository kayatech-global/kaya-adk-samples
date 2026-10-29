"""router: how a supervisor chooses the next node. The core owns the loop, turn limit and allowed set."""

from __future__ import annotations

import json
import re
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..datatypes import RouteChoice
from .base import Provider, ProviderSettings
from .registry import register

FINISH_WORDS = {"FINISH", "END", "__END__", "__end__"}


@dataclass
class RouterInput:
    """What a routing strategy sees when it chooses the next node."""

    node_id: str
    allowed: Sequence[str]
    turn: int
    max_turns: int
    variables: Mapping[str, Any]
    model_output: Any = None  # parsed structured output when the strategy asked for a model
    settings: Mapping[str, Any] = field(default_factory=dict)


class RouterStrategy(Provider):
    """Factory kind that picks the next node; ``requires_model`` says whether the supervisor model is called first."""

    kind = "router"
    requires_model = False

    def output_schema(self, allowed: Sequence[str]) -> dict[str, Any] | None:
        """JSON schema the supervisor model must answer with, or None when no model call is needed."""
        return None

    @abstractmethod
    async def choose(self, inp: RouterInput) -> RouteChoice:
        """Pick the next node.

        Returns:
            A node from ``inp.allowed``, or ``next_node=None`` to finish. The core enforces the allowed set and turn limit, so a strategy may return the ``__invalid__`` sentinel when a model picks a disallowed node.
        """


def _lenient_json(value: Any) -> dict[str, Any]:
    """A dict from a model answer: plain JSON, JSON inside a code fence or prose, or {} when there is none."""
    if isinstance(value, dict):
        return value
    text = str(value or "")
    for candidate in (text, *re.findall(r"\{.*\}", text, re.S)):
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return {}


@register
class LlmStructuredRouter(RouterStrategy):
    """The model answers {next, message}; next is one of the allowed nodes or FINISH."""

    name = "llm_structured"
    requires_model = True

    def output_schema(self, allowed: Sequence[str]) -> dict[str, Any]:
        """Enum of the allowed nodes plus FINISH, and a free-text message."""
        return {"next": {"type": "enum", "values": [*allowed, "FINISH"]}, "message": {"type": "string"}}

    async def choose(self, inp: RouterInput) -> RouteChoice:
        """Read the model's structured answer: FINISH words end the run and a disallowed node yields the ``__invalid__`` sentinel."""
        out = _lenient_json(inp.model_output)
        nxt = str((out or {}).get("next", "")).strip()
        msg = str((out or {}).get("message", ""))
        if nxt.upper() in {w.upper() for w in FINISH_WORDS}:
            return RouteChoice(None, msg or "finish")
        if nxt in inp.allowed:
            return RouteChoice(nxt, msg)
        return RouteChoice("__invalid__", f"model chose '{nxt}', which is not in {list(inp.allowed)}")


@register
class RulesRouter(RouterStrategy):
    """First rule whose condition is true wins: rules: [{when: "amount > 100", next: node}], default: node|FINISH."""

    name = "rules"

    class Settings(ProviderSettings):
        default: str = "FINISH"

    async def choose(self, inp: RouterInput) -> RouteChoice:
        """Return the first rule whose ``when`` is true, else the default; FINISH words map to ``None``."""
        from ..engine.evaluator import evaluate_condition

        rules = list(inp.settings.get("rules") or [])
        for rule in rules:
            res = evaluate_condition(str(rule.get("when", "false")), dict(inp.variables), mode="strict")
            if res.value:
                target = str(rule["next"])
                return RouteChoice(None if target.upper() in FINISH_WORDS else target, f"rule: {rule.get('when')}")
        target = str(inp.settings.get("default", self.settings.default))
        return RouteChoice(None if target.upper() in FINISH_WORDS else target, "default")

