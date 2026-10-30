"""A scheduled task has to arrive looking like one.

``SchedulerController.execute`` was ``team.arun(description)``. The description
a user typed days earlier reached the model in exactly the shape of a message
typed a second ago, and the agent had no way to tell the difference — so the
reasonable-looking move, asking which of two branches they meant, produced a
question into an empty room. The run ends "successfully" having done nothing,
and the answer arrives never.

``/loop`` already solved this for its iterations and the message queue already
solved it for out-of-band notes. This file pins the third channel: what the
wrapper says, that it does not swallow the instruction it wraps, and that the
controller actually puts it on the prompt rather than only owning a function
that could.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ember_code.backend.server_scheduler import SchedulerController
from ember_code.core.scheduler.prompt import wrap_scheduled_prompt

_DESCRIPTION = "check the error rate on the payments dashboard and summarise it"


class TestTheWrapper:
    def test_the_description_survives_verbatim(self):
        """The framing is context, not a rewrite. If the instruction can be
        altered on its way through, the whole thing is a liability."""
        wrapped = wrap_scheduled_prompt(_DESCRIPTION)

        assert _DESCRIPTION in wrapped

    def test_it_says_there_is_no_one_to_ask(self):
        wrapped = wrap_scheduled_prompt(_DESCRIPTION).lower()

        assert "scheduled task" in wrapped
        assert "do not ask" in wrapped
        assert "nobody is watching" in wrapped

    def test_it_offers_the_alternative_to_asking(self):
        """ "Don't ask" on its own leaves an agent that is blocked with nothing
        to do. What it should do instead is the useful half."""
        wrapped = wrap_scheduled_prompt(_DESCRIPTION).lower()

        assert "assumption" in wrapped
        assert "report what stopped it" in wrapped

    def test_the_tag_uses_angle_brackets(self):
        """Not decoration. The loop wrapper's own comment records a
        square-bracket tag taking the TUI down with a MarkupError, because
        Textual reads ``[/loop ...]`` as a closing markup tag."""
        wrapped = wrap_scheduled_prompt(_DESCRIPTION)

        assert wrapped.startswith("<scheduled-run>")
        assert wrapped.endswith("</scheduled-run>")
        assert "[" not in wrapped.split(_DESCRIPTION)[0]

    def test_the_task_id_is_carried_when_known(self):
        wrapped = wrap_scheduled_prompt(_DESCRIPTION, task_id="task-42")

        assert wrapped.startswith('<scheduled-run task="task-42">')

    def test_no_attribute_when_the_caller_has_no_id(self):
        assert wrap_scheduled_prompt(_DESCRIPTION, task_id=None).startswith("<scheduled-run>")


class TestTheControllerUsesIt:
    @pytest.mark.asyncio
    async def test_execute_sends_the_wrapped_prompt(self):
        """The regression that matters. A wrapper nothing calls is the state
        this row was filed about, one level down."""
        sent: list[str] = []

        async def _arun(prompt, stream=False):  # noqa: ARG001
            sent.append(prompt)
            return "done"

        session = MagicMock()
        session.main_team.arun = _arun
        controller = SchedulerController(session, settings=None)

        await controller.execute(_DESCRIPTION)

        assert len(sent) == 1
        assert sent[0] != _DESCRIPTION, "the raw description reached the model unframed"
        assert sent[0] == wrap_scheduled_prompt(_DESCRIPTION)

    @pytest.mark.asyncio
    async def test_the_runners_default_callback_is_that_same_method(self):
        """The poller dispatches through ``execute_fn``, which defaults to
        ``execute``. If that binding is ever re-pointed at something rawer,
        the framing silently stops applying to the path that fires most."""
        session = MagicMock()
        controller = SchedulerController(session, settings=None)

        assert controller._execute_fn.__func__ is SchedulerController.execute
