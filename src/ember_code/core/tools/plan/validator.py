"""Plan-confidence gate — enforce the "plans must be grounded"
property before :meth:`PlanTool.exit_plan_mode` records them.

The old ``_validate_plan_confidence(plan, tasks, session)`` free
function was the canonical Rule-6 offender in this codebase: it
mutated ``session._plan_mode_attempt``. The behaviour graduated to
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

from ember_code.core.prompts.contributions import PromptContributions, prompt_contributions
from ember_code.core.prompts.document import PromptDocumentName

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

    The gate runs while an extension gives plan mode code
    intelligence (it fills the ``code_intelligence`` slot of the
    ``plan_mode`` prompt — CodeIndex does, while its index covers
    HEAD). The attempts counter is instance state; :meth:`reset` clears it, called by
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
        contributions: PromptContributions | None = None,
    ) -> None:
        self._session = session
        self._contributions = contributions if contributions is not None else prompt_contributions
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
        later"). The check runs only while plan mode has code
        intelligence — without an index the agent legitimately
        can't do as deep a research pass, so we skip the gate.

        Bounded by :data:`MAX_PLAN_ATTEMPTS` — after the cap we
        accept whatever came in (the user still reviews + can
        refine).
        """
        if not self.has_code_intelligence():
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
        :meth:`validate`)."""
        return (
            f"Plan rejected (research pass {attempt + 1}/{self._max_attempts}). "
            f"Found only {citations} file citation(s); need at least "
            f"{self._min_citations}. Your plan must reference specific files "
            "you actually examined — symbol-only descriptions don't ground "
            "the plan enough.\n\n"
            "Do another research pass:\n"
            "1. Ask the code index again, the way your plan-mode "
            "instructions describe — from several angles: by feature, by "
            "symbol, and by what depends on the central pieces.\n"
            # `file_read` is a permission category, not a function — PRD-12.
            # That fix reached the two plan *prompts* and missed the two plan
            # *tool results*, which is why 267 corpus tool results still carry
            # the retired name: the corpus was faithful and the product was not.
            '2. `run_shell_command "cat <path>"` on one or two of the most '
            "central files to see exact source.\n"
            "3. Submit `exit_plan_mode(plan, tasks=[...])` again with "
            f"specific file paths cited. {task_count} task(s) currently."
        )

    # ── Internal helpers ────────────────────────────────────────

    def has_code_intelligence(self) -> bool:
        """Whether an extension gives plan mode code intelligence right now."""
        return "code_intelligence" in self._contributions.slot_fills(PromptDocumentName.PLAN_MODE)


__all__ = [
    "ConfidenceVerdict",
    "FILE_PATH_RE",
    "MAX_PLAN_ATTEMPTS",
    "MIN_FILE_CITATIONS",
    "PlanConfidenceValidator",
]
