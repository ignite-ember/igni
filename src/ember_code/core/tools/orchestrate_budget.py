"""Per-session sub-agent budget for :mod:`orchestrate`.

Owns the counter that used to live as module-globals
(``_agent_counter_lock`` / ``_agent_counters``) in
``orchestrate.py``. One :class:`SpawnBudget` per session; the
per-session instance is served by
:meth:`AgentPool.spawn_budget`, so every
:class:`OrchestrateTools` created for that session shares the
same counter — replaces the module-dict-keyed-by-session-id
pattern with an object-per-session.

Rule 6 (no module-level mutable state) — the counter, the lock,
and the ``reset`` operation all live on one instance now.
"""

from __future__ import annotations

import threading


class SpawnBudget:
    """Tracks the number of sub-agents spawned within one session.

    :meth:`try_reserve` atomically checks the requested count
    against the cap and either reserves the slots (returns
    ``None``) or refuses with an error string. :meth:`reset`
    drops the counter to zero — called on session teardown via
    :meth:`AgentPool.forget_spawn_budget`.

    ``max_agents`` is the hard cap:
    :class:`OrchestrateTools` surfaces ``"Error: Maximum total
    agents (N) reached."`` to the parent agent when a spawn
    would push :attr:`count` past it.
    """

    __slots__ = ("_max_agents", "_count", "_lock")

    def __init__(self, max_agents: int) -> None:
        self._max_agents = int(max_agents)
        self._count = 0
        self._lock = threading.Lock()

    @property
    def max_agents(self) -> int:
        return self._max_agents

    @property
    def count(self) -> int:
        """Current number of reserved sub-agents. Read without a
        lock — a race here just means a slightly-stale display, and
        the authoritative check is inside :meth:`try_reserve`."""
        return self._count

    def try_reserve(self, count: int = 1) -> str | None:
        """Reserve ``count`` sub-agent slots atomically.

        Returns ``None`` on success and an error string when the
        reservation would blow the budget. Same contract as the
        ex-``OrchestrateTools._check_agent_limit`` — moved onto the
        budget class so the invariant ``count <= max_agents`` and
        the mutation live in one place.
        """
        with self._lock:
            if self._count + count > self._max_agents:
                return f"Error: Maximum total agents ({self._max_agents}) reached."
            self._count += count
            return None

    def reset(self) -> None:
        """Drop the counter to zero. Called on session teardown."""
        with self._lock:
            self._count = 0


__all__ = ["SpawnBudget"]
