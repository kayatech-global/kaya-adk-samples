"""Import the typed content of an export package from outside the package: the service, the validator and ``describe``.

An export's content modules import the engine's facade relatively (``from .._engine.content import Prompt``). Imported under an
alias, ``<alias>._engine.content`` is this running engine's own ``content`` module, so the constants are instances of the types
the caller uses, and two versions of one export can be loaded in one process side by side.

Content is code, so it is checked before it runs: every module may only import the facade, the ``typing`` names ``Final`` and
``Literal`` and its sibling content modules, and may only assign literals, names and calls of the facade's constructors, never
to a name the checks rely on. No function, class, loop, comprehension or other call can run at import.
Only the source text that was checked is executed: the loader never uses the normal import system for content (no bytecode
cache, no extension module), refuses any other file in the folder, and runs each module without builtins except the type names
annotations use, with an ``__import__`` that resolves only the facade, ``typing`` and modules of the same content package.
"""

from __future__ import annotations
import __future__

import ast
import importlib.machinery
import importlib.util
import re
import sys
import typing
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType

from .. import content as facade
from ..content import ExportContent
from ..errors import ExportIntegrityError

CONTENT_PACKAGE = "content"
_TYPING = {"Final", "Literal"}
_CALLABLE = set(facade.__all__) - {"digest", "START", "END"}
_TYPE_NAMES = {"dict": dict, "list": list, "tuple": tuple, "set": set, "frozenset": frozenset, "str": str, "int": int, "float": float, "bool": bool}
_CONSTANT = re.compile(r"[A-Z][A-Z0-9_]*")
_RESERVED = set(facade.__all__) | _TYPING | set(_TYPE_NAMES) | {"__builtins__", "__import__", "__name__", "__package__", "__spec__"}


def check_content_source(source: str, filename: str = "content/__init__.py") -> list[str]:
    """The reasons ``source`` is not a plain content module, empty when it is one.

    ``filename`` is the module's path from the package folder (``content/children/kid/prompts.py``): a relative import must
    stay inside ``content``, except the one import of the engine's facade.
    """
    depth = len(Path(filename).parent.parts)  # packages from the module's own up to and including content
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        return [f"{filename}:{exc.lineno}: syntax error: {exc.msg}"]
    problems: list[str] = []

    def bad(node: ast.AST, what: str) -> None:
        problems.append(f"{filename}:{getattr(node, 'lineno', 0)}: {what} is not allowed in content")

    siblings = {  # module names imported from the same content package: the only roots of an attribute
        (a.asname or a.name)
        for stmt in tree.body
        if isinstance(stmt, ast.ImportFrom) and 0 < stmt.level <= depth and not stmt.module
        for a in stmt.names
    }

    def reference(node: ast.Attribute) -> bool:
        # the two shapes the generator writes: module.CONSTANT and module.CONSTANT.id
        inner = node.value if node.attr == "id" else node
        return (
            isinstance(inner, ast.Attribute)
            and _CONSTANT.fullmatch(inner.attr) is not None
            and isinstance(inner.value, ast.Name)
            and inner.value.id in siblings
        )

    def annotation(node: ast.AST) -> None:
        if isinstance(node, ast.Name) and (node.id in _TYPING or node.id in _TYPE_NAMES or node.id in _CALLABLE):
            return
        if isinstance(node, ast.Constant) and (node.value is Ellipsis or isinstance(node.value, str)):
            return
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id in _TYPING | set(_TYPE_NAMES):
            for e in node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]:
                annotation(e)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            annotation(node.left)
            annotation(node.right)
        else:
            bad(node, f"the annotation {ast.unparse(node)}")

    def value(node: ast.AST) -> None:
        if isinstance(node, ast.Constant):
            return
        if isinstance(node, ast.Name):
            if node.id.startswith("_"):
                bad(node, f"the name {node.id}")
            return
        if isinstance(node, ast.Attribute):
            if not reference(node):
                bad(node, f"the attribute access {ast.unparse(node)}")
        elif isinstance(node, ast.Subscript):  # only an id type: Literal["a", "b"]
            ok = isinstance(node.value, ast.Name) and node.value.id == "Literal"
            elts = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            if not ok or not all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in elts):
                bad(node, f"the subscript {ast.unparse(node)}")
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
            value(node.operand)
        elif isinstance(node, ast.List | ast.Tuple | ast.Set):
            for e in node.elts:
                value(e)
        elif isinstance(node, ast.Dict):
            for k in node.keys:
                if k is None:
                    bad(node, "dict unpacking")
                else:
                    value(k)
            for v in node.values:
                value(v)
        elif isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                allowed = target.id in _CALLABLE
            else:  # Cls.model_validate({...}) for a config whose data carries keys the class does not declare
                allowed = (
                    isinstance(target, ast.Attribute)
                    and target.attr == "model_validate"
                    and isinstance(target.value, ast.Name)
                    and target.value.id in _CALLABLE
                )
            if not allowed:
                bad(node, f"calling {ast.unparse(target)}")
            for a in node.args:
                if isinstance(a, ast.Starred):
                    bad(a, "argument unpacking")
                else:
                    value(a)
            for kw in node.keywords:
                if kw.arg is None:
                    bad(kw.value, "keyword unpacking")
                value(kw.value)
        else:
            bad(node, type(node).__name__)

    for i, stmt in enumerate(tree.body):
        if i == 0 and isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
            continue  # the module docstring
        if isinstance(stmt, ast.ImportFrom):
            mod = stmt.module or ""
            if mod == "__future__" or (stmt.level == 0 and mod == "typing" and {a.name for a in stmt.names} <= _TYPING):
                continue
            if any(a.asname in _RESERVED or (a.asname is None and a.name in _RESERVED and mod != "_engine.content") for a in stmt.names):
                bad(stmt, "importing under a reserved name")
            elif stmt.level == depth + 1 and mod == "_engine.content":
                continue  # the facade
            if 0 < stmt.level <= depth and "_engine" not in mod.split("."):
                continue  # a module inside content
            bad(stmt, f"importing {'.' * stmt.level}{mod}")
        elif isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if not isinstance(t, ast.Name):
                    bad(t, "assigning to anything but a name")
                elif t.id in _RESERVED:
                    bad(t, f"assigning to {t.id}")
            value(stmt.value)
        elif isinstance(stmt, ast.AnnAssign):
            if not isinstance(stmt.target, ast.Name):
                bad(stmt.target, "assigning to anything but a name")
            elif stmt.target.id in _RESERVED:
                bad(stmt.target, f"assigning to {stmt.target.id}")
            annotation(stmt.annotation)
            if stmt.value is not None:
                value(stmt.value)
        else:
            bad(stmt, type(stmt).__name__)
    return problems


def _sources(content_dir: Path) -> tuple[dict[str, str], list[str]]:
    """Every module source under ``content_dir`` by path relative to its parent, and the files that do not belong there."""
    sources: dict[str, str] = {}
    strays: list[str] = []
    for path in sorted(content_dir.rglob("*")):
        rel = path.relative_to(content_dir.parent)
        if "__pycache__" in rel.parts:
            continue  # never read: the loader compiles the checked source itself
        if path.is_dir():
            continue
        if path.suffix != ".py" or path.is_symlink():
            strays.append(f"{rel.as_posix()}: only Python source files may be in content")
            continue
        sources[rel.as_posix()] = path.read_text(encoding="utf-8")
    return sources, strays


def check_content_package(content_dir: Path) -> list[str]:
    """Check every file under an export's ``content`` folder."""
    sources, problems = _sources(content_dir)
    for rel, text in sources.items():
        problems.extend(check_content_source(text, rel))
    return problems


def import_content(pkg_dir: Path | str, alias: str) -> ExportContent:
    """Run the checked modules of ``<pkg_dir>/content`` as ``<alias>.content`` and return its ``CONTENT``.

    Raises:
        ExportIntegrityError: The folder has no content package, a file is not plain content, or ``CONTENT`` is missing.
    """
    pkg_dir = Path(pkg_dir).resolve()
    content_dir = pkg_dir / CONTENT_PACKAGE
    if not (content_dir / "__init__.py").is_file():
        raise ExportIntegrityError(f"{content_dir} is not a content package")
    if not alias.isidentifier():
        raise ValueError(f"alias '{alias}' is not a Python identifier")
    sources, problems = _sources(content_dir)
    for rel, text in sources.items():
        problems.extend(check_content_source(text, rel))
    if problems:
        raise ExportIntegrityError("content refused: " + "; ".join(problems[:10]))
    unload(alias)
    root = ModuleType(alias)
    root.__path__ = []
    engine = ModuleType(f"{alias}._engine")
    engine.__path__ = []
    sys.modules[alias], sys.modules[f"{alias}._engine"], sys.modules[f"{alias}._engine.content"] = root, engine, facade
    try:
        module = _ContentImporter(alias, pkg_dir, sources).load(f"{alias}.{CONTENT_PACKAGE}")
    except Exception as exc:
        unload(alias)
        raise ExportIntegrityError(f"cannot import the content of {pkg_dir.name}: {type(exc).__name__}: {exc}") from exc
    found = getattr(module, "CONTENT", None)
    if not isinstance(found, ExportContent):
        unload(alias)
        raise ExportIntegrityError(f"{content_dir}/__init__.py defines no CONTENT")
    return found


class _ContentImporter:
    """Executes the checked source texts of one content package, and resolves their imports, without the import system."""

    def __init__(self, alias: str, pkg_dir: Path, sources: dict[str, str]):
        self.alias, self.pkg_dir, self.sources = alias, pkg_dir, sources

    def _rel(self, modname: str) -> tuple[str, bool] | None:
        parts = modname.split(".")[1:]  # without the alias
        package = "/".join(parts) + "/__init__.py"
        if package in self.sources:
            return package, True
        module = "/".join(parts) + ".py"
        return (module, False) if module in self.sources else None

    def load(self, modname: str) -> ModuleType:
        if modname in sys.modules:
            return sys.modules[modname]
        found = self._rel(modname)
        if found is None:
            raise ImportError(f"no content module {modname}")
        rel, is_package = found
        parent = modname.rpartition(".")[0]
        if parent != f"{self.alias}.{CONTENT_PACKAGE}".rpartition(".")[0] and parent not in sys.modules:
            self.load(parent)  # a package runs before its modules, as with a normal import
        module = ModuleType(modname)
        module.__file__ = str(self.pkg_dir / rel)
        module.__package__ = modname if is_package else modname.rpartition(".")[0]
        module.__spec__ = importlib.machinery.ModuleSpec(modname, None, origin=module.__file__, is_package=is_package)
        if is_package:
            module.__path__ = []
        module.__dict__["__builtins__"] = {**_TYPE_NAMES, "__import__": self._import}
        sys.modules[modname] = module
        last = modname.rpartition(".")[2]
        exec(compile(self.sources[rel], module.__file__, "exec", dont_inherit=True), module.__dict__)  # noqa: S102  (checked above)
        if parent in sys.modules:
            setattr(sys.modules[parent], last, module)
        return module

    def _import(
        self,
        name: str,
        globals: Mapping[str, object] | None = None,
        locals: Mapping[str, object] | None = None,
        fromlist: Sequence[str] | None = (),
        level: int = 0,
    ) -> ModuleType:
        if level == 0 and name in ("typing", "__future__"):
            return typing if name == "typing" else __future__
        package = str((globals or {}).get("__package__") or "")
        target = importlib.util.resolve_name("." * level + name, package) if level else name
        if target == f"{self.alias}._engine.content":
            return facade
        if target != f"{self.alias}.{CONTENT_PACKAGE}" and not target.startswith(f"{self.alias}.{CONTENT_PACKAGE}."):
            raise ImportError(f"content may not import {target}")
        module = self.load(target)
        for item in fromlist or ():
            if not hasattr(module, item):
                self.load(f"{target}.{item}")
        return module


def unload(alias: str) -> None:
    """Forget every module imported under ``alias``."""
    for name in [m for m in sys.modules if m == alias or m.startswith(alias + ".")]:
        del sys.modules[name]
