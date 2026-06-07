"""Dijkstra's shortest-path on a weighted graph.

Genuinely complex (priority queue + relaxation + early-exit), but
well-structured and well-covered. Demonstrates that ``complexity=high``
doesn't always mean poor quality — some algorithms are just intricate.

Tagged ``complexity=high``, ``quality=good``, ``testing=well-tested``,
``patterns=[dijkstra]``, ``domain=[algorithms]``.
"""

from __future__ import annotations

import heapq
from typing import Hashable


def shortest_path(
    graph: dict[Hashable, dict[Hashable, float]],
    source: Hashable,
    target: Hashable,
) -> tuple[float, list[Hashable]] | None:
    """Return ``(total_weight, path)`` from ``source`` to ``target``.

    ``graph[u][v] = weight`` describes a directed edge u→v.
    Returns ``None`` if no path exists. Negative weights are NOT
    supported — that's a Bellman-Ford problem, not Dijkstra.
    """
    if source not in graph or target not in graph:
        return None

    dist: dict[Hashable, float] = {source: 0.0}
    prev: dict[Hashable, Hashable] = {}
    heap: list[tuple[float, Hashable]] = [(0.0, source)]

    while heap:
        d, u = heapq.heappop(heap)
        if u == target:
            return d, _reconstruct(prev, source, target)
        if d > dist.get(u, float("inf")):
            continue
        for v, weight in graph.get(u, {}).items():
            if weight < 0:
                raise ValueError("Dijkstra requires non-negative edge weights")
            alt = d + weight
            if alt < dist.get(v, float("inf")):
                dist[v] = alt
                prev[v] = u
                heapq.heappush(heap, (alt, v))
    return None


def _reconstruct(
    prev: dict[Hashable, Hashable], source: Hashable, target: Hashable
) -> list[Hashable]:
    """Walk back from target to source via the predecessor map."""
    path = [target]
    cursor = target
    while cursor != source:
        cursor = prev[cursor]
        path.append(cursor)
    path.reverse()
    return path
