"""Plan-mode agent tools — ``enter_plan_mode`` and ``exit_plan_mode``.

The two agent-facing tool methods live here. Everything that
sequences the underlying enter / submit transactions
(permission-mode flip, plan_store write, todo_store populate,
broadcast queue, researcher spawn, confidence-gate) lives on
:class:`PlanTransactionCoordinator` in ``submission.py``. This
module is now a thin Toolkit adapter:

* :meth:`PlanTool.enter_plan_mode` — delegates to
  :meth:`PlanTransactionCoordinator.enter` and returns
  ``result.reply_text``.
* :meth:`PlanTool.exit_plan_mode` — delegates to
  :meth:`PlanTransactionCoordinator.submit` and returns
  ``result.reply_text``.

The long agent-facing docstrings on both methods are preserved
verbatim — they steer the model's tool selection, so relocating
them silently would degrade behaviour.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agno.tools import Toolkit

from ember_code.core.tools.plan.submission import PlanTransactionCoordinator

if TYPE_CHECKING:
    from ember_code.core.session.core import Session

logger = logging.getLogger(__name__)


class PlanTool(Toolkit):
    """Registers ``enter_plan_mode`` + ``exit_plan_mode`` on the
    agent's tool list.

    Asymmetric security envelope:

    * ``enter_plan_mode`` flips mode → ``plan``. Safe to expose to
      the agent — moving INTO the sandbox is strictly stricter
      (read-only). The agent self-disciplines for complex tasks.
    * ``exit_plan_mode`` records a plan but does NOT touch the
      mode. Exit is user-controlled (``/plan off`` or the Approve
      button) so the agent can't unsandbox itself.

    :class:`PlanTool` is a thin Toolkit adapter: both methods
    delegate to the internal :class:`PlanTransactionCoordinator`,
    which owns the multi-step sequencing (validator + researcher
    composition, permission-evaluator flip, broadcast queueing,
    plan_store / todo_store writes). The tool renders the agent-
    visible reply string from the coordinator's typed envelope at
    a single site per method.
    """

    def __init__(self, session: Session) -> None:
        super().__init__(name="ember_plan")
        self._session = session
        self._coordinator = PlanTransactionCoordinator(session)
        self.register(self.enter_plan_mode)
        self.register(self.exit_plan_mode)

    async def enter_plan_mode(self, reason: str = "", task: str = "") -> str:
        """Enter plan mode AND spawn the plan_researcher sub-agent.

        Call this BEFORE doing any work when the user asks for
        something that benefits from a written plan:

        * Multi-file refactors / architectural changes
        * "Add feature X" requests that span several modules
        * Investigations where the right path isn't obvious
        * Anything where committing to a direction without
          checking with the user first would be expensive to
          undo

        Don't call this for simple, one-shot requests
        (a small bug fix, a single-file edit, an obvious tweak).

        Args:
            reason: Short string the UI shows next to the plan-mode
                badge ("auth refactor spans 4 services"). Helps
                the user understand WHY you switched modes.
            task: The user's original request, verbatim or
                paraphrased. Passed to the spawned
                ``plan_researcher`` sub-agent so it knows what
                to research. When provided, this method
                automatically spawns the researcher and returns
                its findings — you don't need a separate
                ``spawn_agent`` call. Omit only for very short
                "I want to plan this manually" turns.

        Behavior:
        1. Flips the permission evaluator to ``plan`` (blocks
           file edits + mutating shell).
        2. Resets the plan-mode attempt counter.
        3. If ``task`` is provided, spawns the
           ``plan_researcher`` sub-agent with the task. The
           researcher does multi-angle CodeIndex queries (or
           grep fallback), reads the critical files, and
           returns a structured report (Findings / Proposed
           Plan / Tasks JSON / Confidence / Open Questions).
        4. Returns the researcher's report so you can use it
           verbatim or refine it before calling
           ``exit_plan_mode(plan, tasks)``.

        The validation hook in ``exit_plan_mode`` may reject
        plans that aren't grounded in concrete codebase facts —
        if so, the rejection message tells you exactly what's
        missing, do another research pass, then submit again.
        Bounded by 3 attempts.
        """
        result = await self._coordinator.enter(reason=reason, task=task)
        return result.reply_text

    def exit_plan_mode(self, plan: str, tasks: list | None = None) -> str:
        """Submit a plan for the user's review.

        Call this at the END of a plan-mode turn after you've
        finished gathering context and have a concrete proposal
        for what to do next.

        Args:
            plan: Markdown-formatted plan describing the steps
                you intend to take. This is the prose the user
                reads when deciding whether to approve.
            tasks: Optional list of structured tasks — one entry
                per execution step. Each entry is a dict with
                ``content`` (required, the imperative step
                description) and optional ``activeForm`` (the
                verb-noun gerund shown while in progress). The
                tool populates the session's ``TodoStore`` with
                these so the user sees a checklist alongside
                the prose plan, AND the same store updates live
                as you call ``todo_write`` during execution.
                Pass tasks unless the plan is genuinely
                unstructured (a freeform proposal where steps
                aren't enumerable).

        Example call::

            exit_plan_mode(
                plan="## JWT refactor\\n\\nMove from session cookies to JWT...",
                tasks=[
                    {"content": "Generate JWT signing keys",
                     "activeForm": "Generating JWT signing keys"},
                    {"content": "Add /auth/refresh endpoint",
                     "activeForm": "Adding /auth/refresh endpoint"},
                    {"content": "Migrate session table",
                     "activeForm": "Migrating session table"},
                ],
            )

        Plan-mode etiquette:

        * Only call this when you're in plan mode (the user
          enabled it via ``/plan`` or you called
          ``enter_plan_mode``). Calling it outside plan mode
          still records the plan, but it's noise — nobody asked
          for one.
        * Stop after calling this tool. Do NOT continue
          executing steps in the same turn — the whole point of
          plan mode is that the user reviews before execution.
          Wait for their next message.

        Returns a confirmation. The plan + tasks are stored on
        the session for the UI to render and the user can
        ``/plan`` (or click Approve) to exit plan mode and let
        you execute.
        """
        result = self._coordinator.submit(plan=plan, tasks=tasks)
        return result.reply_text


__all__ = ["PlanTool"]
