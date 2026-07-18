"""Identity coordinator for :class:`Session`.

Extracted from :mod:`ember_code.core.session.core` — the
``session_id`` / ``session_named`` / ``user_id`` triplet plus the
two invariant-owning methods (``rotate_id``, ``rebind_identity``)
migrate to one class here.

Owns the three-attribute rotation invariant (``session_id``,
``main_team.session_id``, ``persistence.session_id``) so callers
can rotate to a fresh id (``/clear`` / ``/fork``) or rebind to an
existing session (``SessionsController.switch_session``) with a
single method call.

Rule 6 (oop_offender #10): a coordinator class replaces the five
sprawled fields / methods on the Session god-class.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class SessionIdentity:
    """Owns the session-identity triple + its rotation / rebind
    invariants.

    Constructor takes narrow deps: the initial ids, plus
    ``main_team_ref`` and ``persistence_ref`` closures so the
    identity can propagate id changes to the live team + persister
    even after either has been rebuilt.
    """

    def __init__(
        self,
        session_id: str,
        session_named: bool,
        user_id: str,
        main_team_ref: Callable[[], Any],
        persistence_ref: Callable[[], Any],
    ) -> None:
        self._session_id = session_id
        self._session_named = session_named
        self._user_id = user_id
        self._main_team_ref = main_team_ref
        self._persistence_ref = persistence_ref

    # ── Reads ───────────────────────────────────────────────────

    @property
    def session_id(self) -> str:
        """The active session id (8-char hex prefix)."""
        return self._session_id

    @property
    def session_named(self) -> bool:
        """Whether the session was resumed (or has been renamed)."""
        return self._session_named

    @property
    def user_id(self) -> str:
        """The active user id (from :func:`getpass.getuser`)."""
        return self._user_id

    # ── Mutations ───────────────────────────────────────────────

    def mark_named(self) -> None:
        """Flip :attr:`session_named` to ``True``.

        Called after :meth:`SessionPersistence.auto_name` succeeds
        so the flag reflects the on-disk state without callers
        needing to set it directly.
        """
        self._session_named = True

    def rotate(self, new_id: str) -> None:
        """Propagate ``new_id`` to every component that holds the
        active session id.

        Agno keys persistence on ``team.session_id`` (not on our
        ``session.session_id``), so rotating the id in one place
        isn't enough. The three-attribute invariant
        (``session_id``, ``main_team.session_id``,
        ``persistence.session_id``) is encapsulated here so
        external callers don't need to know about the three
        writes.
        """
        self._session_id = new_id
        team = self._main_team_ref()
        if team is not None:
            team.session_id = new_id
        persistence = self._persistence_ref()
        if persistence is not None:
            persistence.session_id = new_id

    async def rebind(self, session_id: str) -> None:
        """Swap the active session identity to an EXISTING
        ``session_id`` and reload its persisted history.

        Superset of :meth:`rotate`: propagates the id to the same
        three attributes, additionally flips
        :attr:`session_named` to ``True`` (a switch is by
        definition to a named / persisted session), and asks
        Agno to reload the target session's persisted history via
        :meth:`Agent.aget_session`.
        """
        self.rotate(session_id)
        self._session_named = True
        team = self._main_team_ref()
        if team is not None:
            await team.aget_session(
                session_id=session_id,
                user_id=self._user_id,
            )
