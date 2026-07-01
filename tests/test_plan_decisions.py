"""Plan-decision persistence contract.

The bug this whole module guards against: a user typed ``/plan``,
the agent submitted a plan via ``exit_plan_mode``, and the
PlanCard rendered with the footer text *"Plan approved — plan
mode exited"* despite the user never clicking the Approve
button. Root cause was ``BackendServer._infer_plan_state``: any
time the permission mode happened to be ``default`` at
rehydration, every plan turn that lacked an explicit decision
got stamped as ``"approved"`` — silently swallowing never-acted
plans whenever the mode flipped for any other reason.

The fix moved plan state from inferred-from-mode to
persisted-via-explicit-decision. These tests pin the new
contract end to end:

* ``PlanStore.decisions`` — in-memory map, validated input.
* ``Session.approve_plan`` / ``dismiss_plan`` — recording +
  persistence + broadcast + mode flip (approve only).
* The "I've never approved it" regression — a mode flip with
  no decision must leave state ``pending``.
"""

from __future__ import annotations

import pytest

from ember_code.core.tools.plan import PlanStore

# ── PlanStore.decisions ─────────────────────────────────────


class TestPlanStoreDecisions:
    def test_default_empty(self) -> None:
        store = PlanStore()
        assert store.decisions == {}

    def test_set_decision_records_approval(self) -> None:
        store = PlanStore()
        store.set_decision("run-abc", "approved")
        assert store.get_decision("run-abc") == "approved"

    def test_set_decision_records_dismissal(self) -> None:
        store = PlanStore()
        store.set_decision("run-xyz", "dismissed")
        assert store.get_decision("run-xyz") == "dismissed"

    def test_set_decision_overwrites(self) -> None:
        # Approve, then change your mind via Refine. Last writer
        # wins — there's no "you can't undo" semantics.
        store = PlanStore()
        store.set_decision("run-1", "approved")
        store.set_decision("run-1", "dismissed")
        assert store.get_decision("run-1") == "dismissed"

    def test_set_decision_rejects_invalid_value(self) -> None:
        store = PlanStore()
        with pytest.raises(ValueError, match="decision must be one of"):
            store.set_decision("run-1", "maybe")

    def test_set_decision_rejects_empty_run_id(self) -> None:
        store = PlanStore()
        with pytest.raises(ValueError, match="run_id must be non-empty"):
            store.set_decision("", "approved")

    def test_get_decision_returns_none_for_unknown_run(self) -> None:
        # The whole point: NO decision recorded → None → caller
        # treats as pending. This is the path the original bug
        # short-circuited via mode inference.
        store = PlanStore()
        assert store.get_decision("never-decided") is None

    def test_get_decision_empty_run_id_returns_none(self) -> None:
        store = PlanStore()
        store.set_decision("run-1", "approved")
        # Defensive — an empty key collision would mask real
        # decisions.
        assert store.get_decision("") is None

    def test_load_decisions_accepts_valid_blob(self) -> None:
        store = PlanStore()
        store.load_decisions({"r1": "approved", "r2": "dismissed"})
        assert store.get_decision("r1") == "approved"
        assert store.get_decision("r2") == "dismissed"

    def test_load_decisions_filters_invalid_entries(self) -> None:
        # Persistence-layer corruption (e.g. someone hand-edited
        # session_data) should be silently filtered, not raise.
        store = PlanStore()
        store.load_decisions(
            {
                "good": "approved",
                "bad-value": "maybe",
                42: "approved",  # type: ignore[dict-item]
                "empty-key": "",
            }
        )
        assert store.decisions == {"good": "approved"}

    def test_load_decisions_none_is_noop(self) -> None:
        store = PlanStore()
        store.set_decision("r1", "approved")
        store.load_decisions(None)
        assert store.decisions == {"r1": "approved"}

    def test_load_decisions_non_dict_is_noop(self) -> None:
        store = PlanStore()
        store.load_decisions("not a dict")  # type: ignore[arg-type]
        store.load_decisions([("r1", "approved")])  # type: ignore[arg-type]
        assert store.decisions == {}

    def test_snapshot_returns_independent_copy(self) -> None:
        # Returning the live dict would let the persistence layer
        # mutate the store while serializing — caused subtle bugs
        # when the persistence layer was async.
        store = PlanStore()
        store.set_decision("r1", "approved")
        snap = store.decisions_snapshot()
        snap["r1"] = "dismissed"  # mutate the copy
        assert store.get_decision("r1") == "approved"  # store unchanged


# ── Session.approve_plan / dismiss_plan ─────────────────────


class _StubPermissionEvaluator:
    """Just enough surface for ``set_permission_mode``."""

    def __init__(self) -> None:
        from ember_code.core.config.permission_eval import PermissionMode

        self.mode = PermissionMode.PLAN


class _StubPersistence:
    """Captures save_plan_decisions calls without touching disk."""

    def __init__(self) -> None:
        self.saved: list[dict[str, str]] = []
        self.fail = False

    async def save_plan_decisions(self, decisions: dict[str, str]) -> None:
        if self.fail:
            raise RuntimeError("simulated DB outage")
        # Snapshot — mirror what the real persistence layer does
        # (no live reference).
        self.saved.append(dict(decisions))

    async def load_plan_decisions(self) -> dict[str, str]:
        return {}


def _build_session() -> object:
    """Construct a Session-shaped object exposing only what
    ``approve_plan`` / ``dismiss_plan`` touch. Skips Agno
    initialisation — keeps the test fast and isolated from
    schema drift in the storage layer."""
    from ember_code.core.session.core import Session

    session = Session.__new__(Session)
    session._broadcast_callbacks = []
    session._pending_post_run_broadcasts = []
    session.plan_store = PlanStore()
    session.permission_evaluator = _StubPermissionEvaluator()
    session.persistence = _StubPersistence()
    return session


class TestSessionApproveDismiss:
    async def test_approve_records_decision(self) -> None:
        session = _build_session()
        result = await session.approve_plan("run-123")
        assert session.plan_store.get_decision("run-123") == "approved"
        assert result["run_id"] == "run-123"
        assert result["decision"] == "approved"

    async def test_approve_persists(self) -> None:
        # session_data write must happen — without this the
        # decision evaporates on reload, which is the original
        # bug.
        session = _build_session()
        await session.approve_plan("run-123")
        saved = session.persistence.saved
        assert saved and saved[-1] == {"run-123": "approved"}

    async def test_approve_flips_mode_to_default(self) -> None:
        from ember_code.core.config.permission_eval import PermissionMode

        session = _build_session()
        await session.approve_plan("run-123")
        assert session.permission_evaluator.mode is PermissionMode.DEFAULT

    async def test_approve_broadcasts_plan_decided(self) -> None:
        # FE listens on this channel to flip the card. Without
        # the broadcast the user would click Approve and watch
        # the card stay in pending until they reloaded.
        session = _build_session()
        received: list[tuple[str, dict]] = []
        session._broadcast_callbacks.append(lambda c, p: received.append((c, p)))
        await session.approve_plan("run-123")
        decided = [(c, p) for c, p in received if c == "plan_decided"]
        assert decided
        c, p = decided[0]
        assert p == {"run_id": "run-123", "decision": "approved"}

    async def test_dismiss_records_decision(self) -> None:
        session = _build_session()
        await session.dismiss_plan("run-789")
        assert session.plan_store.get_decision("run-789") == "dismissed"

    async def test_dismiss_does_NOT_flip_mode(self) -> None:
        # Refine = "stay in plan mode, let me iterate". A mode
        # flip here would defeat the entire point of the button.
        from ember_code.core.config.permission_eval import PermissionMode

        session = _build_session()
        await session.dismiss_plan("run-789")
        assert session.permission_evaluator.mode is PermissionMode.PLAN

    async def test_dismiss_broadcasts_plan_decided(self) -> None:
        session = _build_session()
        received: list[tuple[str, dict]] = []
        session._broadcast_callbacks.append(lambda c, p: received.append((c, p)))
        await session.dismiss_plan("run-789")
        decided = [(c, p) for c, p in received if c == "plan_decided"]
        assert decided
        assert decided[0][1] == {"run_id": "run-789", "decision": "dismissed"}

    async def test_empty_run_id_raises(self) -> None:
        session = _build_session()
        with pytest.raises(ValueError, match="run_id must be non-empty"):
            await session.approve_plan("")
        with pytest.raises(ValueError, match="run_id must be non-empty"):
            await session.dismiss_plan("")

    async def test_persistence_failure_does_NOT_block_decision(self) -> None:
        # If session_data write fails (transient DB issue), the
        # in-memory state still has the decision and the
        # broadcast still fires. The user sees the right UI; the
        # loss surfaces only on reload, which is acceptable
        # since the next decision will rewrite the blob anyway.
        session = _build_session()
        session.persistence.fail = True
        received: list[tuple[str, dict]] = []
        session._broadcast_callbacks.append(lambda c, p: received.append((c, p)))
        result = await session.approve_plan("run-1")
        assert result["decision"] == "approved"
        assert session.plan_store.get_decision("run-1") == "approved"
        assert any(c == "plan_decided" for c, _ in received)


# ── The "I've never approved it" regression ─────────────────


class TestNeverApprovedRegression:
    """The original bug. Reproduced as the exact sequence the
    user reported, then asserted to NOT exhibit the pre-fix
    symptom.

    Sequence:
      1. Agent submits a plan via ``exit_plan_mode`` (run_id=R1).
      2. Permission mode flips to ``default`` for any OTHER
         reason — could be a bug, could be a slash command, could
         be a different toolkit. The user has not clicked
         Approve / Refine.
      3. Chat history is rehydrated (e.g. on reload, on view
         switch).
      4. The latest plan turn's ``state`` must still be
         ``"pending"``. The pre-fix code returned ``"approved"``
         here purely because mode != plan — that's the bug.
    """

    async def test_mode_flip_without_decision_keeps_pending(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        from ember_code.backend.server import BackendServer

        # Build a single-plan history with no recorded decision.
        plan_call = {
            "id": "call_plan_1",
            "type": "function",
            "function": {
                "name": "exit_plan_mode",
                "arguments": '{"plan": "Step 1.", "tasks": []}',
            },
        }
        assistant_msg = SimpleNamespace(
            role="assistant",
            content="",
            tool_calls=[plan_call],
            reasoning_content=None,
            from_history=False,
            created_at=10,
        )
        tool_msg = SimpleNamespace(
            role="tool",
            content="Plan submitted.",
            tool_name="exit_plan_mode",
            tool_args=None,
            tool_call_id="call_plan_1",
            tool_call_error=False,
            from_history=False,
            created_at=11,
        )
        run = SimpleNamespace(
            run_id="R1",
            parent_run_id=None,
            messages=[assistant_msg, tool_msg],
            metrics=None,
        )
        agno_session = SimpleNamespace(runs=[run])

        server = BackendServer.__new__(BackendServer)
        server._session = MagicMock()
        server._session.session_id = "sess"
        server._session.user_id = "u"
        server._session.main_team.aget_session = AsyncMock(return_value=agno_session)
        # The exact failure condition from the bug report: mode
        # flipped to default, but no plan_decisions entry for R1.
        server._session.permission_evaluator = SimpleNamespace(
            mode=SimpleNamespace(value="default")
        )
        server._session.plan_store = PlanStore()  # empty decisions

        history = await server.get_chat_history("sess")
        plan_turns = [t for t in history if t.get("role") == "plan"]
        assert len(plan_turns) == 1
        assert plan_turns[0]["state"] == "pending", (
            "Bug regression: mode flipped to default with NO recorded "
            "decision still marks the plan as approved. The whole point "
            "of plan_decisions is to break this inference."
        )

    async def test_recorded_approval_survives_rehydration(self) -> None:
        # The other half — once a decision IS recorded, it
        # surfaces on reload regardless of current mode.
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        from ember_code.backend.server import BackendServer

        plan_call = {
            "id": "call_plan_1",
            "type": "function",
            "function": {
                "name": "exit_plan_mode",
                "arguments": '{"plan": "Step 1.", "tasks": []}',
            },
        }
        assistant_msg = SimpleNamespace(
            role="assistant",
            content="",
            tool_calls=[plan_call],
            reasoning_content=None,
            from_history=False,
            created_at=10,
        )
        tool_msg = SimpleNamespace(
            role="tool",
            content="Plan submitted.",
            tool_name="exit_plan_mode",
            tool_args=None,
            tool_call_id="call_plan_1",
            tool_call_error=False,
            from_history=False,
            created_at=11,
        )
        run = SimpleNamespace(
            run_id="R2",
            parent_run_id=None,
            messages=[assistant_msg, tool_msg],
            metrics=None,
        )
        agno_session = SimpleNamespace(runs=[run])

        server = BackendServer.__new__(BackendServer)
        server._session = MagicMock()
        server._session.session_id = "sess"
        server._session.user_id = "u"
        server._session.main_team.aget_session = AsyncMock(return_value=agno_session)
        server._session.permission_evaluator = SimpleNamespace(
            mode=SimpleNamespace(value="plan")  # still in plan mode...
        )
        store = PlanStore()
        store.set_decision("R2", "approved")  # ...but user approved
        server._session.plan_store = store

        history = await server.get_chat_history("sess")
        plan_turns = [t for t in history if t.get("role") == "plan"]
        assert plan_turns[0]["state"] == "approved", (
            "Recorded approval must take precedence over mode. The "
            "decision is the source of truth; mode is incidental."
        )

    async def test_historical_plan_without_decision_marks_dismissed(self) -> None:
        # Two plans, neither decided. The OLDER one is treated
        # as dismissed (user moved on); the LATEST stays pending.
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        from ember_code.backend.server import BackendServer

        def _run(run_id: str, plan_text: str, ts: int) -> SimpleNamespace:
            call = {
                "id": f"call_{run_id}",
                "type": "function",
                "function": {
                    "name": "exit_plan_mode",
                    "arguments": f'{{"plan": "{plan_text}", "tasks": []}}',
                },
            }
            return SimpleNamespace(
                run_id=run_id,
                parent_run_id=None,
                messages=[
                    SimpleNamespace(
                        role="assistant",
                        content="",
                        tool_calls=[call],
                        reasoning_content=None,
                        from_history=False,
                        created_at=ts,
                    ),
                    SimpleNamespace(
                        role="tool",
                        content="Plan submitted.",
                        tool_name="exit_plan_mode",
                        tool_args=None,
                        tool_call_id=f"call_{run_id}",
                        tool_call_error=False,
                        from_history=False,
                        created_at=ts + 1,
                    ),
                ],
                metrics=None,
            )

        agno_session = SimpleNamespace(
            runs=[_run("R-old", "Old plan", 10), _run("R-new", "New plan", 20)]
        )

        server = BackendServer.__new__(BackendServer)
        server._session = MagicMock()
        server._session.session_id = "sess"
        server._session.user_id = "u"
        server._session.main_team.aget_session = AsyncMock(return_value=agno_session)
        server._session.permission_evaluator = SimpleNamespace(
            mode=SimpleNamespace(value="default")
        )
        server._session.plan_store = PlanStore()

        history = await server.get_chat_history("sess")
        plan_turns = [t for t in history if t.get("role") == "plan"]
        assert len(plan_turns) == 2
        # Older plan: user moved on without clicking. We render
        # it as dismissed so the footer is clean and there are
        # no stale Approve buttons. The plan text stays
        # visible — it's part of the transcript.
        assert plan_turns[0]["state"] == "dismissed"
        # Latest plan: still up for grabs.
        assert plan_turns[1]["state"] == "pending"


# ── Post-run broadcast run_id stamping ──────────────────────


class TestPostRunBroadcastRunIdStamp:
    """The FE needs ``run_id`` in the ``plan_submitted`` push
    payload to key approve/dismiss RPCs. The plan tool can't
    see the run_id from inside its toolkit context, so the run
    loop stamps it at drain time."""

    def test_drain_stamps_run_id_into_payload(self) -> None:
        from ember_code.core.session.core import Session

        session = Session.__new__(Session)
        session._broadcast_callbacks = []
        session._pending_post_run_broadcasts = []
        received: list[tuple[str, dict]] = []
        session._broadcast_callbacks.append(lambda c, p: received.append((c, p)))

        session.queue_post_run_broadcast("plan_submitted", {"plan": "X", "tasks": []})
        session.drain_post_run_broadcasts(run_id="R-stamped")

        assert received == [("plan_submitted", {"plan": "X", "tasks": [], "run_id": "R-stamped"})]

    def test_drain_without_run_id_preserves_payload(self) -> None:
        # Drain called from a context that doesn't have a
        # run_id (degenerate / cancelled). Payload passes
        # through untouched.
        from ember_code.core.session.core import Session

        session = Session.__new__(Session)
        session._broadcast_callbacks = []
        session._pending_post_run_broadcasts = []
        received: list[tuple[str, dict]] = []
        session._broadcast_callbacks.append(lambda c, p: received.append((c, p)))

        session.queue_post_run_broadcast("plan_submitted", {"plan": "X", "tasks": []})
        session.drain_post_run_broadcasts()  # no run_id arg

        assert received == [("plan_submitted", {"plan": "X", "tasks": []})]

    def test_drain_does_NOT_overwrite_existing_run_id(self) -> None:
        # If the caller explicitly set run_id in the payload,
        # the drain respects it. Belt and suspenders for tools
        # that already know their run_id at queue time.
        from ember_code.core.session.core import Session

        session = Session.__new__(Session)
        session._broadcast_callbacks = []
        session._pending_post_run_broadcasts = []
        received: list[tuple[str, dict]] = []
        session._broadcast_callbacks.append(lambda c, p: received.append((c, p)))

        session.queue_post_run_broadcast("plan_submitted", {"plan": "X", "run_id": "explicit"})
        session.drain_post_run_broadcasts(run_id="from-loop")

        assert received[0][1]["run_id"] == "explicit"
