"""Unit tests for ``session/broadcast.py``.

Broadcast machinery was extracted from Session in iter 141 and
turned into the :class:`BroadcastBus` class in the OOP pivot for
the ``session/`` package. Session's own tests cover the delegating
facade methods; these pins guard the class contract itself —
idempotent register, exception isolation between callbacks, and
the run_id stamping policy on drain.
"""

from unittest.mock import MagicMock

from ember_code.core.session.broadcast import BroadcastBus, BroadcastEvent


def _bus_with_callback(cb):
    """Fresh bus wired with a single callback — most tests want
    this shape and building it inline noise-up the reads."""
    bus = BroadcastBus()
    bus.register(cb)
    return bus


class TestRegister:
    def test_registers_new(self):
        bus = BroadcastBus()
        cb = MagicMock()
        bus.register(cb)
        assert bus.has_callbacks

    def test_idempotent(self):
        # Same callback registered twice should only appear once
        # so a reload doesn't fan out events N times.
        bus = BroadcastBus()
        cb = MagicMock()
        bus.register(cb)
        bus.register(cb)
        # Fan out through emit and confirm the callback fires
        # exactly once.
        bus.emit(BroadcastEvent(channel="c", payload={}))
        assert cb.call_count == 1


class TestEmit:
    def test_fires_all_callbacks(self):
        a, b = MagicMock(), MagicMock()
        bus = BroadcastBus()
        bus.register(a)
        bus.register(b)
        bus.emit(BroadcastEvent(channel="chan", payload={"k": 1}))
        a.assert_called_once_with("chan", {"k": 1})
        b.assert_called_once_with("chan", {"k": 1})

    def test_no_callbacks_is_noop(self):
        # Bare bus should not raise. Common during session
        # bootstrap before any transport has subscribed.
        bus = BroadcastBus()
        bus.emit(BroadcastEvent(channel="chan", payload={"k": 1}))

    def test_one_callback_exception_does_not_sink_others(self):
        # Headline safety property: a bad plugin can't stop
        # legitimate FE clients from receiving the same event.
        bad = MagicMock(side_effect=RuntimeError("boom"))
        good = MagicMock()
        bus = BroadcastBus()
        bus.register(bad)
        bus.register(good)
        bus.emit(BroadcastEvent(channel="chan", payload={"k": 1}))
        good.assert_called_once()


class TestQueuePostRun:
    def test_appends_to_queue(self):
        bus = BroadcastBus()
        bus.queue_post_run(BroadcastEvent(channel="plan_submitted", payload={"plan": "x"}))
        assert bus.pending_count == 1

    def test_queue_holds_until_drain(self):
        cb = MagicMock()
        bus = _bus_with_callback(cb)
        bus.queue_post_run(BroadcastEvent(channel="plan_submitted", payload={"plan": "x"}))
        # Nothing fires until drain — the whole point.
        cb.assert_not_called()
        bus.drain_post_run()
        cb.assert_called_once_with("plan_submitted", {"plan": "x"})


class TestDrainPostRun:
    def test_flushes_all(self):
        cb = MagicMock()
        bus = _bus_with_callback(cb)
        bus.queue_post_run(BroadcastEvent(channel="a", payload={"x": 1}))
        bus.queue_post_run(BroadcastEvent(channel="b", payload={"y": 2}))
        bus.drain_post_run()
        assert cb.call_count == 2
        assert bus.pending_count == 0

    def test_empty_queue_is_noop(self):
        cb = MagicMock()
        bus = _bus_with_callback(cb)
        bus.drain_post_run()
        cb.assert_not_called()

    def test_stamps_run_id_into_payload(self):
        # The plan-tool's payload doesn't include ``run_id`` because
        # the tool can't see it from inside its toolkit context;
        # drain injects it here via ``BroadcastEvent.with_run_id``.
        cb = MagicMock()
        bus = _bus_with_callback(cb)
        bus.queue_post_run(BroadcastEvent(channel="plan_submitted", payload={"plan": "x"}))
        bus.drain_post_run(run_id="run-42")
        cb.assert_called_once_with("plan_submitted", {"plan": "x", "run_id": "run-42"})

    def test_existing_run_id_preserved(self):
        # If the payload already carries a run_id, don't clobber it.
        cb = MagicMock()
        bus = _bus_with_callback(cb)
        bus.queue_post_run(BroadcastEvent(channel="evt", payload={"run_id": "original"}))
        bus.drain_post_run(run_id="different")
        cb.assert_called_once_with("evt", {"run_id": "original"})

    def test_snapshot_before_clear_prevents_reentry_loop(self):
        # If a callback re-queues during drain, that entry stays
        # in the queue for the NEXT drain — it doesn't loop here.
        bus = BroadcastBus()
        received: list[tuple[str, dict]] = []

        def _requeuer(channel, payload):
            received.append((channel, payload))
            bus.queue_post_run(BroadcastEvent(channel="re", payload={"y": 2}))

        bus.register(_requeuer)
        bus.queue_post_run(BroadcastEvent(channel="a", payload={"x": 1}))
        bus.drain_post_run()
        # First drain: original entry fired, re-queued one waits.
        assert received == [("a", {"x": 1})]
        assert bus.pending_count == 1
        # Second drain: the re-queued entry now fires.
        bus.drain_post_run()
        assert received[-1] == ("re", {"y": 2})


class TestBroadcastEvent:
    def test_with_run_id_stamps_when_missing(self):
        event = BroadcastEvent(channel="c", payload={"x": 1})
        stamped = event.with_run_id("R-1")
        assert stamped.payload == {"x": 1, "run_id": "R-1"}
        # Source event is not mutated.
        assert event.payload == {"x": 1}

    def test_with_run_id_preserves_existing(self):
        event = BroadcastEvent(channel="c", payload={"run_id": "keep"})
        assert event.with_run_id("override") is event

    def test_with_run_id_noop_on_empty(self):
        event = BroadcastEvent(channel="c", payload={"x": 1})
        assert event.with_run_id(None) is event
        assert event.with_run_id("") is event
