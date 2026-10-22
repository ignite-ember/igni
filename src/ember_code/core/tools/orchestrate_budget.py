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

    def release(self, count: int = 1) -> None:
        """Hand ``count`` reserved slots back.

        The reservation has to happen before the sub-runnable is
        built — the cap is there to stop work starting, so it is
        checked first. A build that then fails started no work, and
        keeping its slot makes the budget a count of *attempts*
        rather than of agents. Naming an agent that does not exist
        is the model's documented failure mode on this tool (DPO-4:
        79/79 rejected sides named a phantom ``researcher``), and
        ``AgentPool.get`` rejects it with ``KeyError`` — so twenty
        typos silently retired a session's entire 20-agent budget
        without a single sub-agent having run, with nothing in the
        refusal to suggest why.

        Floors at zero so a double release cannot mint slots that
        were never reserved.
        """
        with self._lock:
            self._count = max(0, self._count - count)

    def reset(self) -> None:
        """Drop the counter to zero. Called on session teardown."""
        with self._lock:
            self._count = 0


__all__ = ["SpawnBudget"]
