"""Edge-condition evaluator: a syntax-tree walker, never eval(). Two modes.

``compat`` reproduces the source platform's expression quirks (word rewrite inside strings, no ``not``/``in``/``is``, errors mean false).
``strict`` (default) accepts ``not`` and ``in``, rewrites words outside strings only and reports errors.
"""

from __future__ import annotations

import ast
import io
import operator
import re
import tokenize
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ...errors import ConditionError

MAX_LENGTH = 2000
MAX_NODES = 400
CALLS: dict[str, Callable[..., Any]] = {
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
}
STR_METHODS = {"lower", "upper", "strip", "startswith", "endswith", "lstrip", "rstrip", "title"}
_WORDS = re.compile(r"\b(AND|OR|true|false|null)\b", re.I)
_WORD_MAP = {"and": "and", "or": "or", "true": "True", "false": "False", "null": "None"}
_CMP: dict[type, Callable[[Any, Any], Any]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}
_BIN: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_ORDERING = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)


@dataclass
class Scope:
    """Lookup order: workflow variables first, then custom state."""

    variables: Mapping[str, Any] = field(default_factory=dict)
    custom_state: Mapping[str, Any] = field(default_factory=dict)

    def lookup(self, name: str) -> tuple[bool, Any]:
        """Resolve a (possibly dotted) name to a (found, value) pair.

        A dotted name is tried whole, then without its prefix against custom state, then walked through nested mappings.
        """
        if "." not in name:
            if name in self.variables:
                return True, self.variables[name]
            if name in self.custom_state:
                return True, self.custom_state[name]
            return False, None
        if name in self.custom_state:
            return True, self.custom_state[name]
        if name in self.variables:
            return True, self.variables[name]
        tail = name.split(".", 1)[1]
        if tail in self.custom_state:
            return True, self.custom_state[tail]
        head, *rest = name.split(".")
        for src in (self.variables, self.custom_state):
            if head in src:
                node: Any = src[head]
                for part in rest:
                    if isinstance(node, Mapping) and part in node:
                        node = node[part]
                    else:
                        break
                else:
                    return True, node
        return False, None


@dataclass
class EvalResult:
    """Outcome of evaluating an expression: the value, plus an error or missing names when evaluation failed."""
    value: Any
    error: str | None = None
    missing: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when evaluation produced no error."""
        return self.error is None


def _strip_strings(text: str, fn: Callable[[str], str]) -> str:
    """Apply fn to the parts of text outside string literals."""
    out: list[str] = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return fn(text)
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for ln in lines:
        offsets.append(offsets[-1] + len(ln))

    def pos(rc: tuple[int, int]) -> int:
        return offsets[rc[0] - 1] + rc[1]

    last = 0
    for tok in toks:
        if tok.type == tokenize.STRING:
            s, e = pos(tok.start), pos(tok.end)
            out.append(fn(text[last:s]))
            out.append(text[s:e])
            last = e
    out.append(fn(text[last:]))
    return "".join(out)


def _rewrite(expr: str, mode: str) -> str:
    def sub(seg: str) -> str:
        return _WORDS.sub(lambda m: _WORD_MAP[m.group(1).lower()], seg)

    if mode == "compat":
        return _WORDS.sub(lambda m: _WORD_MAP[m.group(1).lower()], expr)  # also inside quoted strings, as the source platform does
    return _strip_strings(expr, sub)


def _coerce(v: Any) -> Any:
    if isinstance(v, str):
        s = v.strip()
        try:
            return int(s)
        except ValueError:
            try:
                return float(s)
            except ValueError:
                return v
    return v


class _Walker:
    def __init__(self, scope: Scope, mode: str, coerce_names: bool):
        self.scope, self.mode, self.coerce_names = scope, mode, coerce_names
        self.missing: list[str] = []
        self.count = 0

    def visit(self, node: ast.AST) -> Any:
        self.count += 1
        if self.count > MAX_NODES:
            raise ConditionError("expression is too complex")
        meth = getattr(self, f"v_{type(node).__name__}", None)
        if meth is None:
            raise ConditionError(f"'{type(node).__name__}' is not allowed in a condition")
        return meth(node)

    def v_Expression(self, n: ast.Expression) -> Any:
        return self.visit(n.body)

    def v_Constant(self, n: ast.Constant) -> Any:
        return n.value

    def _name_value(self, name: str) -> Any:
        found, value = self.scope.lookup(name)
        if not found:
            self.missing.append(name)
            return None
        return _coerce(value) if self.coerce_names else value

    def v_Name(self, n: ast.Name) -> Any:
        if n.id in ("True", "False", "None"):
            return {"True": True, "False": False, "None": None}[n.id]
        return self._name_value(n.id)

    def _dotted(self, n: ast.AST) -> str | None:
        parts: list[str] = []
        while isinstance(n, ast.Attribute):
            parts.append(n.attr)
            n = n.value
        if isinstance(n, ast.Name):
            parts.append(n.id)
            return ".".join(reversed(parts))
        return None

    def v_Attribute(self, n: ast.Attribute) -> Any:
        if n.attr.startswith("_"):
            raise ConditionError(f"attribute '{n.attr}' is not allowed")
        dotted = self._dotted(n)
        if dotted is None:
            raise ConditionError("attribute access is only allowed on variable names")
        return self._name_value(dotted)

    def v_BoolOp(self, n: ast.BoolOp) -> Any:
        result: Any = None
        for v in n.values:
            result = self.visit(v)
            if isinstance(n.op, ast.And) and not result:
                return result
            if isinstance(n.op, ast.Or) and result:
                return result
        return result

    def v_UnaryOp(self, n: ast.UnaryOp) -> Any:
        if isinstance(n.op, ast.Not):
            if self.mode == "compat":
                raise ConditionError("'not' is not supported in conditions")
            return not self.visit(n.operand)
        v = self.visit(n.operand)
        if isinstance(n.op, ast.USub):
            return -v
        if isinstance(n.op, ast.UAdd):
            return +v
        raise ConditionError("unsupported unary operator")

    def v_BinOp(self, n: ast.BinOp) -> Any:
        op = _BIN.get(type(n.op))
        if op is None:
            raise ConditionError(f"operator '{type(n.op).__name__}' is not allowed")
        return op(self.visit(n.left), self.visit(n.right))

    def v_Compare(self, n: ast.Compare) -> Any:
        left = self.visit(n.left)
        for op, right_node in zip(n.ops, n.comparators, strict=True):
            right = self.visit(right_node)
            if type(op) in _CMP:
                if self.mode == "strict" and isinstance(left, str) != isinstance(right, str) and None not in (left, right):
                    left, right = _coerce(left), _coerce(right)
                try:
                    ok = _CMP[type(op)](left, right)
                except TypeError:
                    ok = False
            elif isinstance(op, (ast.In, ast.NotIn, ast.Is, ast.IsNot)):
                if self.mode == "compat":
                    raise ConditionError("'in' and 'is' are not supported in conditions")
                if isinstance(op, ast.In):
                    ok = left in right
                elif isinstance(op, ast.NotIn):
                    ok = left not in right
                elif isinstance(op, ast.Is):
                    ok = left is right
                else:
                    ok = left is not right
            else:
                raise ConditionError("unsupported comparison")
            if not ok:
                return False
            left = right
        return True

    def v_IfExp(self, n: ast.IfExp) -> Any:
        return self.visit(n.body) if self.visit(n.test) else self.visit(n.orelse)

    def v_Subscript(self, n: ast.Subscript) -> Any:
        base, key = self.visit(n.value), self.visit(n.slice)
        try:
            return base[key]
        except (KeyError, IndexError, TypeError):
            return None

    def v_List(self, n: ast.List) -> Any:
        return [self.visit(e) for e in n.elts]

    def v_Tuple(self, n: ast.Tuple) -> Any:
        return tuple(self.visit(e) for e in n.elts)

    def v_Dict(self, n: ast.Dict) -> Any:
        return {self.visit(k): self.visit(v) for k, v in zip(n.keys, n.values, strict=True) if k is not None}

    def v_Call(self, n: ast.Call) -> Any:
        args = [self.visit(a) for a in n.args]
        if n.keywords:
            raise ConditionError("keyword arguments are not allowed")
        if isinstance(n.func, ast.Name):
            fn = CALLS.get(n.func.id)
            if fn is None:
                raise ConditionError(f"function '{n.func.id}' is not allowed")
            return fn(*args)
        if isinstance(n.func, ast.Attribute) and n.func.attr in STR_METHODS:
            target = self.visit(n.func.value)
            if isinstance(target, str):
                return getattr(target, n.func.attr)(*args)
        raise ConditionError("only allow-listed function and string-method calls are permitted")


def evaluate_expression(expr: str, scope: Scope | Mapping[str, Any], mode: str = "strict") -> EvalResult:
    """Evaluate to a value. Never raises for bad input: errors come back in the result (and value is None)."""
    sc = scope if isinstance(scope, Scope) else Scope(variables=scope)
    text = (expr or "").strip()
    if not text:
        return EvalResult(None, "empty expression")
    if len(text) > MAX_LENGTH:
        return EvalResult(None, "expression is too long")
    if mode == "strict" and ("&&" in text or "||" in text):
        return EvalResult(None, "use 'and' / 'or' instead of '&&' / '||'")
    try:
        tree = ast.parse(_rewrite(text, mode), mode="eval")
    except SyntaxError as exc:
        return EvalResult(None, f"syntax error: {exc.msg}")
    coerce = any(isinstance(n, ast.Compare) and any(isinstance(o, _ORDERING) for o in n.ops) for n in ast.walk(tree))
    walker = _Walker(sc, mode, coerce)
    try:
        value = walker.visit(tree)
    except ConditionError as exc:
        return EvalResult(None, str(exc), walker.missing)
    except Exception as exc:  # arithmetic on None, division by zero, ...
        return EvalResult(None, f"{type(exc).__name__}: {exc}", walker.missing)
    return EvalResult(value, None, walker.missing)


def evaluate_condition(expr: str, scope: Scope | Mapping[str, Any], mode: str = "strict") -> EvalResult:
    """Boolean form used for routing: any error means False, with the error kept for the trace."""
    res = evaluate_expression(expr, scope, mode)
    if res.error is not None:
        return EvalResult(False, res.error, res.missing)
    return EvalResult(bool(res.value), None, res.missing)
