"""Context-breakdown reporter for the ``/ctx`` slash command.

Small collaborator that owns the token accounting used to
explain why ``/compact`` cannot shrink the context all the way
down. Extracted out of :class:`CompactionCoordinator` so the
coordinator stays focused on the compaction-lifecycle
concern.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agno.models.message import Message as AgnoMessage

from ember_code.core.session.schemas import ContextBreakdown

if TYPE_CHECKING:
    from ember_code.core.session.core import Session

logger = logging.getLogger(__name__)


class ContextBreakdownReporter:
    """Splits Agno's assembled message list into ``runs`` +
    ``floor`` token counts for the ``/ctx`` slash command.

    Composed by :class:`CompactionCoordinator`. The floor-
    clamp invariant (``floor = max(0, total - runs)``) lives on
    :meth:`ContextBreakdown.from_totals` — this reporter just
    supplies the two totals.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    async def report(self) -> ContextBreakdown:
        """Return per-component token counts for the current context.

        * ``runs``  — conversational turns (user / assistant / tool
          messages held under ``agno_session.runs``).
        * ``floor`` — everything else baked into every prompt: system
          instructions, tool schemas, project rules, active memories,
          injected session summary.
        * ``total`` — the full assembled-message figure
          (``total == runs + floor``).

        Returns a zero-filled breakdown on any I/O or tokenizer
        failure so the ``/ctx`` card renders a well-formed
        empty state instead of erroring out.
        """
        session = self._session
        try:
            agno_session = await session.main_team.aget_session(
                session_id=session.session_id,
                user_id=session.user_id,
            )
        except Exception:
            return ContextBreakdown.from_totals(total=0, runs=0)
        if agno_session is None:
            return ContextBreakdown.from_totals(total=0, runs=0)

        try:
            all_messages = agno_session.get_messages()
            total = int(session.main_team.model.count_tokens(all_messages))
        except Exception:
            total = 0

        run_messages: list[AgnoMessage] = []
        for run in getattr(agno_session, "runs", None) or []:
            msgs = getattr(run, "messages", None)
            if msgs:
                run_messages.extend(msgs)
        try:
            runs_tokens = (
                int(session.main_team.model.count_tokens(run_messages)) if run_messages else 0
            )
        except Exception:
            runs_tokens = 0

        return ContextBreakdown.from_totals(total=total, runs=runs_tokens)
