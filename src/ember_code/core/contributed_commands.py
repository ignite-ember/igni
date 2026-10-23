"""Slash commands an extension answers.

``contributes.commands`` has been in the manifest and the schema
since the contract was written, and nothing read it — an extension
could declare ``/compliance`` and typing it got the skill fallback,
because the catalog had three sources and the dispatcher had three
tiers and neither knew about a fourth.

This is the table they both consult. ``core`` owns it and the
extension layer registers into it, which is the same shape
:mod:`ember_code.core.prompts.fragments` and
:mod:`ember_code.core.agents.variants` already use and keeps ``core``
from importing the extension system.

**The invoker lives here too.** A registry of names alone would mean
the dispatcher had to reach back into the extension layer to run
one, which is the import this arrangement exists to avoid. Whoever
registers a command supplies the coroutine that runs it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: ``(args) -> text``. The rest of the line, and what to show.
CommandInvoker = Callable[[str], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class ContributedCommand:
    """One command, who owns it, and how to run it."""

    #: Bare, no leading slash — the form the dispatcher compares.
    name: str
    description: str
    owner: str
    invoke: CommandInvoker


class ContributedCommands:
    """Commands contributed by whatever is installed."""

    def __init__(self) -> None:
        self._by_name: dict[str, ContributedCommand] = {}

    def register(
        self,
        name: str,
        *,
        owner: str,
        description: str,
        invoke: CommandInvoker,
    ) -> str | None:
        """Claim a command name. Returns the reason it was refused.

        A name already held by somebody else is refused rather than
        replaced: two extensions both answering ``/scan`` and the
        winner decided by install order is a product that behaves
        differently on two machines with the same software on them.
        """
        bare = name.lstrip("/").strip().lower()
        if not bare:
            return "a command needs a name"
        held = self._by_name.get(bare)
        if held is not None and held.owner != owner:
            return f"'/{bare}' is already answered by '{held.owner}'"
        self._by_name[bare] = ContributedCommand(
            name=bare, description=description, owner=owner, invoke=invoke
        )
        return None

    def remove_owner(self, owner: str) -> None:
        for name in [n for n, c in self._by_name.items() if c.owner == owner]:
            del self._by_name[name]

    def get(self, name: str) -> ContributedCommand | None:
        return self._by_name.get(name.lstrip("/").strip().lower())

    def all(self) -> list[ContributedCommand]:
        return [self._by_name[name] for name in sorted(self._by_name)]


#: The process-wide table. One, because the catalog and the
#: dispatcher are built in different places and must not disagree
#: about what exists.
contributed_commands = ContributedCommands()
