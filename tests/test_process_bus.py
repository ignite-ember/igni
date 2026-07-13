"""Tests for :class:`ProcessEventBus` — the consolidated pub/sub API
that replaces shell.py's three parallel subscriber lists.

Pure module → all invariants testable without spinning up any actual
process. Locks in the interface contract that shell.py depends on so
a future edit can't silently regress fan-out semantics.
"""

from __future__ import annotations

import pytest

from ember_code.core.tools.process_bus import ProcessEventBus


class TestOnOff:
    def test_subscribe_appears_in_count(self):
        bus = ProcessEventBus()
        received: list[dict] = []
        bus.on("start", received.append)
        assert bus.subscriber_count("start") == 1

    def test_subscribe_is_idempotent(self):
        bus = ProcessEventBus()

        def cb(_p: dict) -> None:
            pass

        bus.on("line", cb)
        bus.on("line", cb)
        bus.on("line", cb)
        # Same callback registered three times → still ONE.
        assert bus.subscriber_count("line") == 1

    def test_off_removes(self):
        bus = ProcessEventBus()

        def cb(_p: dict) -> None:
            pass

        bus.on("exit", cb)
        bus.off("exit", cb)
        assert bus.subscriber_count("exit") == 0

    def test_off_unregistered_is_noop(self):
        bus = ProcessEventBus()

        def cb(_p: dict) -> None:
            pass

        # Not registered → shouldn't raise.
        bus.off("line", cb)
        assert bus.subscriber_count("line") == 0

    def test_unknown_event_on_raises(self):
        bus = ProcessEventBus()
        with pytest.raises(ValueError):
            bus.on("nope", lambda _p: None)  # type: ignore[arg-type]

    def test_unknown_event_off_is_noop(self):
        # Off is more lenient than on — mistyped event names during
        # teardown shouldn't crash the tear-down path.
        bus = ProcessEventBus()
        bus.off("nope", lambda _p: None)  # type: ignore[arg-type]


class TestEmit:
    def test_emit_fires_subscriber(self):
        bus = ProcessEventBus()
        received: list[dict] = []
        bus.on("start", received.append)
        bus.emit("start", {"pid": 42, "cmd": "ls"})
        assert received == [{"pid": 42, "cmd": "ls"}]

    def test_emit_fires_multiple_subscribers_in_order(self):
        # In-order emission matters when subscribers have layered
        # semantics (e.g. persistence before FE push).
        bus = ProcessEventBus()
        order: list[str] = []
        bus.on("start", lambda _p: order.append("a"))
        bus.on("start", lambda _p: order.append("b"))
        bus.on("start", lambda _p: order.append("c"))
        bus.emit("start", {})
        assert order == ["a", "b", "c"]

    def test_emit_with_no_subscribers_is_noop(self):
        bus = ProcessEventBus()
        bus.emit("line", {"pid": 1, "line": "hi"})  # no crash

    def test_emit_unknown_event_is_silent(self):
        bus = ProcessEventBus()
        bus.emit("nope", {})  # type: ignore[arg-type]

    def test_subscriber_exception_doesnt_sink_others(self):
        """Fail-soft is the invariant shell.py relies on — one flaky
        FE push mustn't take down the persistence subscriber next to
        it."""
        bus = ProcessEventBus()
        good: list[dict] = []

        def bad(_p: dict) -> None:
            raise RuntimeError("boom")

        bus.on("exit", bad)
        bus.on("exit", good.append)
        # Should NOT raise.
        bus.emit("exit", {"pid": 1})
        # The good subscriber still fired even though ``bad`` blew up.
        assert good == [{"pid": 1}]

    def test_events_are_isolated(self):
        # A subscriber on "start" doesn't see "line" events, and vice
        # versa. Prevents shell.py's original design where every FE
        # concern had to filter irrelevant payloads.
        bus = ProcessEventBus()
        start_evts: list[dict] = []
        line_evts: list[dict] = []
        bus.on("start", start_evts.append)
        bus.on("line", line_evts.append)
        bus.emit("start", {"pid": 1})
        bus.emit("line", {"pid": 1, "line": "x"})
        assert start_evts == [{"pid": 1}]
        assert line_evts == [{"pid": 1, "line": "x"}]


class TestReset:
    def test_reset_drops_all_subscribers(self):
        bus = ProcessEventBus()
        bus.on("start", lambda _p: None)
        bus.on("line", lambda _p: None)
        bus.on("exit", lambda _p: None)
        bus.reset()
        assert bus.subscriber_count("start") == 0
        assert bus.subscriber_count("line") == 0
        assert bus.subscriber_count("exit") == 0

    def test_reset_survives_reuse(self):
        # After reset the bus is still usable — reset is a state
        # clear, not a shutdown.
        bus = ProcessEventBus()
        received: list[dict] = []
        bus.on("line", received.append)
        bus.reset()
        bus.on("line", received.append)
        bus.emit("line", {"pid": 1, "line": "x"})
        assert received == [{"pid": 1, "line": "x"}]
