from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Hashable, Iterable
from typing import TypeVar


Node = TypeVar("Node", bound=Hashable)


class DependencyCycleError(ValueError):
    def __init__(self, nodes: list[Hashable]) -> None:
        super().__init__("dependency graph contains a cycle")
        self.nodes = nodes


def topological_levels(
    nodes: Iterable[Node], dependencies: Iterable[tuple[Node, Node]]
) -> list[list[Node]]:
    """Return levels for `(dependent, prerequisite)` edges using Kahn's algorithm."""

    node_set = set(nodes)
    indegree = {node: 0 for node in node_set}
    downstream: dict[Node, set[Node]] = defaultdict(set)
    for dependent, prerequisite in dependencies:
        if dependent not in node_set or prerequisite not in node_set:
            raise ValueError("dependency references a node outside the group")
        if dependent == prerequisite:
            raise DependencyCycleError([dependent])
        if dependent not in downstream[prerequisite]:
            downstream[prerequisite].add(dependent)
            indegree[dependent] += 1

    current = sorted((n for n, degree in indegree.items() if degree == 0), key=str)
    levels: list[list[Node]] = []
    visited = 0
    while current:
        levels.append(current)
        visited += len(current)
        following: list[Node] = []
        for node in current:
            for child in sorted(downstream[node], key=str):
                indegree[child] -= 1
                if indegree[child] == 0:
                    following.append(child)
        current = sorted(following, key=str)
    if visited != len(node_set):
        raise DependencyCycleError(sorted((n for n, degree in indegree.items() if degree), key=str))
    return levels


def reachable_descendants(
    roots: Iterable[Node], dependencies: Iterable[tuple[Node, Node]]
) -> set[Node]:
    downstream: dict[Node, set[Node]] = defaultdict(set)
    for dependent, prerequisite in dependencies:
        downstream[prerequisite].add(dependent)
    seen: set[Node] = set()
    queue: deque[Node] = deque(roots)
    while queue:
        node = queue.popleft()
        for child in downstream[node]:
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return seen

