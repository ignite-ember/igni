"""Which prompt conditionals are true, and who says so.

A prompt carries ``{{#artifacts}}…{{/artifacts}}`` sections that
are kept or dropped depending on what the session can actually do.
A list of things not to do with a tool nobody has is worse than
silence, so the answers have to be right.

igni used to know them because it owned the capabilities: the
session built a document store, so it could answer "is there one".
Now a capability is an extension, and the host does not know what
is installed — nor should it. The alternative to this module was a
line in ``backend/app.py`` reading ``if extension_id ==
"igni.artifacts"``, which is igni knowing one extension by name:
the exact thing an extension platform exists to stop.

So an extension *declares* which conditionals it satisfies, and
this is the table it declares into. ``core`` owns it and anyone
with conditionals registers, which is the shape
:mod:`ember_code.core.agents.variants` and
:mod:`ember_code.core.prompts.fragments` already use, and keeps
the dependency pointing the right way: ``core`` does not import
the extension system.

**Only live matters here**, unlike variants. A variant that is
known-but-down still has to be recognised, because its agent file
sits on disk either way. A conditional that is down is simply
false: the prompt drops the section, which is the correct thing to
tell a model about a capability that is not answering.
"""

from __future__ import annotations

import logging
from collections.abc import Collection

logger = logging.getLogger(__name__)


class ConditionalRegistry:
    """Prompt conditional names, by whoever currently satisfies them."""

    def __init__(self) -> None:
        self._by_owner: dict[str, frozenset[str]] = {}

    def declare(self, owner: str, names: Collection[str]) -> None:
        """Say which conditionals ``owner`` satisfies.

        Re-declaring replaces, which is what an upgrade does — an
        extension that dropped a conditional should stop answering
        yes to it.
        """
        if not names:
            self._by_owner.pop(owner, None)
            return
        self._by_owner[owner] = frozenset(names)
        logger.debug("%s satisfies %s", owner, sorted(names))

    def remove_owner(self, owner: str) -> None:
        """Forget everything ``owner`` claimed.

        Called when it stops being READY as well as when it is
        uninstalled: a capability whose service has died should
        stop telling the model it is there, which is the rule the
        prompt anchors and agent variants already follow.
        """
        self._by_owner.pop(owner, None)

    def live(self) -> frozenset[str]:
        """Every conditional something currently satisfies."""
        names: set[str] = set()
        for owned in self._by_owner.values():
            names |= owned
        return frozenset(names)

    def owners_of(self, name: str) -> list[str]:
        """Who answers yes to this one.

        Several may, and that is not a collision: two extensions
        both offering somewhere to persist both make
        ``can-persist`` true, and neither has to know about the
        other.
        """
        return sorted(o for o, names in self._by_owner.items() if name in names)


#: The table the prompt builder falls back to when nobody passes one.
#:
#: Module scope for the reason :data:`~ember_code.core.agents.variants.variants`
#: is: the thing that registers and the thing that reads are wired
#: at different times, by different layers.
conditionals = ConditionalRegistry()

__all__ = ["ConditionalRegistry", "conditionals"]
