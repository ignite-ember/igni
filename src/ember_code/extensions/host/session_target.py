"""Which session a per-session request is about: ``host.sessions.chat(id)``.

``host.chat`` is the session on screen. ``host.sessions.chat(id)`` is the
same object for any session of this project: its requests carry the
session's id (``Request.session``). :class:`SessionTargets` decides
whether the request may reach it:

* only an object that belongs to a session (``per_session``) takes one;
* a property reads any session; a method acts on one that is not on
  screen only when it names the grant that takes
  (``MethodSpec.elsewhere``) and the extension holds it — a write where
  the person is not looking is broader than a write in front of them;
* the session must be this project's — live, or in the list the
  person's sidebar shows — and is reached by the check a window's attach
  passes, so another project's session is refused as the person's
  would be. An id that is none of them is not created: it is not found.

Named by its id, the session on screen is ``host.chat`` and nothing
else: the same grants, the same behaviour.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from ember_code.extensions.host.errors import HostModelError
from ember_code.extensions.host.grants import HostGrants
from ember_code.extensions.host.spec import (
    ErrorCode,
    MethodSpec,
    ObjectSpec,
    PropertySpec,
    SignalSpec,
)
from ember_code.extensions.host.wire import Op, Request
from ember_code.protocol.rpc import RpcMethod

if TYPE_CHECKING:
    from ember_code.extensions.host.model import HostModel


class SessionTarget(BaseModel):
    """The session a request is about, and whether it is the one on screen."""

    session_id: str
    on_screen: bool


class SessionTargets:
    """Resolves ``Request.session`` against the grants and the project."""

    def __init__(self, model: HostModel) -> None:
        self._model = model

    async def resolve(
        self,
        *,
        grants: HostGrants,
        spec: ObjectSpec,
        member: PropertySpec | SignalSpec | MethodSpec,
        request: Request,
        on_screen: str,
    ) -> SessionTarget:
        wanted = request.session
        if wanted is not None and not spec.per_session:
            raise HostModelError(ErrorCode.INVALID, f"'{spec.path}' does not belong to a session")
        if wanted is None or wanted == on_screen:
            return SessionTarget(session_id=on_screen, on_screen=True)
        if not wanted:
            raise HostModelError(ErrorCode.INVALID, "a session id is required")
        self._may_reach(grants, member, request)
        if not await self._known(wanted, on_screen):
            raise HostModelError(ErrorCode.NOT_FOUND, f"no session {wanted!r} in this project")
        attached = await self._model.attach(wanted, None)
        return SessionTarget(session_id=attached["session_id"], on_screen=False)

    @staticmethod
    def _may_reach(
        grants: HostGrants, member: PropertySpec | SignalSpec | MethodSpec, request: Request
    ) -> None:
        path = request.path
        if isinstance(member, PropertySpec):
            if request.op is Op.SET:
                raise HostModelError(
                    ErrorCode.REFUSED, f"'{path}' is set only in the session on screen"
                )
            return
        if not isinstance(member, MethodSpec):
            return
        if member.elsewhere is None:
            raise HostModelError(
                ErrorCode.REFUSED,
                f"'{path}' acts only on the session on screen; in another session an "
                "extension reads, posts and appends",
            )
        if not grants.allows(member.elsewhere):
            raise HostModelError.not_granted(
                grants.extension_id,
                f"'{path}' in a session that is not on screen",
                member.elsewhere,
            )

    async def _known(self, session_id: str, on_screen: str) -> bool:
        """Live in this backend, or one of the sessions the person's list shows.

        Asked of the session on screen, never of ``session_id`` itself:
        asking a session that does not exist would create it.
        """
        if session_id in self._model.backend.live_sessions():
            return True
        try:
            listed = await self._model.backend.rpc(
                RpcMethod.LIST_SESSIONS, {}, session_id=on_screen
            )
        except HostModelError:
            return False
        return session_id in self._ids(listed)

    @staticmethod
    def _ids(listed: Any) -> set[str]:
        sessions = listed.get("sessions", []) if isinstance(listed, dict) else listed
        return {str(s.get("session_id", "")) for s in sessions or [] if isinstance(s, dict)}


__all__ = ["SessionTarget", "SessionTargets"]
