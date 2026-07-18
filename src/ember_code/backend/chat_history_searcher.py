"""Substring-scan over a persisted chat-history list — powers the
``search_chat`` RPC.

Owns the behavior formerly split between
``server_helpers._search_history`` (the scan) and
``server_helpers._turn_get`` (a dict|entry shape-tolerance shim). By
normalising the input to :class:`ChatHistoryEntry` at construction,
:class:`ChatHistorySearcher` retires ``_turn_get`` entirely — the
Pydantic model exposes attribute access on every field the scan
touches, so there's no per-field branching.

The snippet half-width (formerly ``_SEARCH_CHAT_SNIPPET_HALF_WIDTH``)
moves from module-level constant to a constructor argument so scan
tuning is instance state, not module state.
"""

from __future__ import annotations

from ember_code.backend.schemas_history import ChatHistoryEntry, ChatSearchHit


class ChatHistorySearcher:
    """Case-insensitive substring scan over a typed chat-history list.

    :param history: Typed history from
        :meth:`ChatHistoryRebuilder.rebuild` (or its dict-shaped
        wire form re-validated at the RPC seam via
        :meth:`ChatHistoryEntry.model_validate`).
    :param snippet_half_width: Characters on either side of the match
        included in the emitted snippet. Generous enough for the user
        to see context but tight enough to keep the search-results
        dropdown skimmable.
    """

    def __init__(
        self,
        history: list[ChatHistoryEntry],
        snippet_half_width: int = 80,
    ) -> None:
        self._history = history
        self._snippet_half_width = snippet_half_width

    def search(self, needle: str, *, limit: int) -> list[ChatSearchHit]:
        """Scan the history and emit at most ``limit``
        :class:`ChatSearchHit` rows (one per matching turn).

        Empty needles short-circuit — ``str.find("")`` returns 0 for
        every string and would flood the results with matches for
        every turn.
        """
        needle_lower = needle.lower()
        needle_len = len(needle)
        if needle_len == 0:
            return []
        matches: list[ChatSearchHit] = []
        for idx, turn in enumerate(self._history):
            hit = self._scan_turn(idx, turn, needle_lower, needle_len)
            if hit is None:
                continue
            matches.append(hit)
            if len(matches) >= limit:
                break
        return matches

    # ── Private per-turn logic ─────────────────────────────────────

    def _scan_turn(
        self,
        idx: int,
        turn: ChatHistoryEntry,
        needle_lower: str,
        needle_len: int,
    ) -> ChatSearchHit | None:
        """Return a :class:`ChatSearchHit` for one turn, or ``None``
        when the turn has no textual content or no match."""
        content = turn.content
        if not isinstance(content, str) or not content:
            return None
        pos = content.lower().find(needle_lower)
        if pos < 0:
            return None
        snippet, match_start = self._build_snippet(content, pos, needle_len)
        return ChatSearchHit(
            history_index=idx,
            role=str(turn.role or ""),
            run_id=str(turn.run_id or ""),
            snippet=snippet,
            match_start=match_start,
            match_end=match_start + needle_len,
            created_at=int(turn.created_at or 0),
        )

    def _build_snippet(self, content: str, pos: int, needle_len: int) -> tuple[str, int]:
        """Slice a context snippet around ``pos`` and return
        ``(snippet, match_start_in_snippet)``.

        The FE highlights by slicing ``snippet[match_start:match_end]``
        so ``match_start`` must be relative to the snippet — the
        leading ellipsis (when present) shifts the match forward by
        one character.
        """
        start = max(0, pos - self._snippet_half_width)
        end = min(len(content), pos + needle_len + self._snippet_half_width)
        raw = content[start:end]
        leading = "…" if start > 0 else ""
        trailing = "…" if end < len(content) else ""
        snippet = f"{leading}{raw}{trailing}"
        match_start = (pos - start) + len(leading)
        return snippet, match_start
