"""Pool-created sessions must receive broadcast callbacks too.

Bug: ``_create_runtime`` in ``backend/__main__.py`` used to spawn a
fresh BackendServer + Session for every session the pool was asked
to materialize, but never registered the broadcast callback that
turns ``session.broadcast(channel, payload)`` calls into outgoing
``PushNotification`` messages. The boot-time default session had
the wiring; every pool-created session was a silent black hole.

The visible symptom was the agent's ``exit_plan_mode(plan=..., tasks=...)``
producing a regular markdown reply instead of a dedicated PlanCard:
the BE-side broadcast happened, but with zero subscribers it never
reached the wire and the FE never saw ``plan_submitted``.

These tests pin that the broadcast→PushNotification translation
happens for both the boot path and a pool-equivalent path.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ember_code.protocol import messages as msg


class _CapturingTransport:
    """Records every outbound message."""

    def __init__(self) -> None:
        self.sent: list[Any] = []
        self.is_closed = False

    async def send(self, message: Any) -> None:
        self.sent.append(message)


class _StubSession:
    """Mimics the parts of Session that ``register_broadcast_callback``
    and ``broadcast`` touch, without booting all of Agno + persistence."""

    def __init__(self) -> None:
        self._broadcast_callbacks: list = []

    def register_broadcast_callback(self, cb) -> None:
        if cb not in self._broadcast_callbacks:
            self._broadcast_callbacks.append(cb)

    def broadcast(self, channel: str, payload: dict) -> None:
        for cb in list(self._broadcast_callbacks):
            try:
                cb(channel, payload)
            except Exception:
                # Mirror Session.broadcast's "one bad callback doesn't
                # sink the rest" semantic.
                continue


def _make_broadcast_callback_under_test(send_through: Any, loop: asyncio.AbstractEventLoop):
    """Mirror of ``__main__._make_broadcast_callback`` so the test can
    pin the wiring shape without importing the closure. If the real
    closure changes signature, this stays a single-file change."""

    def _on_event(channel: str, payload: dict) -> None:
        def _send() -> None:
            asyncio.ensure_future(
                send_through.send(msg.PushNotification(channel=channel, payload=payload))
            )

        loop.call_soon_threadsafe(_send)

    return _on_event


class TestBroadcastCallbackShape:
    async def test_plan_submitted_reaches_transport(self) -> None:
        # ``exit_plan_mode`` calls ``session.broadcast("plan_submitted", {...})``.
        # With the callback registered, a PushNotification must land
        # on the transport.
        loop = asyncio.get_running_loop()
        transport = _CapturingTransport()
        session = _StubSession()
        session.register_broadcast_callback(_make_broadcast_callback_under_test(transport, loop))

        session.broadcast(
            "plan_submitted",
            {"plan": "Add a comment.", "tasks": [{"content": "x", "status": "pending"}]},
        )
        # The callback hops onto the loop via ``call_soon_threadsafe``;
        # let the scheduler run the deferred send.
        await asyncio.sleep(0.01)

        assert len(transport.sent) == 1
        push = transport.sent[0]
        assert isinstance(push, msg.PushNotification)
        assert push.channel == "plan_submitted"
        assert push.payload["plan"] == "Add a comment."
        assert push.payload["tasks"][0]["content"] == "x"

    async def test_permission_mode_changed_uses_same_path(self) -> None:
        # Same wiring serves the badge updates — regression check.
        loop = asyncio.get_running_loop()
        transport = _CapturingTransport()
        session = _StubSession()
        session.register_broadcast_callback(_make_broadcast_callback_under_test(transport, loop))

        session.broadcast(
            "permission_mode_changed",
            {"mode": "plan", "previous": "default"},
        )
        await asyncio.sleep(0.01)

        assert len(transport.sent) == 1
        assert transport.sent[0].channel == "permission_mode_changed"

    async def test_zero_subscribers_drops_silently(self) -> None:
        # The pre-fix behavior: no callback registered → broadcast
        # iterates an empty list → silent. Tests the symptom we just
        # fixed, so it stays a regression.
        session = _StubSession()
        session.broadcast("plan_submitted", {"plan": "x", "tasks": []})  # must not raise
        assert session._broadcast_callbacks == []

    async def test_stamped_transport_carries_session_id(self) -> None:
        # The pool callback must use the SessionStampingTransport so
        # the PushNotification's session_id is set; the FE filters
        # views by session_id and would otherwise drop the push or
        # render it in the wrong view.
        from ember_code.backend.session_pool import SessionStampingTransport

        class _Backend:
            session_id = "sess-pool-7"

        loop = asyncio.get_running_loop()
        inner = _CapturingTransport()
        stamped = SessionStampingTransport(inner, _Backend())
        session = _StubSession()
        session.register_broadcast_callback(_make_broadcast_callback_under_test(stamped, loop))

        session.broadcast("plan_submitted", {"plan": "y", "tasks": []})
        await asyncio.sleep(0.01)

        assert len(inner.sent) == 1
        push = inner.sent[0]
        assert push.session_id == "sess-pool-7"


class TestPostRunDeferral:
    """``exit_plan_mode`` calls ``queue_post_run_broadcast`` so the
    PlanCard appears AFTER the agent's closing reply, not mid-stream
    above it. These tests pin the queue + drain contract."""

    def test_queue_holds_until_drain(self) -> None:
        from ember_code.core.session.core import Session

        session = Session.__new__(Session)
        session._broadcast_callbacks = []
        session._pending_post_run_broadcasts = []
        received: list[tuple[str, dict]] = []
        session._broadcast_callbacks.append(lambda c, p: received.append((c, p)))

        session.queue_post_run_broadcast("plan_submitted", {"plan": "x", "tasks": []})

        # Nothing fires until drain — that's the whole point.
        assert received == []
        assert len(session._pending_post_run_broadcasts) == 1

        session.drain_post_run_broadcasts()

        assert received == [("plan_submitted", {"plan": "x", "tasks": []})]
        # Queue empties so a later drain doesn't re-emit.
        assert session._pending_post_run_broadcasts == []

    def test_drain_with_empty_queue_is_noop(self) -> None:
        from ember_code.core.session.core import Session

        session = Session.__new__(Session)
        session._broadcast_callbacks = []
        session._pending_post_run_broadcasts = []
        received: list = []
        session._broadcast_callbacks.append(lambda c, p: received.append((c, p)))

        session.drain_post_run_broadcasts()
        assert received == []

    def test_fallback_to_immediate_when_queue_missing(self) -> None:
        # Tests / headless callers may build the session via __new__
        # without the queue list. The method must fall back to
        # immediate broadcast so the event isn't silently dropped.
        from ember_code.core.session.core import Session

        session = Session.__new__(Session)
        session._broadcast_callbacks = []
        # Deliberately do NOT set _pending_post_run_broadcasts.
        received: list = []
        session._broadcast_callbacks.append(lambda c, p: received.append((c, p)))

        session.queue_post_run_broadcast("plan_submitted", {"plan": "y", "tasks": []})

        assert received == [("plan_submitted", {"plan": "y", "tasks": []})]


class TestMainWiring:
    """Confirm ``__main__`` actually wires the callback in BOTH the
    boot path and ``_create_runtime``. The integration is asserted by
    introspecting the source — running the full ``_run`` requires a
    transport, a project dir, and an event loop, all of which would
    bloat this test without adding signal."""

    def test_helper_defined_and_called_for_both_paths(self) -> None:
        import inspect

        import ember_code.backend.__main__ as main_mod

        src = inspect.getsource(main_mod._run)
        # The factory exists.
        assert "_make_broadcast_callback" in src, (
            "_make_broadcast_callback helper not defined in _run; "
            "pooled sessions will silently miss broadcasts"
        )
        # And it's called at least twice — once for boot, once
        # inside _create_runtime for pool sessions.
        assert src.count("_make_broadcast_callback(") >= 3, (
            "_make_broadcast_callback should be defined + called for "
            "boot transport + pooled stamped transport"
        )
        # The pool-side call must use the stamped transport, not the
        # raw boot transport — otherwise the push lacks session_id.
        assert "_make_broadcast_callback(stamped)" in src, (
            "Pool callback must bind to the SessionStampingTransport"
        )
