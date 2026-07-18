"""Friendly-name ↔ internal-name resolver for tool permission rules.

Encapsulates the reverse-index that used to live as a module-level
``_FRIENDLY_TO_INTERNAL`` dict cached across the whole process.

The class:

* Owns the reverse index (``friendly → set(internal)``) as an instance
  attribute — no module-level mutable state, no ``global`` reassignment.
* Populates the index lazily on the first ``.internals_for(name)`` call
  so instantiating an evaluator doesn't boot Agno (Agno is heavy; the
  permission_eval package is on the CLI startup path).
* Supports dependency injection: tests build a resolver with an
  explicit ``friendly_names_provider`` dict; production code uses
  :meth:`FriendlyToolNameResolver.default` which pulls the mapping
  from the shared ``ToolCallFormatterRegistry`` on first use.
* Ships a module-private cached ``default()`` instance so many
  evaluators constructed in a run share the reverse-index build
  cost — but each caller can still override with ``resolver=...``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from ember_code.protocol.agno_tool_formatter import default_registry


class FriendlyToolNameResolver:
    """Reverse-index for the catalog friendly-name ↔ internal-name map.

    Replaces the ``_FRIENDLY_TO_INTERNAL`` module cache + free
    ``_internals_for_friendly`` function. Every evaluator gets a
    resolver (defaults to the shared process-wide instance); tests
    can inject an alternate mapping.
    """

    #: Process-wide shared instance built on first use. Not populated
    #: until :meth:`internals_for` runs — construction is cheap.
    _default_instance: FriendlyToolNameResolver | None = None

    def __init__(
        self,
        friendly_names_provider: Callable[[], Mapping[str, str]] | None = None,
    ) -> None:
        """Build a resolver.

        ``friendly_names_provider`` is a callable returning an
        ``internal_name → friendly_name`` mapping (matches the shape
        exposed by :class:`ToolCallFormatterRegistry.friendly_names`).
        Defaults to :func:`_registry_provider` — pulled from the
        shared Agno registry the first time the reverse map is asked
        for.
        """
        self._provider = friendly_names_provider or _registry_provider
        self._reverse: dict[str, frozenset[str]] | None = None

    def internals_for(self, friendly: str) -> frozenset[str]:
        """Return the set of internal tool names that map to
        ``friendly``. Empty set when no mapping exists.

        First call triggers the (lazy) build of the reverse index —
        subsequent calls hit the cached dict.
        """
        if self._reverse is None:
            self._reverse = self._build_reverse()
        return self._reverse.get(friendly, frozenset())

    def _build_reverse(self) -> dict[str, frozenset[str]]:
        """Invert the ``internal → friendly`` map into
        ``friendly → set(internal)``. Called exactly once per
        resolver instance."""
        names = self._provider()
        buckets: dict[str, set[str]] = {}
        for internal, friendly in names.items():
            buckets.setdefault(friendly, set()).add(internal)
        return {k: frozenset(v) for k, v in buckets.items()}

    def reset(self) -> None:
        """Drop the cached reverse map — the next
        :meth:`internals_for` will rebuild. Used only by tests that
        swap the underlying provider between assertions."""
        self._reverse = None

    @classmethod
    def default(cls) -> FriendlyToolNameResolver:
        """Return the shared process-wide resolver, building it on
        first use. Multiple evaluators share the built reverse index
        this way (matches the old module-level cache's behaviour but
        with instance ownership)."""
        if cls._default_instance is None:
            cls._default_instance = cls()
        return cls._default_instance


def _registry_provider() -> Mapping[str, str]:
    """Default provider — pulls ``friendly_names`` from the shared
    Agno tool-call formatter registry.

    Kept as a module-level function (rather than inlined inside
    ``__init__``) so tests can compare identity when wiring a custom
    provider.
    """
    return default_registry().friendly_names
