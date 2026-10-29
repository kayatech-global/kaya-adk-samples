"""Plan branch conditions: a Python snippet that assigns ``next``. Checked against an allow-list, then wrapped in a function.

The same checks run in the exporter (when it writes conditions/<node>.py) and in the core (when it loads the file),
so a tampered file is refused. Besides plain statements a snippet may use comprehensions and generator expressions, and
define small helper functions at its top level; a helper may call only safe built-ins and helpers defined before it.
It may also ``import re`` and call the pattern functions of ``SAFE_MODULES`` on it; the module it gets is a stand-in that
holds only those functions.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import textwrap
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...errors import ConditionError

IMPLICIT = {"result", "is_error", "workflow_variables"}
SAFE_CALLS = {"len", "str", "int", "float", "bool", "abs", "min", "max", "round", "sum", "any", "all", "sorted", "list", "dict", "set"}
SAFE_METHODS = {
    "lower",
    "upper",
    "strip",
    "lstrip",
    "rstrip",
    "startswith",
    "endswith",
    "get",
    "items",
    "keys",
    "values",
    "title",
    "split",
    "join",
    "count",
    "isdigit",
}
# Modules a snippet may import and the only functions it may call on them (``re.sub(...)``); nothing else is reachable.
SAFE_MODULES: dict[str, frozenset[str]] = {"re": frozenset({"sub", "match", "search", "fullmatch", "findall", "split", "escape"})}
# Introspection names that lead from a value to frames, code or globals; refused even if a safe method ever shared a name.
_FORBIDDEN_ATTRS = {"gi_frame", "gi_code", "gi_yieldfrom", "ag_frame", "ag_code", "cr_frame", "cr_code", "f_back", "f_globals"}
_FORBIDDEN_ATTRS |= {"f_locals", "f_builtins", "f_code", "tb_frame", "tb_next", "format", "format_map", "mro"}
_ALLOWED_STMT = (ast.Assign, ast.AugAssign, ast.If, ast.Pass)
_COMPREHENSIONS = (ast.GeneratorExp, ast.ListComp, ast.SetComp, ast.DictComp)
_ALLOWED_EXPR = (
    ast.Name,
    ast.Constant,
    ast.Compare,
    ast.BoolOp,
    ast.UnaryOp,
    ast.BinOp,
    ast.Subscript,
    ast.Call,
    ast.Attribute,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Set,
    ast.IfExp,
    ast.JoinedStr,
    ast.FormattedValue,
    ast.Slice,
    *_COMPREHENSIONS,
)
_OPS = (
    ast.And,
    ast.Or,
    ast.Not,
    ast.USub,
    ast.UAdd,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
    ast.Load,
    ast.Store,
)


class SnippetError(ConditionError):
    """Raised when a condition snippet or module fails the allow-list check; carries every error found."""
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class SnippetReport:
    """Static-analysis result for a condition snippet: free names, whether it assigns ``next``, and errors."""
    free_names: list[str] = field(default_factory=list)
    assigns_next: bool = False
    always_assigns_next: bool = False
    errors: list[str] = field(default_factory=list)


def _always_assigns(body: list[ast.stmt]) -> bool:
    for stmt in body:
        if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "next" for t in stmt.targets):
            return True
        if isinstance(stmt, ast.If) and stmt.orelse and _always_assigns(stmt.body) and _always_assigns(stmt.orelse):
            return True
    return False


def analyse_snippet(code: str) -> SnippetReport:
    """Statically check a condition snippet against the allow-list without running it.

    Syntax errors are reported in the result, not raised.
    """
    rep = SnippetReport()
    try:
        tree = ast.parse(textwrap.dedent(code))
    except SyntaxError as exc:
        rep.errors.append(f"syntax error: {exc.msg} (line {exc.lineno})")
        return rep
    assigned: set[str] = set()
    free: set[str] = set()
    helpers: set[str] = set()  # helper functions defined so far, callable from here on
    modules: set[str] = set()  # SAFE_MODULES imported so far

    def check_expr(node: ast.AST, local: frozenset[str] = frozenset()) -> None:
        methods = _method_calls(node, modules)
        bound = local | {
            n.id for c in ast.walk(node) if isinstance(c, ast.comprehension) for n in ast.walk(c.target) if isinstance(n, ast.Name)
        }
        for sub in ast.walk(node):
            if isinstance(sub, ast.comprehension):
                if sub.is_async:
                    rep.errors.append("async comprehensions are not allowed")
                if not all(isinstance(n, (ast.Name, ast.Tuple, ast.Store)) for n in ast.walk(sub.target)):
                    rep.errors.append("comprehension targets must be simple names")
                continue
            if isinstance(sub, _OPS):
                continue
            if not isinstance(sub, _ALLOWED_EXPR) and not isinstance(sub, ast.keyword):
                rep.errors.append(f"'{type(sub).__name__}' is not allowed")
            if isinstance(sub, ast.Attribute):
                rep.errors.extend(_attribute_errors(sub, methods))
            if isinstance(sub, ast.Call):
                f = sub.func
                if isinstance(f, ast.Name):
                    if f.id not in SAFE_CALLS and f.id not in helpers:
                        rep.errors.append(f"call to '{f.id}' is not allowed")
                elif isinstance(f, ast.Attribute):
                    if _is_module_call(f, modules):
                        pass
                    elif isinstance(f.value, ast.Name) and f.value.id in modules:
                        rep.errors.append(f"'{f.value.id}.{f.attr}' is not allowed")
                    elif f.attr not in SAFE_METHODS:
                        rep.errors.append(f"method '{f.attr}' is not allowed")
                else:
                    rep.errors.append("this kind of call is not allowed")
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                if sub.id.startswith("__"):
                    rep.errors.append(f"name '{sub.id}' is not allowed")
                elif (
                    sub.id not in SAFE_CALLS
                    and sub.id not in IMPLICIT
                    and sub.id not in assigned
                    and sub.id not in bound
                    and sub.id not in helpers
                    and sub.id not in modules
                    and sub.id not in {"True", "False", "None"}
                ):
                    free.add(sub.id)

    def walk(body: list[ast.stmt], local: set[str] | None = None) -> None:
        """Check statements; ``local`` is the set of names bound inside a helper function, None at the top level."""
        for stmt in body:
            if isinstance(stmt, ast.FunctionDef) and local is None:
                define(stmt)
                continue
            if isinstance(stmt, ast.Import) and local is None:
                for alias in stmt.names:
                    if alias.name not in SAFE_MODULES or alias.asname:
                        rep.errors.append(f"import of '{alias.name}' is not allowed")
                    else:
                        modules.add(alias.name)
                continue
            if isinstance(stmt, ast.Return) and local is not None:
                if stmt.value is not None:
                    check_expr(stmt.value, frozenset(local))
                continue
            if not isinstance(stmt, _ALLOWED_STMT):
                rep.errors.append(f"statement '{type(stmt).__name__}' is not allowed")
                continue
            scope = frozenset(local) if local is not None else frozenset()
            if isinstance(stmt, ast.Assign):
                check_expr(stmt.value, scope)
                for t in stmt.targets:
                    if isinstance(t, ast.Name) and t.id in SAFE_MODULES:
                        rep.errors.append(f"'{t.id}' cannot be assigned")
                    elif isinstance(t, ast.Name) and not t.id.startswith("__"):
                        if local is not None:
                            local.add(t.id)
                            continue
                        assigned.add(t.id)
                        if t.id == "next":
                            rep.assigns_next = True
                    else:
                        rep.errors.append("only simple names can be assigned")
            elif isinstance(stmt, ast.AugAssign):
                check_expr(stmt.value, scope)
                if isinstance(stmt.target, ast.Name) and stmt.target.id not in SAFE_MODULES:
                    (local if local is not None else assigned).add(stmt.target.id)
                else:
                    rep.errors.append("only simple names can be assigned")
            elif isinstance(stmt, ast.If):
                check_expr(stmt.test, scope)
                walk(stmt.body, local)
                walk(stmt.orelse, local)

    def define(fn: ast.FunctionDef) -> None:
        """A top-level helper: plain parameters, no decorators, a body of allowed statements and returns."""
        a = fn.args
        if fn.decorator_list or a.vararg or a.kwarg or fn.name.startswith("_") or fn.name in IMPLICIT | SAFE_CALLS | {"next", "route"}:
            rep.errors.append(f"helper '{fn.name}' must be a plain function with a plain name and no decorators or *args")
        for default in [*a.defaults, *(d for d in a.kw_defaults if d is not None)]:
            check_expr(default)
        params = {p.arg for p in [*a.posonlyargs, *a.args, *a.kwonlyargs]}
        if params & SAFE_MODULES.keys():
            rep.errors.append(f"helper '{fn.name}' cannot take a parameter named like a module")
        walk(fn.body, params)  # the helper is not callable inside its own body: no recursion
        helpers.add(fn.name)
        assigned.add(fn.name)

    walk(tree.body)
    rep.free_names = sorted(free)
    rep.always_assigns_next = _always_assigns(tree.body)
    if not rep.assigns_next:
        rep.errors.append("the snippet never assigns 'next'")
    return rep


def render_condition_module(node_id: str, snippet: str, labels: list[str]) -> str:
    """Source of conditions/<node>.py. Deterministic: depends only on its arguments."""
    rep = analyse_snippet(snippet)
    if rep.errors:
        raise SnippetError(rep.errors)
    code = textwrap.dedent(snippet).strip("\n")
    imports = {
        n
        for stmt in ast.parse(code).body
        if isinstance(stmt, ast.Import)
        for n in range(stmt.lineno, (stmt.end_lineno or stmt.lineno) + 1)
    }
    code = "\n".join(line for i, line in enumerate(code.split("\n"), 1) if i not in imports).strip("\n")
    body = textwrap.indent(code, "    ")
    loads = "".join(f"    {n} = workflow_variables.get({n!r})\n" for n in rep.free_names)
    return (
        f'"""Generated branch condition for plan node {node_id!r}. Do not edit; change the plan and re-export."""\n\n'
        f"LABELS = {sorted(labels)!r}\n\n\n"
        "def route(result, is_error, workflow_variables):\n"
        '    """Return the label of the edge to take, or None when no branch matches."""\n'
        f"{loads}    next = None\n{body}\n    return next\n"
    )


def check_module_source(source: str) -> list[str]:
    """Forbidden-construct scan of a generated condition module (run again at load time)."""
    errors: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"syntax error: {exc.msg}"]
    helpers = _helper_order(tree)
    methods = _method_calls(tree, set(SAFE_MODULES))
    for node in ast.walk(tree):
        if isinstance(
            node,
            (
                ast.Import,
                ast.ImportFrom,
                ast.Global,
                ast.Nonlocal,
                ast.Lambda,
                ast.With,
                ast.Try,
                ast.While,
                ast.For,
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.Await,
                ast.Yield,
            ),
        ):
            errors.append(f"'{type(node).__name__}' is not allowed in a condition module")
        if isinstance(node, ast.Attribute):
            errors.extend(_attribute_errors(node, methods))
        if isinstance(node, ast.FunctionDef) and node.decorator_list:
            errors.append(f"decorators are not allowed on '{node.name}'")
        if isinstance(node, ast.Name) and node.id in SAFE_MODULES and not isinstance(node.ctx, ast.Load):
            errors.append(f"'{node.id}' cannot be assigned")
        if isinstance(node, ast.arg) and node.arg in SAFE_MODULES:
            errors.append(f"'{node.arg}' cannot be a parameter")
        if isinstance(node, ast.comprehension) and node.is_async:
            errors.append("async comprehensions are not allowed")
    for caller, callees in _calls_by_function(tree).items():
        allowed = helpers.get(caller, set())
        for name in callees:
            if name not in SAFE_CALLS and name not in allowed:
                errors.append(f"call to '{name}' is not allowed")
    return errors


def _is_module_call(func: ast.Attribute, modules: set[str]) -> bool:
    """``re.sub`` and the like: a function of an imported safe module, called by its plain module name."""
    return isinstance(func.value, ast.Name) and func.value.id in modules and func.attr in SAFE_MODULES.get(func.value.id, ())


def _method_calls(tree: ast.AST, modules: set[str]) -> set[int]:
    """The ids of attribute nodes that are the callee of a call to a safe method or safe module function, the only attribute
    access allowed."""
    calls = [n.func for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    return {
        id(f)
        for f in calls
        if (_is_module_call(f, modules) if isinstance(f.value, ast.Name) and f.value.id in modules else f.attr in SAFE_METHODS)
    }


def _attribute_errors(node: ast.Attribute, methods: set[int]) -> list[str]:
    """An attribute is allowed only as a safe method call (``x.strip()``), never read on its own (``gen.gi_frame``)."""
    if node.attr.startswith("_") or node.attr in _FORBIDDEN_ATTRS:
        return [f"attribute '{node.attr}' is not allowed"]
    if id(node) not in methods:
        return [f"attribute '{node.attr}' is not allowed (only calls to safe methods)"]
    return []


def _helper_order(tree: ast.Module) -> dict[str, set[str]]:
    """For each function, the helpers it may call: those defined before it and those defined in its own body.

    A helper is never visible to itself, so a condition cannot recurse; ``route`` is never callable.
    """
    out: dict[str, set[str]] = {"": set()}

    def visit(body: list[ast.stmt], visible: set[str]) -> None:
        seen = set(visible)
        for stmt in body:
            if isinstance(stmt, ast.FunctionDef):
                visit(stmt.body, seen)
                out[stmt.name] = seen | {s.name for s in stmt.body if isinstance(s, ast.FunctionDef)}
                if stmt.name != "route":
                    seen.add(stmt.name)

    visit(tree.body, set())
    return out


def _calls_by_function(tree: ast.Module) -> dict[str, list[str]]:
    """Every call by plain name, grouped by the innermost function that makes it (module level: ``""``)."""
    out: dict[str, list[str]] = {}

    def visit(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef):
                visit(child, child.name)
                continue
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                out.setdefault(owner, []).append(child.func.id)
            visit(child, owner)

    visit(tree, "")
    return out


def build_route_function(source: str) -> Callable[..., Any]:
    """Compile condition module source into its ``route`` function.

    Raises:
        SnippetError: The source uses anything outside the allow-list. Checked before compiling, because the code is then exec'd with only safe builtins.
    """
    errors = check_module_source(source)
    if errors:
        raise SnippetError(errors)
    safe = {n: getattr(builtins, n) for n in SAFE_CALLS | {"True", "False", "None"} if hasattr(builtins, n)}
    namespace: dict[str, Any] = {"__builtins__": safe, **{m: _module_stand_in(m) for m in SAFE_MODULES}}
    exec(compile(source, "<condition>", "exec"), namespace)  # noqa: S102  (source passed the allow-list scan above)
    return namespace["route"]


def _module_stand_in(name: str) -> types.SimpleNamespace:
    module = importlib.import_module(name)
    return types.SimpleNamespace(**{f: getattr(module, f) for f in SAFE_MODULES[name]})


def load_route_function(path: Path) -> Callable[..., Any]:
    """Read a condition module file and compile it into its ``route`` function."""
    return build_route_function(path.read_text())
