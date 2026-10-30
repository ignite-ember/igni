"""``PushChannel`` catalogues the channels that are actually produced.

The enum's docstring used to call itself "the single authoritative
list" of push channels. It was not: six channels reached the frontend
and were routed on there, while ``PushChannel(value)`` mapped every one
of them to ``UNKNOWN``. The enum is not a wire gate — ``channel`` is a
raw ``str`` on ``PushNotification`` precisely so an uncatalogued value
survives — so the cost was not dropped messages; it was that the one
place claiming to tell you what channels exist told you wrong, and a
reader adding a consumer had no way to find the other five.

The producer set is open by design (``BroadcastEvent.channel`` says so,
and ``push_bridge`` forwards raw strings), so this does not try to prove
the catalogue is complete. It pins the six that were missing, each next
to the module that emits it.
"""

from __future__ import annotations

import pytest

from ember_code.protocol.schemas.enums import PushChannel

#: channel wire string -> the module that emits it.
PRODUCED_CHANNELS = {
    "workflow_event": "backend/workflow_runner.py",
    "todos_updated": "core/tools/todo.py",
    "plan_decided": "core/session/plan_ops.py",
    "http_retry_attempt": "core/tools/http_fetcher.py",
    "http_retry_succeeded": "core/tools/http_fetcher.py",
    "visualization_action_dispatched": "backend/visualization_action_bus.py",
}


@pytest.mark.parametrize(("channel", "producer"), sorted(PRODUCED_CHANNELS.items()))
def test_a_produced_channel_is_in_the_catalogue(channel: str, producer: str):
    """``_missing_`` routes anything unlisted to ``UNKNOWN``, which is
    what made the gap invisible — the lookup never raised."""
    assert PushChannel(channel) is not PushChannel.UNKNOWN, (
        f"{channel!r} is emitted by {producer} but is not a PushChannel member"
    )
    assert PushChannel(channel).value == channel


def test_visualization_is_not_catalogued():
    """It had no producer.

    ``VisualizeTools.visualize`` stopped broadcasting when the
    visualizer moved to tool-arg streaming; the live path is a
    ``visualization_delta`` event on ``orchestrate_event``. The member
    and the ~50-line ``App.tsx`` branch that consumed it were both
    dead. Kept as a test so re-adding the member without a producer is
    a deliberate act.
    """
    assert not hasattr(PushChannel, "VISUALIZATION")
    assert PushChannel("visualization") is PushChannel.UNKNOWN
