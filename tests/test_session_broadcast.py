"""Unit tests for ``session/broadcast.py``.

Broadcast machinery was extracted from Session in iter 141.
Session's own tests cover the delegation, but the free-function
API deserves its own pins so future refactors (e.g. adding a
priority queue for post-run broadcasts) can't silently regress
the defensive-against-partial-init contract or the
one-callback-can't-sink-the-rest guarantee.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from ember_code.core.session.broadcast import (
    broadcast,
    drain_post_run_broadcasts,
    queue_post_run_broadcast,
    register_broadcast_callback,
)


def _bare_session():
    """Session-shaped stub with only the fields broadcast.py reads."""
    return SimpleNamespace(_broadcast_callbacks=[], _pending_post_run_broadcasts=[])


class TestRegisterBroadcastCallback:
    def test_registers_new(self):
        s = _bare_session()
        cb = MagicMock()
        register_broadcast_callback(s, cb)
        assert cb in s._broadcast_callbacks

    def test_idempotent(self):
        # Same callback registered twice should only appear once
        # so a reload doesn't fan out events N times.
        s = _bare_session()
        cb = MagicMock()
        register_broadcast_callback(s, cb)
        register_broadcast_callback(s, cb)
        assert s._broadcast_callbacks.count(cb) == 1


class TestBroadcast:
    def test_fires_all_callbacks(self):
        s = _bare_session()
        a, b = MagicMock(), MagicMock()
        s._broadcast_callbacks = [a, b]
        broadcast(s, "chan", {"k": 1})
        a.assert_called_once_with("chan", {"k": 1})
        b.assert_called_once_with("chan", {"k": 1})

    def test_no_callbacks_is_noop(self):
        s = _bare_session()
        broadcast(s, "chan", {"k": 1})  # should not raise

    def test_missing_attr_is_noop(self):
        # Defensive path — the audit called out Session.__new__
        # bypass in tests as a real concern.
        s = SimpleNamespace()
        broadcast(s, "chan", {"k": 1})  # should not raise

    def test_one_callback_exception_does_not_sink_others(self):
        # Headline safety property: a bad plugin can't stop
        # legitimate FE clients from receiving the same event.
        s = _bare_session()
        bad = MagicMock(side_effect=RuntimeError("boom"))
        good = MagicMock()
        s._broadcast_callbacks = [bad, good]
        broadcast(s, "chan", {"k": 1})
        good.assert_called_once()


class TestQueuePostRunBroadcast:
    def test_appends_to_queue(self):
        s = _bare_session()
        queue_post_run_broadcast(s, "plan_submitted", {"plan": "x"})
        assert s._pending_post_run_broadcasts == [("plan_submitted", {"plan": "x"})]

    def test_falls_back_to_immediate_when_queue_missing(self):
        # Session.__new__ path — no queue attr initialised.
        s = SimpleNamespace(_broadcast_callbacks=[])
        cb = MagicMock()
        s._broadcast_callbacks = [cb]
        queue_post_run_broadcast(s, "plan_submitted", {"plan": "x"})
        # Fell back to immediate broadcast.
        cb.assert_called_once_with("plan_submitted", {"plan": "x"})


class TestDrainPostRunBroadcasts:
    def test_flushes_all(self):
        s = _bare_session()
        cb = MagicMock()
        s._broadcast_callbacks = [cb]
        s._pending_post_run_broadcasts = [("a", {"x": 1}), ("b", {"y": 2})]
        drain_post_run_broadcasts(s)
        assert cb.call_count == 2
        assert s._pending_post_run_broadcasts == []

    def test_empty_queue_is_noop(self):
        s = _bare_session()
        cb = MagicMock()
        s._broadcast_callbacks = [cb]
        drain_post_run_broadcasts(s)
        cb.assert_not_called()

    def test_stamps_run_id_into_payload(self):
        # The plan-tool's payload doesn't include ``run_id`` because
        # the tool can't see it from inside its toolkit context;
        # drain injects it here.
        s = _bare_session()
        cb = MagicMock()
        s._broadcast_callbacks = [cb]
        s._pending_post_run_broadcasts = [("plan_submitted", {"plan": "x"})]
        drain_post_run_broadcasts(s, run_id="run-42")
        cb.assert_called_once_with("plan_submitted", {"plan": "x", "run_id": "run-42"})

    def test_existing_run_id_preserved(self):
        # If the payload already carries a run_id, don't clobber it.
        s = _bare_session()
        cb = MagicMock()
        s._broadcast_callbacks = [cb]
        s._pending_post_run_broadcasts = [("evt", {"run_id": "original"})]
        drain_post_run_broadcasts(s, run_id="different")
        cb.assert_called_once_with("evt", {"run_id": "original"})

    def test_snapshot_before_clear_prevents_reentry_loop(self):
        # If a callback re-queues during drain, that entry stays
        # in the queue for the NEXT drain — it doesn't loop here.
        s = _bare_session()
        s._pending_post_run_broadcasts = [("a", {"x": 1})]

        def _requeuer(channel, payload):
            s._pending_post_run_broadcasts.append(("re", {"y": 2}))

        s._broadcast_callbacks = [_requeuer]
        drain_post_run_broadcasts(s)
        # The re-queued entry survived to the next drain.
        assert s._pending_post_run_broadcasts == [("re", {"y": 2})]
