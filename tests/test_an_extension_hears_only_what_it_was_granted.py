"""Signals, and the isolation rule that defines them.

The rule that shapes the whole design: **subscription is a grant, not
a filter.** The check happens where a signal is dispatched, never where
it is received. Anything that posted to every extension and asked each
to discard what was not its own would be trusting the extension to
discard it, and the extension is the untrusted party.

The four host events that predate the host object model are signals of
it now, granted by ``permissions.host`` like every other; their topic
strings are the signals' wire names (``docs/HOST_OBJECT_MODEL.md``,
*The existing events*).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from ember_code.extensions.events import HostTopic
from ember_code.extensions.host.definition import HOST_SCHEMA
from ember_code.extensions.host.wire import SignalMessage
from ember_code.extensions.registry import ExtensionState
from ember_code.protocol import messages as msg
from tests.host_model_helpers import HostRig, Sink

STATE = HostTopic.EXTENSION_STATE_CHANGED.value


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[HostRig]:
    rig = await HostRig(tmp_path).start()
    try:
        yield rig
    finally:
        await rig.stop()


def listening(rig: HostRig, extension_id: str, *signals: str) -> Sink:
    assert rig.model is not None
    sink = Sink()
    rig.model.bus.subscribe(rig.model.bus.attach(extension_id, sink), list(signals))
    return sink


# ── The topics are wire names, not a second vocabulary ─────────────


def test_every_old_topic_is_a_signal_of_the_model():
    for topic in HostTopic:
        assert HOST_SCHEMA.signal_by_wire(topic.value) is not None, topic


# ── Grants ─────────────────────────────────────────────────────────


async def test_an_exact_grant_is_heard(rig: HostRig):
    heard = listening(rig, rig.install("acme.one", host=["chat.read"]), "chat.run_started")
    await rig.transport.send(msg.RunStarted(session_id="boot", run_id="r1"))
    assert [v["run_id"] for v in heard.values("chat.run_started")] == ["r1"]


async def test_a_prefix_grant_covers_a_family(rig: HostRig):
    heard = listening(rig, rig.install("acme.one", host=["chat.*"]), "chat.run_started")
    await rig.transport.send(msg.RunStarted(session_id="boot", run_id="r1"))
    assert len(heard.values("chat.run_started")) == 1


async def test_what_was_not_granted_is_not_heard(rig: HostRig):
    heard = listening(rig, rig.install("acme.one", host=["sessions"]), "chat.run_started")
    await rig.transport.send(msg.RunStarted(session_id="boot", run_id="r1"))
    assert heard.got == []


async def test_two_extensions_do_not_overhear_each_other(rig: HostRig):
    """Each is told its own health; neither is told the other's
    without the `extensions` grant."""
    one, two = rig.install("acme.one"), rig.install("acme.two")
    heard_one = listening(rig, one, "self.state_changed", STATE)
    heard_two = listening(rig, two, "self.state_changed", STATE)
    rig.registry.set_state(one, ExtensionState.FAILED, "its service died")
    assert [v["extension_id"] for v in heard_one.values("self.state_changed")] == [one]
    assert heard_two.got == []
    assert heard_one.values(STATE) == []


async def test_only_the_audience_is_delivered_to(rig: HostRig):
    assert rig.model is not None
    one = rig.install("acme.one", host=["chat.read"])
    rig.install("acme.two", host=["chat.read"])
    rig.install("acme.three", host=[])
    listening(rig, one, "chat.run_started")
    listening(rig, "acme.three", "chat.run_started")
    told = rig.model.emit(
        "chat.run_started",
        {"session_id": "boot", "run_id": "r1", "parent_run_id": "", "agent_name": "", "model": ""},
    )
    # acme.two is entitled and not subscribed; acme.three is subscribed
    # and not entitled.
    assert told == [one]


async def test_one_failing_delivery_does_not_cost_the_others_a_signal(rig: HostRig):
    class Broken:
        def deliver(self, message: SignalMessage) -> None:
            raise RuntimeError("its process went away")

    assert rig.model is not None
    broken = rig.install("acme.broken", host=["chat.read"])
    rig.model.bus.subscribe(rig.model.bus.attach(broken, Broken()), ["chat.run_started"])
    heard = listening(rig, rig.install("acme.fine", host=["chat.read"]), "chat.run_started")
    await rig.transport.send(msg.RunStarted(session_id="boot", run_id="r1"))
    assert len(heard.values("chat.run_started")) == 1


async def test_an_uninstalled_extension_is_told_nothing(rig: HostRig):
    assert rig.model is not None
    gone = rig.install("acme.gone", host=["chat.read"])
    heard = listening(rig, gone, "chat.run_started")
    rig.registry.remove(gone)
    await rig.transport.send(msg.RunStarted(session_id="boot", run_id="r1"))
    assert heard.got == []


async def test_a_signal_about_one_extension_reaches_only_that_one(rig: HostRig):
    """`self.state_changed` is addressed: the grant was checked and the
    audience could still be wrong, which is the isolation rule failing
    in the one place it is easiest to miss."""
    assert rig.model is not None
    one, two = rig.install("acme.one"), rig.install("acme.two")
    heard_one = listening(rig, one, "self.state_changed")
    heard_two = listening(rig, two, "self.state_changed")
    change: dict[str, Any] = {"extension_id": one, "state": "READY", "reason": ""}
    told = rig.model.emit("self.state_changed", change, only=one)
    assert told == [one]
    assert heard_one.values("self.state_changed") == [change]
    assert heard_two.got == []
