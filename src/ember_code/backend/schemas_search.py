"""Wire schemas for the composer-paste ``search_code`` RPC.

Extracted from :mod:`ember_code.backend.server_search` to match
the sibling ``schemas_*.py`` convention used across ``backend/``
(``schemas_files``, ``schemas_panels``, ``schemas_context`` …).

Wire schemas (RPC-returned Pydantic models):

* :class:`SearchCodeMatch` — one hit row shipped to the FE.
* :class:`SearchCodeResult` — the wire envelope. Carries
  ``matches`` / ``truncated`` / ``error``, plus two behaviour
  methods that keep the truncation + timeout logic in one place:

    - :meth:`SearchCodeResult.timed_out` (classmethod) — the
      canonical timeout payload; used by
      :class:`RgSearchStrategy` when ``rg`` blows past the
      subprocess timeout.
    - :meth:`SearchCodeResult.append` — push a match and return
      ``True`` when the ``max_results`` cap is hit. Both search
      strategies use it so the "append then check the cap and
      flip ``truncated``" branch exists in exactly one place.
"""

from __future__ import annotations

from pydantic import BaseModel


class SearchCodeMatch(BaseModel):
    """One hit in :attr:`SearchCodeResult.matches`."""

    path: str
    line: int
    end_line: int
    preview: str


class SearchCodeResult(BaseModel):
    """Wire shape for :meth:`SearchController.search_code` —
    composer paste decoration. ``truncated`` is True whenever the
    max-results cap was hit (or a search timed out) so the FE can
    render "and N more…". ``error`` is empty on success paths."""

    matches: list[SearchCodeMatch] = []
    truncated: bool = False
    error: str = ""

    @classmethod
    def timed_out(cls) -> SearchCodeResult:
        """Canonical "search timed out" payload — used by strategies
        that can hit a hard timeout (currently
        :class:`RgSearchStrategy`) so the wire string lives in one
        place."""
        return cls(matches=[], truncated=True, error="search timed out")

    def append(self, match: SearchCodeMatch, max_results: int) -> bool:
        """Push ``match`` onto :attr:`matches` and return ``True``
        once ``max_results`` is reached.

        Also flips :attr:`truncated` when the cap is hit — the
        strategy loop should ``break`` (or ``return``) as soon as
        this returns True. Consolidates the append-then-check
        pattern that used to be duplicated in both
        :class:`RgSearchStrategy` and :class:`PythonWalkSearchStrategy`.
        """
        self.matches.append(match)
        if len(self.matches) >= max_results:
            self.truncated = True
            return True
        return False
