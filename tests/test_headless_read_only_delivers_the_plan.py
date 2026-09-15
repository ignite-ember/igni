"""A headless plan-mode run prints the plan instead of swallowing it.

``--read-only`` selects ``PermissionMode.PLAN``. A denied edit tells the model
to "call ``exit_plan_mode(plan)`` when ready to execute", which writes the plan
to :class:`PlanStore`. The only consumer of that store is the frontend's plan
card: ``approve_plan`` is an FE operation and ``/plan`` is an interactive slash
command.

So a headless ``--read-only`` run produced a plan that nothing could read, and
then ended. From outside it looked like the agent had declined to do the work.
Worse, the advice in the denial message was unfollowable — there was no second
turn for an approval to unblock, because a single-message run is one-shot.

Printing is the right resolution rather than auto-approving. The user asked for
a read-only session; executing the plan because nobody was there to say no
would be the opposite of the flag.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from ember_code.core.session.single_message_run import SingleMessageRun
from ember_code.core.tools.plan.store import PlanStore


def _run_with(plan: str) -> list[str]:
    """Drive ``_emit_pending_plan`` against a session holding ``plan``."""
    printed: list[str] = []
    display = MagicMock()
    display.print_info.side_effect = printed.append

    run = SingleMessageRun.__new__(SingleMessageRun)
    store = PlanStore()
    if plan:
        store.set_plan(plan)
    run._session = SimpleNamespace(plan_store=store, display=display)

    run._emit_pending_plan()
    return printed


class TestThePlanReachesTheUser:
    def test_a_submitted_plan_is_printed(self):
        out = _run_with("1. Rename the thing\n2. Update callers")

        assert out, "the plan was produced and never shown"
        assert "Rename the thing" in out[0]
        assert "Update callers" in out[0]

    def test_the_output_says_nothing_ran(self):
        """The plan alone is ambiguous — it reads like a summary of work done."""
        out = _run_with("1. Rename the thing")

        assert "nothing was executed" in out[0]

    def test_it_says_how_to_carry_the_plan_out(self):
        out = _run_with("1. Rename the thing")

        assert "--read-only" in out[0]


class TestItStaysQuietOtherwise:
    def test_no_plan_prints_nothing(self):
        assert _run_with("") == []

    def test_a_session_without_a_plan_store_does_not_raise(self):
        """Bare-session test doubles exist throughout this suite; a run that
        crashes on a missing attribute would turn a reporting nicety into an
        outage."""
        run = SingleMessageRun.__new__(SingleMessageRun)
        run._session = SimpleNamespace(display=MagicMock())

        run._emit_pending_plan()
