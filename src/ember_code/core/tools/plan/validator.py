"""Plan-confidence gate — enforce the "plans must be grounded"
property before :meth:`PlanTool.exit_plan_mode` records them.

The old ``_validate_plan_confidence(plan, tasks, session)`` free
function was the canonical Rule-6 offender in this codebase: it
mutated ``session._plan_mode_attempt`` and reached in for
``session._codeindex_available``. The behaviour graduated to
:class:`PlanConfidenceValidator`, which owns the attempt counter,
the regex, the thresholds, and the rejection-feedback prose as
first-class instance state.

Public thresholds — :data:`MIN_FILE_CITATIONS`,
:data:`MAX_PLAN_ATTEMPTS`, :data:`FILE_PATH_RE` — remain on the
module so tests can pin the tuning without instantiating the
validator; ``_MAX_PLAN_ATTEMPTS`` is re-exported at package top-
level for the historic import path (see ``__init__.py``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from ember_code.core.session.core import Session

logger = logging.getLogger(__name__)


# Validation thresholds. Tuned so a thin "trust me bro" plan
# bounces but a half-decent grounded plan goes through. The
# attempts cap exists to break the loop when the agent
# genuinely can't satisfy us — better to surface a thin plan to
# the user (who can refine it) than to ping-pong forever.
MIN_FILE_CITATIONS = 2
MAX_PLAN_ATTEMPTS = 3
# Regex catches typical file-path shapes: ``foo/bar.py``,
# ``src/x.ts``, ``backticked/code/files.md``. Generous on the
# extension list — we want to count file mentions in plan
# markdown across mixed-language repos.
FILE_PATH_RE = re.compile(
    r"[\w./_-]+\.(?:py|ts|tsx|js|jsx|md|rs|go|java|kt|swift|c|cc|cpp|h|hpp|"
    r"rb|php|cs|scala|css|scss|sql|sh|toml|yaml|yml|json|html|jinja)"
)


class ConfidenceVerdict(BaseModel):
    """Result of :meth:`PlanConfidenceValidator.validate`.

    On rejection, ``feedback`` is the string the plan tool sends
    back to the agent verbatim — it names what's missing and
    which tools to run next.
    """

    reject: bool
    feedback: str
    attempts_remaining: int


class PlanConfidenceValidator:
    """Owns the confidence-gate state for one :class:`Session`.

    Constructor takes the session so :meth:`validate` can read
    :attr:`Session.codeindex_available` (the public accessor —
    no reach-in on the private attr). The attempts counter is
    instance state; :meth:`reset` clears it, called by
    :meth:`PlanTool.enter_plan_mode` on fresh plan-mode entry.

    Compat: the counter is mirrored to
    ``session._plan_mode_attempt`` so existing tests that read /
    seed the attribute keep working. Writes from the validator
    are the source of truth; external writes are picked up on
    the next :meth:`validate` call.
    """

    def __init__(
        self,
        session: Session,
        *,
        min_citations: int = MIN_FILE_CITATIONS,
        max_attempts: int = MAX_PLAN_ATTEMPTS,
        file_path_re: re.Pattern[str] = FILE_PATH_RE,
    ) -> None:
        self._session = session
        self._min_citations = min_citations
        self._max_attempts = max_attempts
        self._pattern = file_path_re
        self._attempts = int(getattr(session, "_plan_mode_attempt", 0))

    # ── Counter accessors (mirrored to session for compat) ──────

    @property
    def attempts(self) -> int:
        """Number of rejections observed since the last
        :meth:`reset`. Read via the session compat mirror when
        available so external seeds take effect."""
        sess_val = getattr(self._session, "_plan_mode_attempt", None)
        if isinstance(sess_val, int):
            self._attempts = sess_val
        return self._attempts

    @attempts.setter
    def attempts(self, value: int) -> None:
        self._attempts = int(value)
        if hasattr(self._session, "_plan_mode_attempt"):
            self._session._plan_mode_attempt = self._attempts

    def reset(self) -> None:
        """Zero the counter — called on ``enter_plan_mode`` so
        each fresh plan-mode entry gets the full attempt
        budget."""
        self.attempts = 0

    # ── Gate ────────────────────────────────────────────────────

    def validate(self, plan: str, tasks: Iterable | None) -> ConfidenceVerdict:
        """Enforce the "plan must be grounded" property.

        Currently checks one signal: the plan markdown must
        mention at least :data:`MIN_FILE_CITATIONS` file paths.
        Plans without them are usually hand-wavy and produce
        execution-time surprises ("oh, I'll find the right file
        later"). The check runs only when the session reports a
        CodeIndex is available — without an index the agent
        legitimately can't do as deep a research pass, so we
        skip the gate.

        Bounded by :data:`MAX_PLAN_ATTEMPTS` — after the cap we
        accept whatever came in (the user still reviews + can
        refine).
        """
        # Public accessor — Session.codeindex_available is a real
        # method on the class. Bare object() test stubs may also
        # set the private attribute directly; support both so the
        # fake-session flow keeps working.
        if not self._session_codeindex_available():
            return ConfidenceVerdict(reject=False, feedback="", attempts_remaining=0)

        attempt = self.attempts
        remaining = max(0, self._max_attempts - attempt - 1)
        if attempt >= self._max_attempts - 1:
            # On the LAST allowed attempt we accept whatever came in
            # — surfacing a thin plan beats infinite loops. The
            # counter resets when the next ``enter_plan_mode`` fires.
            return ConfidenceVerdict(reject=False, feedback="", attempts_remaining=0)

        citations = set(self._pattern.findall(plan or ""))
        task_count = len(list(tasks or []))
        if len(citations) >= self._min_citations:
            return ConfidenceVerdict(reject=False, feedback="", attempts_remaining=remaining)

        # Bump the attempt counter so the next call sees a
        # different state. Writing through the property mirrors
        # onto ``session._plan_mode_attempt`` too.
        self.attempts = attempt + 1

        feedback = self.format_rejection(
            citations=len(citations),
            task_count=task_count,
            attempt=attempt,
        )
        return ConfidenceVerdict(reject=True, feedback=feedback, attempts_remaining=remaining)

    # ── Feedback template ───────────────────────────────────────

    def format_rejection(self, *, citations: int, task_count: int, attempt: int) -> str:
        """Render the rejection message shown to the agent.

        Kept as a method so the prose belongs to the validator's
        identity (rather than being an inline blob in
        :meth:`validate`). If the wording ever needs tuning per-
        session (e.g. codeindex-off variants), it happens here."""
        return (
            f"Plan rejected (research pass {attempt + 1}/{self._max_attempts}). "
            f"Found only {citations} file citation(s); need at least "
            f"{self._min_citations}. Your plan must reference specific files "
            "you actually examined — symbol-only descriptions don't ground "
            "the plan enough.\n\n"
            "Do another research pass:\n"
            "1. Run `codeindex_query` with at least 2 distinct angles you "
            "haven't tried yet (e.g. symbol names, related features, file "
            "patterns).\n"
            "2. For the top hits, call `codeindex_tree(id=<uuid>)` to see "
            "the reference graph — that finds the blast radius of any "
            "change.\n"
            "3. `file_read` one or two of the most central files to see "
            "exact source.\n"
            "4. Submit `exit_plan_mode(plan, tasks=[...])` again with "
            f"specific file paths cited. {task_count} task(s) currently."
        )

    # ── Internal helpers ────────────────────────────────────────

    def _session_codeindex_available(self) -> bool:
        """Read the CodeIndex-available flag from the session.

        Prefers the public :attr:`Session.codeindex_available`
        property; falls back to the private ``_codeindex_available``
        attribute for bare-Session test stubs that set the flag
        directly on ``__new__`` instances.
        """
        # Test stubs may set the private attribute directly on
        # bare ``Session.__new__`` instances — check that first
        # so the manual seed always wins.
        private = getattr(self._session, "_codeindex_available", None)
        if isinstance(private, bool):
            return private
        return bool(getattr(self._session, "codeindex_available", False))


__all__ = [
    "ConfidenceVerdict",
    "FILE_PATH_RE",
    "MAX_PLAN_ATTEMPTS",
    "MIN_FILE_CITATIONS",
    "PlanConfidenceValidator",
]
