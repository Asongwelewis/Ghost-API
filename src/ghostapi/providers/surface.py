"""Static API surface of one version of a Python distribution, built with griffe.

Nothing here imports or executes the package: griffe parses the `.py`/`.pyi` files. The surface
follows what a static type checker can see; anything it can't see into is marked `dynamic` so
the engine stays silent instead of guessing.
"""

from __future__ import annotations

import ast
import logging
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import griffe

from ghostapi.models import ApiSymbol, Param, ParamKind

FORMAT = 1  # bump when the extraction logic changes, to invalidate cached surfaces
EXTERNAL = "<external>"  # member defined outside the package: it exists, but we can't see into it

logging.getLogger("griffe").setLevel(logging.CRITICAL)

_PARAM_KINDS: dict[str, ParamKind] = {
    "positional-only": "positional_only",
    "positional or keyword": "positional_or_keyword",
    "variadic positional": "var_positional",
    "keyword-only": "keyword_only",
    "variadic keyword": "var_keyword",
}
# Decorators that leave the call signature alone. Any other decorator may rewrite it.
_SIGNATURE_SAFE_DECORATORS = {
    "staticmethod", "classmethod", "abstractmethod", "override", "final", "deprecated",
}  # fmt: skip
# Bases that add no public attributes we would otherwise miss.
_SAFE_BASES = {"object", "Generic", "Protocol", "ABC"}


@dataclass
class Node:
    kind: str  # module | class | function | attribute
    params: tuple[Param, ...] | None = None
    dynamic: bool = False
    deprecated: bool = False
    members: dict[str, str] = field(default_factory=dict)  # name -> canonical path (or EXTERNAL)


_EXTERNAL_NODE = Node("attribute", dynamic=True)


@dataclass
class Surface:
    roots: dict[str, str]  # top-level import name -> canonical path
    nodes: dict[str, Node]

    def node(self, canonical: str) -> Node:
        return self.nodes.get(canonical, _EXTERNAL_NODE)

    def walk(self, path: str) -> list[Node]:
        """Nodes for each leading segment of `path` that exists. Shorter than `path` = missing."""
        parts = path.split(".")
        root = self.roots.get(parts[0])
        if root is None:
            return []
        chain = [self.node(root)]
        for part in parts[1:]:
            target = chain[-1].members.get(part)
            if target is None:
                break
            chain.append(self.node(target))
        return chain

    def has(self, path: str) -> bool:
        return len(self.walk(path)) == path.count(".") + 1

    def public_paths(self, name: str, kind: str, max_depth: int = 8) -> list[str]:
        """Public dotted paths that end in `name` and point at a `kind` object."""
        found: list[str] = []
        seen: set[str] = set()
        queue = deque((root, canonical, 0) for root, canonical in self.roots.items())
        while queue:
            path, canonical, depth = queue.popleft()
            if canonical in seen or depth >= max_depth:
                continue
            seen.add(canonical)
            for member, target in self.node(canonical).members.items():
                if member.startswith("_") or target == EXTERNAL:
                    continue
                child = f"{path}.{member}"
                if member == name and self.node(target).kind == kind:
                    found.append(child)
                queue.append((child, target, depth + 1))
        return found

    def to_json(self) -> dict[str, Any]:
        nodes = {}
        for path, node in self.nodes.items():
            data: dict[str, Any] = {"k": node.kind}
            if node.params is not None:
                data["p"] = [[p.name, p.kind, p.required] for p in node.params]
            if node.dynamic:
                data["d"] = 1
            if node.deprecated:
                data["x"] = 1
            if node.members:
                data["m"] = node.members
            nodes[path] = data
        return {"format": FORMAT, "roots": self.roots, "nodes": nodes}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Surface | None:
        if data.get("format") != FORMAT:
            return None
        nodes = {
            path: Node(
                kind=raw["k"],
                params=(tuple(Param(n, k, r) for n, k, r in raw["p"]) if "p" in raw else None),
                dynamic=bool(raw.get("d")),
                deprecated=bool(raw.get("x")),
                members=raw.get("m", {}),
            )
            for path, raw in data["nodes"].items()
        }
        return cls(roots=data["roots"], nodes=nodes)


def to_symbol(node: Node, path: str, version: str | None) -> ApiSymbol:
    return ApiSymbol(
        path=path,
        kind=node.kind,
        params=node.params,
        dynamic=node.dynamic,
        deprecated_in=version if node.deprecated else None,
    )


def build_surface(
    source_dir: Path, top_modules: Iterable[str], compiled: Iterable[str] = ()
) -> Surface:
    """Surface of the modules under `source_dir`. `compiled` lists extension modules (no source)."""
    known = _module_names(source_dir) | set(compiled)
    roots: dict[str, str] = {}
    nodes: dict[str, Node] = {}
    for name in sorted(top_modules):
        try:
            module = griffe.load(
                name,
                search_paths=[source_dir],
                allow_inspection=False,
                resolve_aliases=True,
                resolve_external=False,
                store_source=False,
            )
        except Exception:  # griffe raises many error types on unusual layouts; skip the module
            continue
        roots[name] = module.path
        _collect(module, nodes, known)
    for dotted in compiled:
        _add_compiled(dotted, roots, nodes)
    return Surface(roots, nodes)


def _module_names(source_dir: Path) -> set[str]:
    names = set()
    for path in source_dir.rglob("*.py*"):
        if path.suffix not in (".py", ".pyi"):
            continue
        parts = list(path.relative_to(source_dir).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        if parts:
            names.add(".".join(parts))
    return names


def _add_compiled(dotted: str, roots: dict[str, str], nodes: dict[str, Node]) -> None:
    parent, _, name = dotted.rpartition(".")
    if not parent:
        roots.setdefault(name, name)
    elif parent in nodes:
        nodes[parent].members.setdefault(name, dotted)
    nodes.setdefault(dotted, Node("module", dynamic=True))


def _target(member: Any) -> Any | None:
    if not member.is_alias:
        return member
    try:
        return member.final_target
    except (griffe.AliasResolutionError, griffe.CyclicAliasError):
        return None


def _collect(root: Any, nodes: dict[str, Node], known_modules: set[str]) -> None:
    queue = [root]
    while queue:
        obj = queue.pop()
        if obj.path in nodes:
            continue
        node = _node(obj, known_modules)
        nodes[obj.path] = node
        if node.kind not in ("module", "class"):
            continue
        try:
            members = obj.all_members if obj.is_class else obj.members
        except Exception:  # inherited members can fail to resolve
            members, node.dynamic = obj.members, True
        for name, member in members.items():
            target = _target(member)
            if target is None:
                node.members[name] = EXTERNAL
                continue
            node.members[name] = target.path
            if target.path not in nodes:
                queue.append(target)


def _decorator_names(obj: Any) -> list[str]:
    return [str(d.value).split("(")[0].rsplit(".", 1)[-1] for d in obj.decorators]


def _node(obj: Any, known_modules: set[str]) -> Node:
    kind = obj.kind.value
    if kind == "module":
        return Node("module", dynamic=_module_dynamic(obj, known_modules))
    if kind == "class":
        dynamic = _class_dynamic(obj)
        params = None if dynamic or obj.decorators else _class_params(obj)
        return Node("class", params, dynamic, deprecated="deprecated" in _decorator_names(obj))
    if kind == "function":
        if "property" in obj.labels:
            return Node("attribute")
        return Node(
            "function",
            _function_params(obj),
            deprecated="deprecated" in _decorator_names(obj),
        )
    return Node("attribute")


def _function_params(fn: Any, bound: bool = False) -> tuple[Param, ...] | None:
    if getattr(fn, "overloads", None):
        return None  # several signatures; picking one would be a guess
    if any(name not in _SIGNATURE_SAFE_DECORATORS for name in _decorator_names(fn)):
        return None
    params = list(fn.parameters)
    is_method = bound or (fn.parent is not None and fn.parent.is_class)
    if (
        is_method
        and "staticmethod" not in fn.labels
        and params
        and params[0].kind.value in ("positional-only", "positional or keyword")
    ):
        params = params[1:]  # self / cls
    return tuple(
        Param(
            p.name,
            _PARAM_KINDS[p.kind.value],
            required=p.default is None and not p.kind.value.startswith("variadic"),
        )
        for p in params
    )


def _class_params(cls: Any) -> tuple[Param, ...] | None:
    members = cls.all_members
    if "__new__" in members:
        return None
    init = _target(members["__init__"]) if "__init__" in members else None
    if init is None or init.kind.value != "function":
        return None
    return _function_params(init, bound=True)


def _class_dynamic(cls: Any, depth: int = 0) -> bool:
    if depth > 20:
        return True
    try:
        members = cls.all_members
        resolved = list(cls.resolved_bases)
    except Exception:
        return True
    if "__getattr__" in members or "__getattribute__" in members:
        return True
    unresolved = [
        b for b in cls.bases if str(b).split("[")[0].rsplit(".", 1)[-1] not in _SAFE_BASES
    ]
    if len(resolved) < len(unresolved):
        return True  # inherits from something outside the package
    return any(_class_dynamic(base, depth + 1) for base in resolved)


def _module_files(module: Any) -> list[Path]:
    paths = module.filepath if isinstance(module.filepath, list) else [module.filepath]
    files = []
    for path in paths:
        if path is None:
            continue
        files.append(path)
        sibling = path.with_suffix(".py" if path.suffix == ".pyi" else ".pyi")
        if sibling.exists():
            files.append(sibling)
    return [f for f in files if f.is_file()]


def _module_dynamic(module: Any, known_modules: set[str]) -> bool:
    """True when the module can grow attributes at runtime in ways we can't see statically."""
    for path in _module_files(module):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            return True
        is_package = path.stem == "__init__"
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "__getattr__":
                if node in tree.body:
                    return True
            elif isinstance(node, ast.Assign) and any(
                isinstance(n, ast.Name) and n.id == "__getattr__"
                for target in node.targets
                for n in ast.walk(target)
            ):
                return True
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "globals":
                    return True
            elif isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
                source = _absolute(module.path, is_package, node.level, node.module)
                if source not in known_modules:
                    return True  # star-import from compiled or external code
    return False


def _absolute(module_path: str, is_package: bool, level: int, name: str | None) -> str:
    if not level:
        return name or ""
    base = module_path.split(".")
    if not is_package:
        base = base[:-1]
    base = base[: len(base) - (level - 1)] if level > 1 else base
    return ".".join([*base, name] if name else base)
