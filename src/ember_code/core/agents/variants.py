"""Which agent variants exist, and which are live right now.

An agent file named ``<agent>.<variant>.md`` takes over from the plain
``<agent>.md`` whenever its variant is live. That generalised one
hardcoded case — ``.codeindex.md`` — and the generalisation went as
far as :class:`~ember_code.core.agents.loader.AgentDefinitionLoader`
accepting the two sets as arguments and no further: nothing ever
passed them, so the loader fell back to a literal ``{"codeindex"}``
and an extension's variants reached nobody.

This is the table it falls back to instead. ``core`` owns it and
anyone with variants registers into it, which is the same shape
:mod:`ember_code.core.prompts.fragments` already uses and keeps the
dependency pointing the right way: ``core`` does not import the
extension system.

**Known and live are different questions**, and conflating them was
the bug the loader's own docstring warns about. A sibling naming a
variant that is *installed but down* has to be recognised and
skipped — otherwise it loads as an agent whose name happens to
contain a dot.
"""

from __future__ import annotations

import logging
from collections.abc import Collection

logger = logging.getLogger(__name__)


class VariantRegistry:
    """Variant names by owner, and which owners are live."""

    def __init__(self) -> None:
        self._declared: dict[str, frozenset[str]] = {}
        self._live: set[str] = set()

    def declare(self, owner: str, names: Collection[str]) -> None:
        """Say which variants ``owner`` has. Declaring is not activating.

        Re-declaring replaces, which is what an upgrade does — an
        extension that dropped a variant should stop being known for
        it.
        """
        if not names:
            self._declared.pop(owner, None)
            return
        self._declared[owner] = frozenset(names)

    def activate(self, owner: str) -> None:
        """``owner`` is READY; its variants take over."""
        if owner in self._declared:
            self._live.add(owner)

    def deactivate(self, owner: str) -> None:
        """``owner`` is not READY; its variants stop applying.

        It stays *known*, so a ``<agent>.<variant>.md`` sibling is
        still recognised as a variant file and skipped rather than
        loaded as an agent in its own right.
        """
        self._live.discard(owner)

    def remove_owner(self, owner: str) -> None:
        """Forget ``owner`` entirely. Uninstalled, not merely down."""
        self._declared.pop(owner, None)
        self._live.discard(owner)

    def known(self) -> frozenset[str]:
        """Every variant that could appear as a sibling, live or not."""
        return frozenset().union(*self._declared.values()) if self._declared else frozenset()

    def active(self) -> frozenset[str]:
        """The subset that is live now."""
        return (
            frozenset().union(
                *(self._declared[owner] for owner in self._live if owner in self._declared)
            )
            if self._live
            else frozenset()
        )

    def owner_of(self, variant: str) -> str | None:
        for owner, names in self._declared.items():
            if variant in names:
                return owner
        return None


#: The process-wide table. One, because the agent loader is
#: constructed in several places and threading a registry through all
#: of them is how one of them ends up with a different answer.
variants = VariantRegistry()
