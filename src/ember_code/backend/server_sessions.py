"""Session management RPCs — list / switch / auto-name / search.

Extracted from :mod:`ember_code.backend.server`. Four free
functions taking ``BackendServer`` as arg:

* :func:`list_sessions` — enumerate every session for the
  project (no cap — the FE virtualises).
* :func:`maybe_auto_name_session` — post-run auto-name pass
  with markdown-wrapper stripping for models that decorate
  the title.
* :func:`switch_session` — flip the active session id +
  reload chat history from Agno.
* :func:`search_chat` — case-insensitive substring search
  across the persisted history with FE-aligned
  ``history_index`` offsets.

Rule 2 clean — all imports at module top.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from ember_code.backend.server_helpers import _search_history
from ember_code.protocol import messages as msg

if TYPE_CHECKING:
    from ember_code.backend.server import BackendServer

logger = logging.getLogger(__name__)


# Titles from the auto-namer occasionally come back wrapped in
# markdown decoration (``**Title**`` / ``# Title`` / ``"Title"``);
# strip the leading/trailing runs before persisting so the
# session list doesn't read like a raw model response.
_TITLE_TRIM_RE = re.compile(r"^[\s*_`'\"#]+|[\s*_`'\"]+$")


async def list_sessions(backend: "BackendServer") -> msg.SessionListResult:
    """List all sessions for this project. No cap — the FE has
    virtualisation and a filter box; truncating server-side
    just hides work from the user without telling them."""
    raw = await backend._session.persistence.list_sessions(limit=None)
    return msg.SessionListResult(sessions=raw)


async def maybe_auto_name_session(backend: "BackendServer") -> str | None:
    """Auto-generate a name for the current session if it has none.

    Called after a run completes — Agno derives the name from
    the conversation so far. Returns the new name, or None
    when the session is already named (or naming failed).
    """
    try:
        if await backend._session.persistence.get_name():
            return None
        await backend._session.persistence.auto_name(backend._session.main_team)
        name = await backend._session.persistence.get_name() or ""
        # Models sometimes wrap the title in markdown
        # ("**Title**").
        clean = _TITLE_TRIM_RE.sub("", name)
        if clean and clean != name:
            await backend._session.persistence.rename(clean)
        return clean or None
    except Exception as exc:
        logger.debug("session auto-name failed: %s", exc)
        return None


async def switch_session(backend: "BackendServer", session_id: str) -> msg.Info:
    """Switch to a different session."""
    backend._session.session_id = session_id
    backend._session.session_named = True
    backend._session.main_team.session_id = session_id
    backend._session.persistence.session_id = session_id

    # Load history — aget_session triggers Agno to restore
    # conversation.
    agent = backend._session.main_team
    await agent.aget_session(
        session_id=session_id,
        user_id=backend._session.user_id,
    )
    name = await backend._session.persistence.get_name()
    return msg.Info(text=f"Switched to session: {name or session_id}")


async def search_chat(
    backend: "BackendServer",
    session_id: str,
    query: str,
    limit: int = 50,
) -> list[dict]:
    """Case-insensitive substring search across the persisted
    history of ``session_id``. Walks runs from the Agno SQLite
    session and emits matches with a ``history_index`` that
    lines up with ``get_chat_history``'s emission order — the
    FE keeps a parallel ``historyIndex -> itemIndex`` map built
    at session load so the result can be mapped straight to a
    chat item.

    Returns at most ``limit`` matches in chronological order:
      ``{history_index, role, run_id, snippet, match_start,
         match_end}``
    ``match_start``/``match_end`` are offsets within
    ``snippet`` (NOT the full content) so the FE can highlight
    without bookkeeping the original string.
    """
    needle = (query or "").strip()
    if not needle:
        return []
    history = await backend.get_chat_history(session_id)
    return _search_history(history, needle, limit)
