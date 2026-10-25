"""Where igni asks before it acts: the agent's decision points, as a seam.

igni's core decides what the agent does — run this tool, put this plan
to the person. Something outside it may want a say before it happens:
the extensions that intercept those events
(:mod:`ember_code.extensions.host.intercepts`). The core does not know
about extensions; it asks :class:`EventGate`, and whoever runs igni
installs what answers. Nothing installed, everything goes ahead.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from pydantic import BaseModel


class GateVerdict(BaseModel):
    """Whether an event goes ahead, and with what."""

    allowed: bool
    #: The value it goes ahead with: changed by whoever changed it.
    value: dict[str, Any]
    #: Why not, for an event that does not go ahead.
    reason: str = ""
    #: Who stopped it.
    by: str = ""


Asker = Callable[[str, dict[str, Any]], Awaitable[GateVerdict]]


class EventGate:
    """The process's one gate. The backend installs what answers it."""

    _asker: ClassVar[Asker | None] = None

    @classmethod
    def install(cls, asker: Asker | None) -> None:
        cls._asker = asker

    @classmethod
    async def ask(cls, event: str, value: dict[str, Any]) -> GateVerdict:
        """Ask before ``event`` happens; allowed as it is when nobody answers the gate."""
        if cls._asker is None:
            return GateVerdict(allowed=True, value=value)
        return await cls._asker(event, value)


__all__ = ["Asker", "EventGate", "GateVerdict"]
