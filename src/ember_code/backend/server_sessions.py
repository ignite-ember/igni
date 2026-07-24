"""Session management RPCs — list / switch / auto-name / search.

Owns :class:`SessionsController`, constructed with the underlying
:class:`Session` and a chat-history rebuilder callable. Every RPC
is a method on the class — the pre-refactor free-function shims
that took ``BackendServer`` as their first arg (Rule 6) have been
retired.

* :meth:`SessionsController.list_sessions` — enumerate all
  sessions for the project.
* :meth:`SessionsController.maybe_auto_name_session` — post-run
  auto-name pass, returning a typed :class:`AutoNameResult`.
* :meth:`SessionsController.switch_session` — flip the active
  session id + reload Agno's persisted history via
  :meth:`Session.rebind_identity`.
* :meth:`SessionsController.search_chat` — case-insensitive
  substring search across the persisted history.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ember_code.backend.chat_history_searcher import ChatHistorySearcher
from ember_code.backend.schemas_history import ChatHistoryEntry, ChatSearchHit
from ember_code.backend.schemas_sessions import AutoNameResult
from ember_code.protocol import messages as msg

if TYPE_CHECKING:
    from ember_code.core.session import Session

logger = logging.getLogger(__name__)


class SessionsController:
    """Session lifecycle RPCs for one :class:`Session`."""

    def __init__(
        self,
        session: Session,
        chat_history_provider: Callable[[str], Awaitable[list[ChatHistoryEntry] | list[dict]]],
    ) -> None:
        self._session = session
        # Injected as a callable so this class doesn't depend on
        # BackendServer (which owns the chat-history rebuilder).
        # The provider currently returns ``list[dict]`` at the wire
        # boundary (see :meth:`BackendServer.get_chat_history` for
        # the byte-identical-serialisation rationale). This class
        # normalises the shape to :class:`ChatHistoryEntry` before
        # handing it to :class:`ChatHistorySearcher`, which expects
        # a typed list — see :meth:`search_chat` for the model_validate
        # seam.
        self._chat_history_provider = chat_history_provider

    async def list_sessions(self) -> msg.SessionListResult:
        """List all sessions for this project. No cap — the FE has
        virtualisation."""
        raw = await self._session.persistence.list_sessions(limit=None)
        return msg.SessionListResult(sessions=raw)

    async def maybe_auto_name_session(self) -> AutoNameResult:
        """Auto-generate a name for the current session if it has none.

        Returns a typed :class:`AutoNameResult` — the dispatcher
        reads ``.name`` when ``.ok`` is true and forwards the FE
        push, matching the previous ``str | None`` semantics
        byte-for-byte.

        Sanitisation (stripping model-emitted markdown wrappers
        like ``**Title**`` / ``# Title`` / ``"Title"``) lives inside
        :meth:`SessionPersistence.auto_name` — callers of this
        method never see the raw wrapped form.
        """
        persistence = self._session.persistence
        try:
            existing = await persistence.get_name()
            if existing:
                return AutoNameResult(ok=False, name=existing, reason="already_named")
            name = await persistence.auto_name(self._session.main_team)
        except Exception as exc:
            logger.debug("session auto-name failed: %s", exc)
            return AutoNameResult(ok=False, name="", reason="error")
        if not name:
            return AutoNameResult(ok=False, name="", reason="no_name_produced")
        return AutoNameResult(ok=True, name=name, reason="generated")

    async def switch_session(self, session_id: str) -> msg.Info:
        """Switch to a different session.

        The four-attribute swap invariant (``session_id`` +
        ``session_named`` + ``main_team.session_id`` +
        ``persistence.session_id``) plus the ``aget_session``
        history reload live on :meth:`Session.rebind_identity` —
        this controller is a one-liner into that method.
        """
        await self._session.rebind_identity(session_id)
        name = await self._session.persistence.get_name()
        return msg.Info(text=f"Switched to session: {name or session_id}")

    async def search_chat(
        self, session_id: str, query: str, limit: int = 50
    ) -> list[ChatSearchHit]:
        """Case-insensitive substring search across the persisted
        history of ``session_id``.

        Emission order is defined by
        :meth:`ChatHistoryRebuilder.rebuild` — grep for that class
        when changing the parallel ``historyIndex -> itemIndex`` map
        on the FE side.
        """
        needle = (query or "").strip()
        if not needle:
            return []
        raw_history = await self._chat_history_provider(session_id)
        # Normalise at the RPC seam: ``get_chat_history`` returns
        # ``list[dict]`` (see the byte-identical-serialisation
        # rationale on :meth:`BackendServer.get_chat_history`), but
        # tests exercise this method with pre-typed
        # :class:`ChatHistoryEntry` lists too — accept both shapes
        # here so ChatHistorySearcher receives a uniform typed input.
        history: list[ChatHistoryEntry] = [
            entry if isinstance(entry, ChatHistoryEntry) else ChatHistoryEntry.model_validate(entry)
            for entry in raw_history
        ]
        return ChatHistorySearcher(history).search(needle, limit=limit)
