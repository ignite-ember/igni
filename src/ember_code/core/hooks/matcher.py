"""Claude Code-compatible tri-mode hook matcher.

Behaviour:

* Empty pattern or ``"*"`` → always match.
* Alphanumeric identifier (with optional pipe-list, e.g.
  ``"Edit|Write"``) → EXACT match against the pipe-separated
  alternatives.
* Anything else → ``re.search`` (case-sensitive). Malformed
  regex is treated as "no match" rather than crashing the
  whole dispatch.

Encapsulated as a class so the compiled-regex constant lives
on the class (ClassVar), not as free module state, and matcher
state (mode, alternatives list, compiled pattern) is
pre-computed once in ``__init__`` instead of re-checked on
every ``matches`` call.
"""

from __future__ import annotations

import logging
import re
from typing import ClassVar

logger = logging.getLogger(__name__)


class HookMatcher:
    """Compiled matcher for a single hook's ``matcher`` field.

    Constructed once per hook definition — cheap enough for the
    executor to build on demand, but callers who match a lot
    (e.g. a hot event with many hooks) can cache the instance.
    """

    # Hook matcher patterns shaped like ``Edit`` or ``Edit|Write``
    # are interpreted as EXACT (or pipe-list-exact) matches —
    # Claude Code's convention. Anything outside this shape
    # (regex anchors, character classes, dots, etc.) is treated
    # as a regex.
    _EXACT_OR_PIPE_LIST_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"^[A-Za-z_][A-Za-z_0-9]*(?:\|[A-Za-z_][A-Za-z_0-9]*)*$"
    )

    def __init__(self, pattern: str):
        self._pattern = pattern
        self._always = not pattern or pattern == "*"
        self._alternatives: list[str] | None = None
        self._regex: re.Pattern[str] | None = None
        if self._always:
            return
        if self._EXACT_OR_PIPE_LIST_RE.match(pattern):
            self._alternatives = pattern.split("|")
            return
        try:
            self._regex = re.compile(pattern)
        except re.error:
            logger.debug("Malformed hook matcher %r — treating as no-match", pattern)
            self._regex = None

    @classmethod
    def always(cls) -> HookMatcher:
        """Factory for the always-match sentinel (empty pattern).

        Handy when a caller wants a matcher-shaped object without
        parsing a string — e.g. a synthetic hook injected at
        runtime with no user-facing matcher config.
        """
        return cls("")

    def matches(self, target: str) -> bool:
        """Return True if ``target`` matches this matcher."""
        if self._always:
            return True
        if self._alternatives is not None:
            return target in self._alternatives
        if self._regex is None:
            return False
        return self._regex.search(target) is not None
