"""The API surface of one version of one package, whatever its language.

A tree of nodes keyed by canonical id; `roots` maps each importable module (the first path
segment, see `ghostapi.paths`) to its node. Builders: `pysurface` (Python, via griffe) and
`dts` (TypeScript declarations, via tree-sitter).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ghostapi.models import ApiSymbol, Param
from ghostapi.paths import child, segments

FORMAT = 1  # bump when extraction logic changes, to invalidate cached surfaces
EXTERNAL = "<external>"  # member defined outside the package: it exists, but we can't see into it


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
        parts = segments(path)
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
        return len(self.walk(path)) == len(segments(path))

    def public_paths(self, name: str, kind: str, max_depth: int = 8) -> list[str]:
        """Public paths that end in `name` and point at a `kind` object."""
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
                member_path = child(path, member)
                if member == name and self.node(target).kind == kind:
                    found.append(member_path)
                queue.append((member_path, target, depth + 1))
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
