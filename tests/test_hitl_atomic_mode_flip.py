"""Regression: HITL ``set_permission_mode`` flips the session mode
atomically with the resume — before the agent's next permission
check.

The bug this file guards: the FE's "Accept all edits during this
session" shortcut used to fire a separate ``/accept on`` slash
command alongside the HITL batch. The BE's WS orchestrator
dispatches every inbound message as its own ``asyncio`` task, so
the slash command and the HITL resolve ran concurrently. The
HITL resolve could call ``team.acontinue_run(...)`` and resume
the agent BEFORE the slash command finished
``set_permission_mode("acceptEdits")`` — so the very next tool
check after the resume saw the OLD mode and re-prompted the user
instead of auto-approving.

The fix moved the mode flip INTO the HITL decision (optional
``set_permission_mode`` field) and the BE applies it inside
``resolve_batch`` BEFORE the ``acontinue_run`` call. Single
message, atomic mutation, no race.

What we pin:
- The session's ``set_permission_mode`` is called with the value
  on the FIRST non-empty decision, before ``acontinue_run``.
- Empty string / missing field → no-op (back-compat for clients
  that don't know about the field).
- When multiple decisions carry a non-empty value, only the
  first wins — subsequent values are ignored.
- The HITLDecision schema carries the new field end-to-end
  (Pydantic round-trip preserves it).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from ember_code.backend.hitl_controller import HitlController
from ember_code.backend.pending_requirements_store import PendingRequirementsStore
from ember_code.backend.schemas_pause import PendingRequirement
from ember_code.protocol import messages as msg


async def _empty_async_iter():
    """Empty async iterator — ``acontinue_run`` returns no events in
    these unit tests since we only care about the call timing."""
    if False:
        yield  # pragma: no cover — turns this into a generator


def _fake_session() -> MagicMock:
    """Minimal Session stand-in for HitlController — the controller
    only touches ``set_permission_mode``, ``main_team``, ``session_id``,
    ``hook_executor``, ``sub_agent_hitl`` on the resume path."""
    session = MagicMock()
    session.set_permission_mode = MagicMock(return_value="Permission mode: default → acceptEdits")
    session.session_id = "test-session"
    # ``acontinue_run`` is the call we want to observe AFTER the
    # mode flip. It's an async method on the main team.
    session.main_team = MagicMock()
    session.main_team.acontinue_run = AsyncMock(return_value=_empty_async_iter())
    # Hook executor fires after the resume; mock it so the
    # controller doesn't blow up.
    session.hook_executor = MagicMock()
    session.hook_executor.execute = AsyncMock(
        return_value=SimpleNamespace(should_continue=False, message="")
    )
    # Sub-agent HITL — none in this test.
    session.sub_agent_hitl = SimpleNamespace(resolve=lambda *_: False)
    return session


def _push(store: PendingRequirementsStore, req_id: str, run_id: str = "run-1") -> None:
    """Seed the store with a single requirement. The resolver reads
    ``entry.req`` and ``entry.run_id`` off the store entry."""
    store.register(
        req_id,
        PendingRequirement(req=MagicMock(), run_id=run_id),
    )


def _call_order(session: MagicMock) -> list[str]:
    """Return the names of the session's mock-calls in invocation
    order — flattened across the session and its children via the
    parent ``mock_calls`` chain. Used to assert the mode flip
    happens BEFORE the resume."""
    return [str(c) for c in session.mock_calls]


class TestAtomicModeFlip:
    """The mode flip must land BEFORE ``acontinue_run`` so the
    next tool check sees the new mode."""

    @pytest.mark.asyncio
    async def test_flip_happens_before_acontinue_run(self) -> None:
        """When a decision carries ``set_permission_mode="acceptEdits"``,
        ``set_permission_mode`` is called on the session BEFORE
        ``acontinue_run`` resumes the agent. Verified by ordering
        on the session's mock manager (parent ``mock_calls``
        records every child call in invocation order)."""
        session = _fake_session()
        store = PendingRequirementsStore()
        _push(store, "req-1")

        controller = HitlController(session, store)

        decisions = [
            msg.HITLDecision(
                requirement_id="req-1",
                action="confirm",
                choice="once",
                set_permission_mode="acceptEdits",
            ),
        ]

        async def drain() -> None:
            async for _ in controller.resolve_batch(decisions):
                pass

        await drain()

        # Both calls happened exactly once.
        assert session.set_permission_mode.call_count == 1
        assert session.set_permission_mode.call_args == call("acceptEdits")
        assert session.main_team.acontinue_run.call_count == 1

        # Order matters — the flip must precede the resume.
        calls = _call_order(session)
        flip_idx = next(i for i, c in enumerate(calls) if "set_permission_mode" in c)
        resume_idx = next(i for i, c in enumerate(calls) if "acontinue_run" in c)
        assert flip_idx < resume_idx, (
            f"mode flip must happen before resume, otherwise the "
            f"agent's next tool check sees the OLD mode and re-prompts. "
            f"call order was: {calls}"
        )

    @pytest.mark.asyncio
    async def test_no_op_when_set_permission_mode_is_omitted(self) -> None:
        """Back-compat: a decision without ``set_permission_mode``
        at all doesn't flip the mode. Mirrors the pre-fix wire
        shape so old clients don't accidentally fire a flip."""
        session = _fake_session()
        store = PendingRequirementsStore()
        _push(store, "req-1")
        controller = HitlController(session, store)

        d = msg.HITLDecision(
            requirement_id="req-1",
            action="confirm",
            choice="once",
        )

        async def drain() -> None:
            async for _ in controller.resolve_batch([d]):
                pass

        await drain()
        assert session.set_permission_mode.call_count == 0

    @pytest.mark.asyncio
    async def test_no_op_when_set_permission_mode_is_empty(self) -> None:
        """Empty string is the canonical no-op value — matches the
        schema's default and the FE's existing pre-fix behavior."""
        session = _fake_session()
        store = PendingRequirementsStore()
        _push(store, "req-1")
        controller = HitlController(session, store)

        d = msg.HITLDecision(
            requirement_id="req-1",
            action="confirm",
            choice="once",
            set_permission_mode="",
        )

        async def drain() -> None:
            async for _ in controller.resolve_batch([d]):
                pass

        await drain()
        assert session.set_permission_mode.call_count == 0

    @pytest.mark.asyncio
    async def test_first_non_empty_value_wins(self) -> None:
        """If two decisions both carry a non-empty value, the first
        one wins. Defensive against callers that don't coordinate
        — the field is "set this if you want a flip, otherwise
        leave blank"."""
        session = _fake_session()
        store = PendingRequirementsStore()
        _push(store, "req-1")
        _push(store, "req-2")
        controller = HitlController(session, store)

        decisions = [
            msg.HITLDecision(
                requirement_id="req-1",
                action="confirm",
                choice="once",
                set_permission_mode="acceptEdits",
            ),
            msg.HITLDecision(
                requirement_id="req-2",
                action="confirm",
                choice="once",
                set_permission_mode="bypassPermissions",
            ),
        ]

        async def drain() -> None:
            async for _ in controller.resolve_batch(decisions):
                pass

        await drain()

        # Only the first one was applied.
        assert session.set_permission_mode.call_count == 1
        assert session.set_permission_mode.call_args == call("acceptEdits")

    @pytest.mark.asyncio
    async def test_unknown_mode_is_forwarded_as_is(self) -> None:
        """The schema accepts any string for ``set_permission_mode``
        (back-compat wire), but ``set_permission_mode`` on the
        session returns an error string for unknown values. The
        controller just forwards whatever the decision says — it
        doesn't validate the enum. Pin that behaviour so a future
        tightening (e.g. switching to ``Literal[...]``) is a
        deliberate breaking change, not an accidental one."""
        session = _fake_session()
        store = PendingRequirementsStore()
        _push(store, "req-1")
        controller = HitlController(session, store)

        decisions = [
            msg.HITLDecision(
                requirement_id="req-1",
                action="confirm",
                choice="once",
                set_permission_mode="notARealMode",
            ),
        ]

        async def drain() -> None:
            async for _ in controller.resolve_batch(decisions):
                pass

        await drain()

        # Forwarded as-is to the session; the session's own validator
        # would surface the error in the response. We don't second-
        # guess the session here.
        assert session.set_permission_mode.call_count == 1
        assert session.set_permission_mode.call_args == call("notARealMode")


class TestHitlDecisionSchema:
    """The new field must round-trip through Pydantic unchanged so
    the FE → BE wire shape stays in lock-step. Catches a refactor
    that drops or renames the field on either side."""

    def test_default_is_empty_string(self) -> None:
        """Back-compat default: omitting the field produces an
        empty string on the wire, which the BE treats as a no-op."""
        d = msg.HITLDecision(
            requirement_id="req-1",
            action="confirm",
            choice="once",
        )
        assert d.set_permission_mode == ""

    def test_acceptEdits_round_trips_through_dump(self) -> None:
        d = msg.HITLDecision(
            requirement_id="req-1",
            action="confirm",
            choice="once",
            set_permission_mode="acceptEdits",
        )
        # ``.model_dump()`` is the canonical wire shape — what the
        # FE builder sends and what the BE parses back.
        wire = d.model_dump()
        assert wire["set_permission_mode"] == "acceptEdits"
        # Round-trip back through ``model_validate``.
        rebuilt = msg.HITLDecision.model_validate(wire)
        assert rebuilt.set_permission_mode == "acceptEdits"

    def test_bypassPermissions_round_trips_through_dump(self) -> None:
        d = msg.HITLDecision(
            requirement_id="req-1",
            action="reject",
            choice="",
            set_permission_mode="bypassPermissions",
        )
        wire = d.model_dump()
        assert wire["set_permission_mode"] == "bypassPermissions"
        rebuilt = msg.HITLDecision.model_validate(wire)
        assert rebuilt.set_permission_mode == "bypassPermissions"

    def test_batch_with_mixed_values_serializes_each(self) -> None:
        """A batch can carry one decision with ``set_permission_mode``
        and several without — the wire shape preserves per-decision
        values, not a batch-level field."""
        batch = [
            msg.HITLDecision(
                requirement_id="r1",
                action="confirm",
                choice="once",
                set_permission_mode="acceptEdits",
            ),
            msg.HITLDecision(
                requirement_id="r2",
                action="confirm",
                choice="always",
            ),
            msg.HITLDecision(
                requirement_id="r3",
                action="reject",
                choice="",
                set_permission_mode="bypassPermissions",
            ),
        ]
        wire = [d.model_dump() for d in batch]
        assert wire[0]["set_permission_mode"] == "acceptEdits"
        assert wire[1]["set_permission_mode"] == ""
        assert wire[2]["set_permission_mode"] == "bypassPermissions"
