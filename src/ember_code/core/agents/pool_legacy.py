"""QUARANTINE / SHIM — legacy backward-compat surface for :class:`AgentPool`.

This module exists ONLY so the existing test suite continues to
compile without a same-PR migration. It quarantines every
``pool._definitions[...]`` / ``pool._codeindex_available`` /
``pool._ephemeral_count`` / ``pool._max_ephemeral`` /
``pool._ephemeral_dir`` / ``pool._load_directory`` reach-in
behind ONE mixin + ONE adapter class so a follow-up PR has a
single grep-and-delete target.

Reach-in call sites still known to depend on this shim (as of the
audit that spawned this file — grep before deleting):

* ``tests/test_ephemeral_agents.py``          — ``_max_ephemeral``, ``_ephemeral_count``
* ``tests/test_plugin_agent_restrictions.py`` — ``_definitions``, ``_codeindex_available``, ``_load_directory``
* ``tests/test_codeindex_availability_refresh.py`` — ``_definitions[...] = (defn, prio)``
* ``scripts/run_codeindex_eval.py``           — ``pool._definitions[...]``

Two names are exported:

* :class:`LegacyAgentPoolMixin` — legacy properties + ``_load_directory``.
  Composed into :class:`AgentPool` via inheritance so ``pool._definitions``
  still resolves. Every property routes through the *public* API of
  :class:`AgentPool` (``iter_entry_items`` / ``upsert_entry`` / ``remove``
  / ``clear_entries``) — zero private-attribute reach-ins.
* :class:`_LegacyDefinitionsView` — dict-like adapter emitting the
  ``(AgentDefinition, priority_int)`` tuples that the legacy tests
  still expect on iteration + subscript.

New code MUST NOT import from this module. New pool consumers use
:meth:`AgentPool.get_entry`, :meth:`AgentPool.iter_entries`,
:meth:`AgentPool.upsert_entry`, :meth:`AgentPool.remove`, and
:meth:`AgentPool.clear_entries` directly.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from ember_code.core.agents.loader import AgentDefinitionLoader
from ember_code.core.agents.plugin_policy import PluginRestrictionPolicy
from ember_code.core.agents.schemas import (
    AgentDefinition,
    AgentEntry,
    AgentPriority,
)

if TYPE_CHECKING:
    from ember_code.core.agents.pool import AgentPool


class LegacyAgentPoolMixin:
    """Legacy backward-compat descriptors mixed into :class:`AgentPool`.

    The mixin only defines *descriptors* (properties + one method).
    All actual state stays on :class:`AgentPool` itself. Every
    property routes through the pool's public API.

    Data-descriptor precedence means the mixin's ``_codeindex_available``
    property intentionally shadows any same-named instance field on
    :class:`AgentPool` — we deliberately keep the raw flag under a
    different name (``_codeindex_available_flag``) so the shadowing
    is stable.
    """

    # --- _definitions ────────────────────────────────────────────

    @property
    def _definitions(self: AgentPool) -> _LegacyDefinitionsView:  # type: ignore[misc]
        """Legacy dict-like view emitting ``(AgentDefinition, int)``.

        Reading routes through :meth:`AgentPool.get_entry` /
        :meth:`AgentPool.iter_entry_items`. Writing routes through
        :meth:`AgentPool.upsert_entry`.
        """
        # Support the ``AgentPool.__new__``-and-assign path used by
        # ``tests/test_plugin_agent_restrictions.py`` (bypasses
        # ``__init__``, then assigns ``_definitions = {}``).
        self._ensure_initialised()
        return _LegacyDefinitionsView(self)

    @_definitions.setter
    def _definitions(  # type: ignore[misc]
        self: AgentPool, value: dict | _LegacyDefinitionsView
    ) -> None:
        """Assignment ``pool._definitions = {...}`` clears then
        replays entries through :meth:`AgentPool.replace_entries_from`.
        """
        if isinstance(value, _LegacyDefinitionsView):
            # Assigning the view back to itself is a no-op.
            return
        self._ensure_initialised()
        self.replace_entries_from(value)

    # --- _codeindex_available ────────────────────────────────────

    @property
    def _codeindex_available(self: AgentPool) -> bool:  # type: ignore[misc]
        """Legacy read of the last-known codeindex flag.

        Shadows any instance attribute of the same name via
        data-descriptor precedence — see class docstring.
        """
        return bool(getattr(self, "_codeindex_available_flag", False))

    @_codeindex_available.setter
    def _codeindex_available(self: AgentPool, value: bool) -> None:  # type: ignore[misc]
        """Legacy write — captured for the next ``_load_directory``."""
        self._codeindex_available_flag = bool(value)

    # --- _ephemeral_count ────────────────────────────────────────

    @property
    def _ephemeral_count(self: AgentPool) -> int:  # type: ignore[misc]
        """Derived count — matches :attr:`EphemeralAgentStore.count`
        when initialised, otherwise scans the entry store."""
        if self._ephemeral is None:
            return sum(
                1
                for _, entry in self.iter_entry_items()
                if entry.priority == AgentPriority.EPHEMERAL
            )
        return self._ephemeral.count

    @_ephemeral_count.setter
    def _ephemeral_count(self: AgentPool, value: int) -> None:  # type: ignore[misc]
        """Legacy write — noop. The count is derived from entries."""
        return

    # --- _max_ephemeral ──────────────────────────────────────────

    @property
    def _max_ephemeral(self: AgentPool) -> int:  # type: ignore[misc]
        if self._ephemeral is None:
            return 5
        return self._ephemeral.max_ephemeral

    @_max_ephemeral.setter
    def _max_ephemeral(self: AgentPool, value: int) -> None:  # type: ignore[misc]
        if self._ephemeral is not None:
            self._ephemeral.max_ephemeral = value

    # --- _ephemeral_dir ──────────────────────────────────────────

    @property
    def _ephemeral_dir(self: AgentPool) -> Path | None:  # type: ignore[misc]
        if self._ephemeral is None:
            return None
        return self._ephemeral.directory

    # --- _load_directory ────────────────────────────────────────

    def _load_directory(
        self: AgentPool,
        path: Path,
        priority: AgentPriority | int,
        namespace: str | None = None,
        plugin_restricted: bool = False,
    ) -> None:
        """Legacy loader shim — kept so
        :class:`PluginLoader.apply_to_agents` and
        ``tests/test_plugin_agent_restrictions.py`` continue to work.

        Prefer :meth:`AgentPool.load_plugin_directory` /
        :meth:`AgentPool.load_directory` in new code.
        """
        policy = PluginRestrictionPolicy.strict() if plugin_restricted else None
        loader = AgentDefinitionLoader(
            settings=self._settings_or_bare(),
            project_dir=Path(self._base_dir) if self._base_dir else Path.cwd(),
            codeindex_available=self._codeindex_available,
            restriction_policy=policy,
        )
        report = loader.load_directory(path, priority, namespace=namespace)
        self._merge(report)


class _LegacyDefinitionsView:
    """QUARANTINE — dict-like view over :meth:`AgentPool.iter_entry_items`
    emitting ``(AgentDefinition, int)`` tuples.

    Only the (few) legacy call sites listed at the top of this
    module still reach for it. New code MUST use
    :meth:`AgentPool.get_entry`, :meth:`AgentPool.iter_entries`,
    :meth:`AgentPool.upsert_entry`, :meth:`AgentPool.remove`, and
    :meth:`AgentPool.clear_entries` directly.

    All access goes through the pool's public API — zero
    ``self._pool._entries`` reach-ins (Rule 6).
    """

    def __init__(self, pool: AgentPool) -> None:
        self._pool = pool

    def __getitem__(self, name: str) -> tuple[AgentDefinition, int]:
        entry = self._pool.get_entry(name)
        return entry.definition, int(entry.priority)

    def __setitem__(self, name: str, value: object) -> None:
        # ``name`` is ignored — the entry's own definition.name is
        # the pool key. This matches the semantics of the pre-mixin
        # setter and the tests that write ``pool._definitions[name]``
        # always pass a definition whose ``.name`` matches ``name``.
        entry = AgentEntry.from_legacy_pair(value)
        self._pool.upsert_entry(entry)

    def __delitem__(self, name: str) -> None:
        self._pool.remove(name)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self._pool.has_definition(name)

    def __iter__(self) -> Iterable[str]:
        return iter(name for name, _ in self._pool.iter_entry_items())

    def __len__(self) -> int:
        return sum(1 for _ in self._pool.iter_entry_items())

    def get(self, name: str, default: object = None) -> tuple[AgentDefinition, int] | object:
        if not self._pool.has_definition(name):
            return default
        entry = self._pool.get_entry(name)
        return entry.definition, int(entry.priority)

    def items(self) -> list[tuple[str, tuple[AgentDefinition, int]]]:
        return [
            (name, (entry.definition, int(entry.priority)))
            for name, entry in self._pool.iter_entry_items()
        ]

    def values(self) -> list[tuple[AgentDefinition, int]]:
        return [
            (entry.definition, int(entry.priority)) for _, entry in self._pool.iter_entry_items()
        ]

    def keys(self) -> list[str]:
        return [name for name, _ in self._pool.iter_entry_items()]

    def clear(self) -> None:
        self._pool.clear_entries()

    def pop(self, name: str, default: object = None) -> tuple[AgentDefinition, int] | object:
        if not self._pool.has_definition(name):
            return default
        entry = self._pool.get_entry(name)
        self._pool.remove(name)
        return entry.definition, int(entry.priority)


__all__ = ["LegacyAgentPoolMixin", "_LegacyDefinitionsView"]
