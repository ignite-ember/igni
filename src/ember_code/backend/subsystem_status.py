"""Optional subsystems may switch themselves off. Not anonymously.

Every panel in this app has had to invent its own vocabulary for "there
is nothing here", and each one invented a worse version of the same
four states. Knowledge said *"Knowledge base failed to initialize"* for
a healthy first launch that was mid-download, *"disabled"* for a
feature that config said was enabled, and nothing at all for a sidecar
that died in 63 ms. CodeIndex says ``not indexed`` and points at the
portal. Neither could say **why**, because nothing recorded a why.

The rule this encodes: a subsystem that is not working owes the user
three things — which state it is in, the reason in a sentence, and what
to do about it. A panel should not have to guess any of them from the
absence of data.

Deliberately tiny, and deliberately not a framework. It is a dict with
opinions about its values, held on the session so that anything with a
session can both write and read it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SubsystemState(str, Enum):
    """The four states a panel actually needs to tell apart.

    ``str``-valued so it crosses the wire as itself and reads correctly
    in a log line without a lookup.
    """

    #: Working.
    READY = "ready"
    #: Coming up. Not an error, and the distinction matters: the first
    #: launch of the knowledge base spends minutes here downloading, and
    #: calling that "failed" is a lie that sends people to fix a
    #: working system.
    PREPARING = "preparing"
    #: Off because someone said so. A choice, not a fault.
    DISABLED = "disabled"
    #: Tried, could not. Always carries a reason.
    FAILED = "failed"


@dataclass(frozen=True)
class SubsystemStatus:
    """One subsystem's state, with the two strings a user needs.

    ``reason`` answers "why is this not working". ``fix`` answers "what
    do I do". They are separate because the second is often actionable
    when the first is inscrutable: "the sidecar exited immediately" is
    not something a user can act on, while "delete ~/.ember/neo4j and
    reopen the app" is.
    """

    name: str
    state: SubsystemState
    reason: str = ""
    fix: str = ""

    @property
    def working(self) -> bool:
        return self.state is SubsystemState.READY

    def to_wire(self) -> dict[str, str]:
        return {
            "name": self.name,
            "state": self.state.value,
            "reason": self.reason,
            "fix": self.fix,
        }


class SubsystemRegistry:
    """Per-session record of which optional parts are up, and why not.

    Not thread-safe and not trying to be: writes happen on the
    backend's event loop, reads happen on RPC handlers that share it.
    """

    def __init__(self) -> None:
        self._entries: dict[str, SubsystemStatus] = {}

    def set(
        self,
        name: str,
        state: SubsystemState,
        *,
        reason: str = "",
        fix: str = "",
    ) -> SubsystemStatus:
        """Record a subsystem's state.

        A ``FAILED`` with no reason is the exact bug this module
        exists to prevent, so it is refused loudly rather than stored
        and discovered later by someone staring at an empty panel.
        """
        if state is SubsystemState.FAILED and not reason:
            raise ValueError(f"{name}: a failed subsystem must say why")
        status = SubsystemStatus(name=name, state=state, reason=reason, fix=fix)
        self._entries[name] = status
        return status

    def get(self, name: str) -> SubsystemStatus | None:
        return self._entries.get(name)

    def all(self) -> list[SubsystemStatus]:
        """Every known subsystem, name order — a stable list is what a
        diagnostics view wants."""
        return [self._entries[k] for k in sorted(self._entries)]

    def to_wire(self) -> list[dict[str, str]]:
        return [s.to_wire() for s in self.all()]


#: Canonical names. Strings shared between a writer and a reader are
#: exactly the thing that drifts — the sentinel-as-contract problem
#: this codebase has hit repeatedly — so they live here once.
KNOWLEDGE = "knowledge"
CODE_INDEX = "code_index"
