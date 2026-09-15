"""A finished scheduled task actually produces a push message.

The runner reported completion as ``(task_id, description, True)``. The consumer
that publishes it is::

    def _on_scheduler_completed(self, task_id, description, result: str)
        payload = SchedulerCompletedPayload(..., result=result)

and ``SchedulerCompletedPayload.result`` is typed ``str``. So the success flag
landed in the result slot, pydantic refused it, and every scheduled task
completion raised ``ValidationError`` inside the callback. The push was never
sent — a scheduled task ran, finished, wrote its result to the store, and the
client was never told.

Nothing failed loudly. The runner calls the callback without a try, but the
exception surfaces in a fire-and-forget context, and the task's stored status
was already ``completed`` by then, so every other signal said it worked.

Two things are pinned here, because the type mismatch was only half of it:

* The payload constructs. That alone catches the reintroduction.
* ``result`` carries the task's actual output. The runner had the text —
  ``result = await self._execute_fn(...)`` — and discarded it in favour of a
  boolean, so even a "fix" that passed ``str(success)`` would satisfy the type
  and still tell the user nothing.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from ember_code.backend.schemas_push import SchedulerCompletedPayload


@pytest.fixture
def bridge():
    """A push bridge with its transport stubbed.

    Only ``_on_scheduler_completed`` is exercised; the transport is where the
    message would go and is not what is under test.
    """
    from ember_code.backend.push_bridge import PushNotificationBridge

    made = PushNotificationBridge.__new__(PushNotificationBridge)
    made._transport = MagicMock()
    return made


def _sent_payload(bridge) -> dict:
    """The payload handed to the transport, dug out of the sent message.

    ``PushNotification.payload`` is a plain dict by the time it is on the wire —
    which is the level worth asserting at, since that is what the client parses.
    """
    sent = bridge._transport.send.call_args
    assert sent is not None, "no message was sent at all"
    message = sent.args[0]
    assert message.channel == "scheduler_completed", message.channel
    return message.payload


class TestTheCompletionPushIsSendable:
    def test_a_successful_task_publishes_its_output(self, bridge, monkeypatch):
        monkeypatch.setattr(
            "ember_code.backend.push_bridge.asyncio.ensure_future", lambda coro: coro
        )

        bridge._on_scheduler_completed("t1", "nightly report", True, "42 rows written")

        payload = _sent_payload(bridge)
        assert payload["result"] == "42 rows written"
        assert payload["task_id"] == "t1"

    def test_a_failed_task_publishes_its_error(self, bridge, monkeypatch):
        monkeypatch.setattr(
            "ember_code.backend.push_bridge.asyncio.ensure_future", lambda coro: coro
        )

        bridge._on_scheduler_completed("t2", "nightly report", False, "Task timed out after 300s")

        assert _sent_payload(bridge)["result"] == "Task timed out after 300s"

    def test_the_flag_alone_still_produces_a_valid_message(self, bridge, monkeypatch):
        """A caller that omits the detail must not resurrect the crash.

        ``detail`` defaults to empty, and an empty string is a legal ``str``, so
        the guard here is that the fallback says something rather than sending a
        blank result the client cannot interpret.
        """
        monkeypatch.setattr(
            "ember_code.backend.push_bridge.asyncio.ensure_future", lambda coro: coro
        )

        bridge._on_scheduler_completed("t3", "nightly report", False)

        assert _sent_payload(bridge)["result"] == "failed"


def test_the_payload_still_refuses_a_bare_flag():
    """The type that caught this is worth keeping strict.

    If ``result`` ever gains a coercing type, the original bug becomes
    expressible again and silently ships a message reading ``result='True'``.
    """
    with pytest.raises(ValidationError):
        SchedulerCompletedPayload(task_id="t", description="d", result=True)
