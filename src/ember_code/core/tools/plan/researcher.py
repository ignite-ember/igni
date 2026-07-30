"""``plan_researcher`` sub-agent spawn — extracted from
:class:`PlanTool` so the reach-in on ``session.main_team.tools``
lives on a dedicated class instead of the tool.

The runner is stateful across a single session:

* Caches the :class:`OrchestrateTools` discovery so subsequent
  spawns don't re-scan ``main_team.tools`` on every call.
* Owns the narrow catch of spawn failures — the underlying
  :meth:`OrchestrateTools.spawn_agent` can raise a variety of
  errors (missing agent definition, model init failure, network
  timeouts), and we deliberately keep planning tolerant: a
  spawn failure falls back to the "manual research" path in
  :meth:`PlanTool.enter_plan_mode`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ember_code.core.tools.orchestrate import OrchestrateTools

if TYPE_CHECKING:
    from ember_code.core.session.core import Session

logger = logging.getLogger(__name__)


class PlanResearcherRunner:
    """Spawns the ``plan_researcher`` sub-agent for one session.

    Instance state:

    * ``_session`` — the parent session we spawn under.
    * ``_orchestrate`` — cached reference to the session's
      :class:`OrchestrateTools`. Populated lazily on the first
      :meth:`run` call; if the team is rebuilt (compact / plugin
      reload), :meth:`invalidate` clears the cache.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._orchestrate: OrchestrateTools | None = None

    def invalidate(self) -> None:
        """Drop the cached :class:`OrchestrateTools` reference —
        called after a main-team rebuild would swap out the
        instance under us."""
        self._orchestrate = None

    async def run(self, task: str) -> str:
        """Spawn the ``plan_researcher`` on ``task`` and return
        its response text.

        Returns ``""`` on any recoverable failure (no
        OrchestrateTools available, agent not registered, spawn
        raised) so :meth:`PlanTool.enter_plan_mode` can fall
        back to the manual-research path. Cancellation is
        re-raised so the surrounding run loop can honour
        cooperative shutdown.
        """
        orchestrate = self._resolve_orchestrate()
        if orchestrate is None:
            logger.debug("plan_researcher spawn skipped — no OrchestrateTools on session")
            return ""

        # Check the agent is registered. The pool's
        # ``_codeindex_available`` flag picks the right variant
        # (``plan_researcher.codeindex.md`` vs ``plan_researcher.md``)
        # — both register under the same canonical name.
        try:
            orchestrate.pool.get("plan_researcher")
        except KeyError:
            logger.debug("plan_researcher agent definition not found in pool")
            return ""

        try:
            result = await orchestrate.spawn_agent(task=task, agent_name="plan_researcher")
        except asyncio.CancelledError:
            # Never swallow cancellation — the run-loop honours
            # cooperative shutdown, so a CancelledError here must
            # propagate.
            raise
        except (KeyError, LookupError, ValueError, RuntimeError) as exc:
            # Narrow catch of the documented spawn failure modes:
            # KeyError / LookupError from a pool miss race, ValueError
            # from bad task shape, RuntimeError from Agno's inner
            # spawn path when the model init trips.
            logger.warning("plan_researcher spawn failed: %s", exc)
            return ""
        return result if isinstance(result, str) else str(result)

    # ── Internal helpers ────────────────────────────────────────

    def _resolve_orchestrate(self) -> OrchestrateTools | None:
        """Discover the session's :class:`OrchestrateTools` and
        cache it. Rescans on cache miss so a late-arriving team
        (e.g. built after the runner was constructed) is picked
        up on the next call."""
        if self._orchestrate is not None:
            return self._orchestrate
        team = getattr(self._session, "main_team", None)
        team_tools = getattr(team, "tools", None) or []
        for tool in team_tools:
            if isinstance(tool, OrchestrateTools):
                self._orchestrate = tool
                return tool
        return None


__all__ = ["PlanResearcherRunner"]
