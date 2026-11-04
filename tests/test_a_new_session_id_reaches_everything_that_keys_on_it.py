"""A new session id reaches everything that keys on it, at once.

Agno keys stored history on ``team.session_id`` and igni's persistence on
its own copy, so ``/clear`` and ``/fork`` rotating only ``Session.session_id``
would write the next turn into the old session. :class:`SessionIdentity`
owns that invariant. Pinned here:

* :meth:`rotate` sets the id on the session, the live team and the
  persister — the ones current *now*, read through their refs, so a team
  rebuilt after construction still gets it — and survives either being
  absent (before start-up finished);
* :meth:`rebind` (switching to an existing session) does the same, marks
  the session as named, and reloads that session's history for this user;
* :meth:`mark_named` is what a successful auto-name flips.
"""

from __future__ import annotations

from typing import Any

from ember_code.core.session.identity import SessionIdentity


class _Team:
    def __init__(self) -> None:
        self.session_id = "old"
        self.loaded: list[tuple[str, str]] = []

    async def aget_session(self, *, session_id: str, user_id: str) -> None:
        self.loaded.append((session_id, user_id))


class _Persistence:
    session_id = "old"


class _Holder:
    """Stands in for the session's slots, which are rebuilt over its life."""

    def __init__(self, team: Any = None, persistence: Any = None) -> None:
        self.team = team
        self.persistence = persistence


def _identity(holder: _Holder, named: bool = False) -> SessionIdentity:
    return SessionIdentity(
        session_id="old",
        session_named=named,
        user_id="dana",
        main_team_ref=lambda: holder.team,
        persistence_ref=lambda: holder.persistence,
    )


def test_rotate_reaches_the_session_the_team_and_the_persister():
    holder = _Holder(_Team(), _Persistence())
    identity = _identity(holder)

    identity.rotate("new")

    assert identity.session_id == holder.team.session_id == holder.persistence.session_id == "new"
    assert identity.user_id == "dana"
    assert not identity.session_named


def test_rotate_reaches_a_team_rebuilt_after_the_identity_was_made():
    holder = _Holder(_Team(), _Persistence())
    identity = _identity(holder)
    holder.team = _Team()

    identity.rotate("new")

    assert holder.team.session_id == "new"


def test_rotate_before_start_up_finished_changes_only_the_id():
    identity = _identity(_Holder())

    identity.rotate("new")

    assert identity.session_id == "new"


async def test_rebind_switches_to_the_session_and_reloads_its_history():
    holder = _Holder(_Team(), _Persistence())
    identity = _identity(holder)

    await identity.rebind("existing")

    assert identity.session_id == holder.persistence.session_id == "existing"
    assert identity.session_named
    assert holder.team.loaded == [("existing", "dana")]


async def test_rebind_without_a_team_still_switches_the_id():
    identity = _identity(_Holder(persistence=_Persistence()))

    await identity.rebind("existing")

    assert identity.session_id == "existing"
    assert identity.session_named


def test_a_successful_auto_name_marks_the_session_named():
    identity = _identity(_Holder())

    identity.mark_named()

    assert identity.session_named
