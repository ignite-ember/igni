"""Tests for the multi-requirement HITL resolution path.

Reported on v0.5.11: an 8-call parallel tool plan would only succeed
on the first call; the other 7 came back as ``"User denied"`` even
though the user never saw a reject dialog. Root cause was in
``BackendServer.resolve_hitl``, which called
``acontinue_run(requirements=[req])`` with only the one just-resolved
requirement — Agno treats requirements absent from the list as
denied, so the other 7 from the same pause silently failed.

``resolve_hitl_batch`` ships every decision in one round-trip so
``acontinue_run`` sees the full resolution set. These tests pin
that contract end-to-end at the backend layer and the FE wiring
that builds the batch envelope.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.backend.server import BackendServer
from ember_code.protocol import messages as msg
from ember_code.frontend.tui.backend_client import BackendClient


def _make_backend_with_pending(*pendings) -> tuple[BackendServer, list]:
    """Build a BackendServer with N pending requirements pre-registered.

    Returns (backend, list_of_Agno_requirement_mocks) so each test can
    later inspect which mocks got ``confirm()`` vs ``reject()`` calls
    and what was passed into ``acontinue_run``.
    """
    server = BackendServer.__new__(BackendServer)
    server._session = MagicMock()
    server._session.sub_agent_hitl.resolve.return_value = False  # all main-team
    server._session.main_team.acontinue_run = MagicMock()
    server._session.hook_executor.execute = AsyncMock(
        return_value=MagicMock(should_continue=True, message="")
    )
    server._session.session_id = "sess"
    server._pending_requirements = {}
    reqs = []
    for req_id, run_id in pendings:
        req = MagicMock(name=f"req_{req_id}")
        server._pending_requirements[req_id] = (req, run_id)
        reqs.append(req)

    # acontinue_run returns an empty async iterator by default — the
    # stream tests assert on call args, not the yielded events.
    async def _empty_stream(*_a, **_kw):
        if False:
            yield  # pragma: no cover
        return

    server._session.main_team.acontinue_run = MagicMock(side_effect=_empty_stream)

    # The multiplexer wraps ``acontinue_run`` — bypass it for these
    # tests and stream the underlying call directly.
    async def _passthrough(stream):
        async for item in stream:
            yield item

    server._stream_with_subagent_hitl = _passthrough  # type: ignore[assignment]
    return server, reqs


class TestResolveHitlBatch:
    @pytest.mark.asyncio
    async def test_passes_every_resolved_requirement_to_acontinue_run(self):
        """The load-bearing fix: ``acontinue_run`` must receive ALL
        the user-confirmed requirements in a single ``requirements=``
        kwarg, not just one. This is what the per-req loop got wrong
        — and what produced the v0.5.11 "user denied" cascade.
        """
        server, reqs = _make_backend_with_pending(("r1", "run-X"), ("r2", "run-X"), ("r3", "run-X"))
        decisions = [
            msg.HITLDecision(requirement_id="r1", action="confirm", choice="once"),
            msg.HITLDecision(requirement_id="r2", action="confirm", choice="similar"),
            msg.HITLDecision(requirement_id="r3", action="confirm", choice="once"),
        ]

        async for _ in server.resolve_hitl_batch(decisions):
            pass

        # Every Agno requirement was confirmed exactly once.
        for r in reqs:
            r.confirm.assert_called_once()
            r.reject.assert_not_called()

        # acontinue_run called ONCE with the full set of reqs.
        server._session.main_team.acontinue_run.assert_called_once()
        kwargs = server._session.main_team.acontinue_run.call_args.kwargs
        assert kwargs["run_id"] == "run-X"
        assert kwargs["requirements"] == reqs

    @pytest.mark.asyncio
    async def test_mixed_confirm_and_reject_routed_correctly(self):
        """Per-req actions: confirm goes to ``req.confirm()``,
        reject goes to ``req.reject(note=...)``. Both still end up
        in the single ``acontinue_run`` batch — Agno needs to see
        every requirement resolved one way or the other so nothing
        is auto-denied behind the user's back."""
        server, reqs = _make_backend_with_pending(("a", "run-Y"), ("b", "run-Y"))
        decisions = [
            msg.HITLDecision(requirement_id="a", action="confirm", choice="once"),
            msg.HITLDecision(requirement_id="b", action="reject", choice="once"),
        ]

        async for _ in server.resolve_hitl_batch(decisions):
            pass

        reqs[0].confirm.assert_called_once()
        reqs[0].reject.assert_not_called()
        reqs[1].reject.assert_called_once()
        reqs[1].confirm.assert_not_called()

        kwargs = server._session.main_team.acontinue_run.call_args.kwargs
        assert kwargs["requirements"] == reqs

    @pytest.mark.asyncio
    async def test_unknown_requirement_yields_error(self):
        """An unknown requirement id should surface as an ``Error``
        message rather than silently dropping it — otherwise an FE
        that ships a stale id would have no idea why the run didn't
        resume."""
        server, _ = _make_backend_with_pending(("known", "run-Z"))
        decisions = [
            msg.HITLDecision(requirement_id="known", action="confirm", choice="once"),
            msg.HITLDecision(requirement_id="ghost", action="confirm", choice="once"),
        ]

        out = []
        async for proto in server.resolve_hitl_batch(decisions):
            out.append(proto)

        errors = [m for m in out if isinstance(m, msg.Error)]
        assert len(errors) == 1
        assert "ghost" in errors[0].text

    @pytest.mark.asyncio
    async def test_empty_decisions_is_noop(self):
        """No decisions → no acontinue_run, no error. Defensive
        against an FE that built a pause with zero requirements
        (shouldn't happen, but cheap to guard)."""
        server, _ = _make_backend_with_pending(("r1", "run"))
        async for _ in server.resolve_hitl_batch([]):
            pass
        server._session.main_team.acontinue_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_sub_agent_decisions_routed_to_coordinator_not_acontinue_run(self):
        """Sub-agent requirements have their own resolution path via
        the coordinator. They should NOT trigger ``acontinue_run`` —
        the spawning stream picks them up on its own. If ALL
        decisions are sub-agent, ``acontinue_run`` must not fire."""
        server, _ = _make_backend_with_pending(("sub1", "run-sub"))
        # Force the sub-agent coordinator to claim the requirement.
        server._session.sub_agent_hitl.resolve.return_value = True

        decisions = [
            msg.HITLDecision(requirement_id="sub1", action="confirm", choice="once"),
        ]
        async for _ in server.resolve_hitl_batch(decisions):
            pass

        server._session.main_team.acontinue_run.assert_not_called()
        server._session.sub_agent_hitl.resolve.assert_called_with("sub1", "confirm")


class TestResolveHitlBatchHardening:
    """Defence-in-depth around ``resolve_hitl_batch``: per-req error
    isolation, cross-run rejection, and sub-agent dual-registration
    cleanup. None of these are routine paths but they're cheap to
    guard and the alternative (pause stuck forever, wrong run
    resumed silently) is bad."""

    @pytest.mark.asyncio
    async def test_one_confirm_raising_does_not_strand_others(self):
        """If ``req.confirm()`` raises on requirement #2 of 3, the
        other two must still get processed AND ``acontinue_run`` must
        still fire with the surviving reqs — otherwise a single bad
        requirement leaves the whole pause stuck forever."""
        server, reqs = _make_backend_with_pending(
            ("ok1", "run-X"), ("bad", "run-X"), ("ok2", "run-X")
        )
        reqs[1].confirm.side_effect = RuntimeError("agno blew up")

        decisions = [
            msg.HITLDecision(requirement_id="ok1", action="confirm", choice="once"),
            msg.HITLDecision(requirement_id="bad", action="confirm", choice="once"),
            msg.HITLDecision(requirement_id="ok2", action="confirm", choice="once"),
        ]
        out = []
        async for proto in server.resolve_hitl_batch(decisions):
            out.append(proto)

        # Surfaces the failure to the FE so the user sees what happened.
        errors = [m for m in out if isinstance(m, msg.Error)]
        assert len(errors) == 1
        assert "bad" in errors[0].text

        # The two good reqs still resolved and acontinue_run got them.
        reqs[0].confirm.assert_called_once()
        reqs[2].confirm.assert_called_once()
        server._session.main_team.acontinue_run.assert_called_once()
        kwargs = server._session.main_team.acontinue_run.call_args.kwargs
        assert kwargs["requirements"] == [reqs[0], reqs[2]]

    @pytest.mark.asyncio
    async def test_cross_run_batch_rejected_with_error_and_req_preserved(self):
        """A batch must not span runs. If it does, the offending req
        is rejected with an ``Error`` and put back into the pending
        dict so a correctly-scoped retry can succeed."""
        server, reqs = _make_backend_with_pending(("a", "run-X"), ("b", "run-Y"))
        decisions = [
            msg.HITLDecision(requirement_id="a", action="confirm", choice="once"),
            msg.HITLDecision(requirement_id="b", action="confirm", choice="once"),
        ]
        out = []
        async for proto in server.resolve_hitl_batch(decisions):
            out.append(proto)

        errors = [m for m in out if isinstance(m, msg.Error)]
        assert len(errors) == 1
        assert "b" in errors[0].text and "run-Y" in errors[0].text

        # First req still resolved against run-X.
        reqs[0].confirm.assert_called_once()
        reqs[1].confirm.assert_not_called()
        kwargs = server._session.main_team.acontinue_run.call_args.kwargs
        assert kwargs["run_id"] == "run-X"
        assert kwargs["requirements"] == [reqs[0]]

        # The cross-run req is still pending so a future batch can
        # resolve it under the right run_id.
        assert "b" in server._pending_requirements

    @pytest.mark.asyncio
    async def test_sub_agent_claim_also_clears_main_team_entry(self):
        """If a requirement id ever exists in both routing paths
        (shouldn't happen by construction, but defensive), the
        sub-agent claim must also evict the main-team entry to keep
        ``_pending_requirements`` clean."""
        server, _ = _make_backend_with_pending(("dual", "run-X"))
        server._session.sub_agent_hitl.resolve.return_value = True

        decisions = [msg.HITLDecision(requirement_id="dual", action="confirm", choice="once")]
        async for _ in server.resolve_hitl_batch(decisions):
            pass

        assert "dual" not in server._pending_requirements
        server._session.main_team.acontinue_run.assert_not_called()


class TestDropPendingForRun:
    """``_drop_pending_for_run`` sweeps stale pending reqs when a run
    completes/errors without going through HITL resolution (user
    closed UI mid-pause, run later cancelled, etc.)."""

    def test_drops_only_matching_run_id(self):
        server, _ = _make_backend_with_pending(("a", "run-X"), ("b", "run-X"), ("c", "run-Y"))

        server._drop_pending_for_run("run-X")

        assert "a" not in server._pending_requirements
        assert "b" not in server._pending_requirements
        assert "c" in server._pending_requirements

    def test_unknown_run_id_is_noop(self):
        server, _ = _make_backend_with_pending(("a", "run-X"))
        server._drop_pending_for_run("run-MISSING")
        assert "a" in server._pending_requirements


class TestResolveHitlShim:
    """``resolve_hitl`` (single-req) is now a thin shim over
    ``resolve_hitl_batch``. This pins the delegation so a future
    refactor doesn't accidentally re-introduce the old
    ``acontinue_run(requirements=[req])`` callsite — the very bug
    that produced the v0.5.11 cascade. If anyone calls the
    single-req entry point, it must go through the batch path."""

    @pytest.mark.asyncio
    async def test_single_resolve_routes_through_batch(self):
        server, reqs = _make_backend_with_pending(("r1", "run-X"))

        async for _ in server.resolve_hitl("r1", "confirm", "once"):
            pass

        reqs[0].confirm.assert_called_once()
        server._session.main_team.acontinue_run.assert_called_once()
        kwargs = server._session.main_team.acontinue_run.call_args.kwargs
        assert kwargs["run_id"] == "run-X"
        assert kwargs["requirements"] == reqs

    @pytest.mark.asyncio
    async def test_single_reject_routes_through_batch(self):
        server, reqs = _make_backend_with_pending(("r1", "run-X"))

        async for _ in server.resolve_hitl("r1", "reject"):
            pass

        reqs[0].reject.assert_called_once()
        reqs[0].confirm.assert_not_called()
        # Reject still calls acontinue_run with the requirement in the
        # batch — Agno needs every req resolved, including rejections.
        server._session.main_team.acontinue_run.assert_called_once()


class TestBackendClientBatch:
    """The FE-side wrapper builds the protocol envelope from a list of
    tuples. Plain-text shape check so the wire format stays stable."""

    @pytest.mark.asyncio
    async def test_builds_hitl_response_batch_envelope(self):
        client = BackendClient.__new__(BackendClient)
        sent = []

        async def _stream(message):
            sent.append(message)
            if False:
                yield  # pragma: no cover

        client._stream = _stream  # type: ignore[assignment]

        async for _ in client.resolve_hitl_batch(
            [
                ("r1", "confirm", "once"),
                ("r2", "confirm", "similar"),
                ("r3", "reject", "once"),
            ]
        ):
            pass

        assert len(sent) == 1
        envelope = sent[0]
        assert isinstance(envelope, msg.HITLResponseBatch)
        assert len(envelope.decisions) == 3
        assert envelope.decisions[0].requirement_id == "r1"
        assert envelope.decisions[0].action == "confirm"
        assert envelope.decisions[0].choice == "once"
        assert envelope.decisions[2].action == "reject"
