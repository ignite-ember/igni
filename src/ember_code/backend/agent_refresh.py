"""Rebuilding the agents when what extensions do to them changes.

An extension becoming ready, one going down, a condition it reports
moving ("my index covers this commit now") — each changes what the
agent's prompt says or which agent definition stands under a name.
:class:`~ember_code.core.prompts.contributions.PromptContributions`
says so whenever it happens, sometimes several times in a row as one
extension's claims land, and this turns that into one rebuild of every
session's agents, on the next turn of the event loop.

A run already under way finishes with the agent it started with; the
next one gets the new prompt.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)


class AgentRefresh:
    """Coalesces requests to rebuild the agents into one rebuild."""

    def __init__(self, *, loop: asyncio.AbstractEventLoop, refresh: Callable[[], None]) -> None:
        self._loop = loop
        self._refresh = refresh
        self._scheduled = False

    def request(self) -> None:
        """Rebuild soon. Safe from any thread, and cheap to call often."""
        if self._scheduled or self._loop.is_closed():
            return
        self._scheduled = True
        self._loop.call_soon_threadsafe(self._run)

    def _run(self) -> None:
        self._scheduled = False
        try:
            self._refresh()
        except Exception:  # noqa: BLE001 — a failed rebuild keeps the agents that work
            logger.exception("rebuilding the agents after an extension change failed")
