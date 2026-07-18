"""Tests for plan mode (row 50) — CC's user-toggled read-only
sandbox with a plan-then-execute workflow.

Three layers:

* :class:`PlanStore` and :class:`PlanTool` — the per-session
  store + agent-facing ``exit_plan_mode`` tool.
* :class:`Session.set_permission_mode` — the runtime mode
  flipper consumed by the slash command and the tool. (The
  ENFORCEMENT side — denying file edits when ``mode == plan`` —
  is already covered by ``test_permission_eval.py``; this file
  just verifies the toggle plumbing.)
* The ``/plan`` slash command end-to-end through the dispatch
  table.
* The ``GET_LATEST_PLAN`` RPC.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.backend.__main__ import _build_rpc_table
from ember_code.backend.command_handler import CommandHandler
from ember_code.backend.server import BackendServer
from ember_code.core.config.permission_eval import PermissionEvaluator, PermissionMode
from ember_code.core.session.broadcast import BroadcastBus as _BroadcastBus
from ember_code.core.session.core import Session
from ember_code.core.tools.orchestrate import OrchestrateTools
from ember_code.core.tools.plan import _MAX_PLAN_ATTEMPTS, PlanStore, PlanTool
from ember_code.core.tools.todo import TodoItem, TodoStore, TodoTools
from ember_code.protocol.rpc import RpcMethod

# ── PlanStore ─────────────────────────────────────────────


class TestPlanStore:
    def test_set_plan_records_latest(self):
        store = PlanStore()
        store.set_plan("Step 1: read spec")
        assert store.latest == "Step 1: read spec"
        assert store.history == []

    def test_set_plan_moves_previous_to_history(self):
        """Submitting a new plan moves the previous one into
        history so the user can review what the agent had
        proposed before any refinement."""
        store = PlanStore()
        store.set_plan("old plan")
        store.set_plan("new plan")
        assert store.latest == "new plan"
        assert store.history == ["old plan"]

    def test_history_bounded(self):
        """Repeat /plan-toggle workflows shouldn't accumulate
        plans indefinitely. Default cap is 10."""
        store = PlanStore()
        for i in range(15):
            store.set_plan(f"plan {i}")
        assert store.latest == "plan 14"
        assert len(store.history) == 10
        # Oldest in history is plan 4 (0-3 evicted).
        assert store.history[0] == "plan 4"

    def test_snapshot_wire_shape(self):
        store = PlanStore()
        store.set_plan("a")
        store.set_plan("b")
        snap = store.snapshot()
        assert snap.latest == "b"
        assert snap.history == ["a"]


# ── PlanTool ──────────────────────────────────────────────


class TestPlanTool:
    @pytest.mark.asyncio
    async def test_exit_plan_mode_records_plan(self):
        session = MagicMock()
        session.plan_store = PlanStore()
        session._codeindex_available = False  # skip citation gate
        tool = PlanTool(session)
        result = tool.exit_plan_mode("Run tests, then refactor.")
        assert "Plan submitted" in result
        assert session.plan_store.latest == "Run tests, then refactor."

    @pytest.mark.asyncio
    async def test_exit_plan_mode_does_not_flip_mode(self):
        """SECURITY: the agent submitting a plan must NOT exit
        plan mode on its own — the user controls that via
        ``/plan``. Verified by confirming the tool doesn't
        touch the permission evaluator."""
        session = MagicMock()
        session.plan_store = PlanStore()
        session.permission_evaluator = PermissionEvaluator.from_strings(mode="plan")
        tool = PlanTool(session)
        tool.exit_plan_mode("plan body")
        assert session.permission_evaluator.mode is PermissionMode.PLAN

    @pytest.mark.asyncio
    async def test_exit_plan_mode_rejects_empty_plan(self):
        session = MagicMock()
        session.plan_store = PlanStore()
        tool = PlanTool(session)
        result = tool.exit_plan_mode("   ")
        assert "Error" in result
        assert "empty" in result.lower()
        assert session.plan_store.latest == ""

    @pytest.mark.asyncio
    async def test_exit_plan_mode_response_steers_agent_to_stop(self):
        """The reply explicitly tells the agent NOT to continue
        executing — otherwise the model often plows ahead in the
        same turn, defeating the point of plan mode."""
        session = MagicMock()
        session.plan_store = PlanStore()
        session._codeindex_available = False
        tool = PlanTool(session)
        result = tool.exit_plan_mode("plan body")
        assert "stop" in result.lower() or "do not continue" in result.lower()

    @pytest.mark.asyncio
    async def test_exit_plan_mode_with_tasks_populates_todo_store(self):
        """When the agent passes ``tasks=[...]`` alongside the
        plan, the same call populates the TodoStore so the
        PlanCard can render a live checklist next to the prose."""
        session = MagicMock()
        session.plan_store = PlanStore()
        session.todo_store = TodoStore()
        session._codeindex_available = False
        tool = PlanTool(session)
        result = tool.exit_plan_mode(
            "## Refactor\n\nSteps inside.",
            tasks=[
                {"content": "Step 1", "activeForm": "Doing step 1"},
                {"content": "Step 2", "activeForm": "Doing step 2"},
            ],
        )
        assert "Plan submitted" in result
        assert "2 structured task" in result
        assert [item.content for item in session.todo_store.items] == ["Step 1", "Step 2"]
        # All tasks start pending — the agent enumerates the plan;
        # status transitions come via subsequent ``todo_write``.
        assert all(item.status == "pending" for item in session.todo_store.items)

    @pytest.mark.asyncio
    async def test_exit_plan_mode_without_tasks_leaves_todo_store_alone(self):
        """Tasks are optional — submitting a prose-only plan
        must not blast the TodoStore."""
        session = MagicMock()
        session.plan_store = PlanStore()
        session.todo_store = TodoStore()
        session.todo_store.set([TodoItem("Pre-existing", "in_progress", "")])
        tool = PlanTool(session)
        tool.exit_plan_mode("just prose, no enumerable steps")
        # Original todos untouched.
        assert len(session.todo_store.items) == 1
        assert session.todo_store.items[0].content == "Pre-existing"

    @pytest.mark.asyncio
    async def test_exit_plan_mode_broadcasts_tasks_in_payload(self):
        """The ``plan_submitted`` push payload carries the
        structured tasks alongside the plan markdown so the FE
        seeds the PlanCard checklist on first render."""
        # Real session stand-in to exercise the broadcast.
        session = Session.__new__(Session)
        session.plan_store = PlanStore()
        session.todo_store = TodoStore()
        session.broadcast_bus = _BroadcastBus()
        captured: list[tuple[str, dict]] = []
        session.register_broadcast_callback(lambda ch, p: captured.append((ch, p)))
        tool = PlanTool(session)
        tool.exit_plan_mode(
            "plan",
            tasks=[{"content": "A"}, {"content": "B"}],
        )
        # ``exit_plan_mode`` queues the event via ``queue_post_run``;
        # the run-loop normally drains after ``RunCompleted``. In-
        # test we drain explicitly.
        session.broadcast_bus.drain_post_run(run_id=None)
        evt = next(p for ch, p in captured if ch == "plan_submitted")
        assert evt["plan"] == "plan"
        assert len(evt["tasks"]) == 2
        assert evt["tasks"][0]["content"] == "A"
        assert evt["tasks"][0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_exit_plan_mode_validation_errors_surfaced(self):
        """Malformed task rows are dropped (so the rest still
        applies) and the errors come back in the reply so the
        agent can correct the next call."""
        session = MagicMock()
        session.plan_store = PlanStore()
        session.todo_store = TodoStore()
        session._codeindex_available = False
        tool = PlanTool(session)
        result = tool.exit_plan_mode(
            "plan",
            tasks=[
                {"content": "OK row"},
                {"content": ""},  # invalid - empty content
            ],
        )
        assert "Tasks validation errors" in result
        # Good row still applied.
        assert len(session.todo_store.items) == 1


class TestExitPlanModeValidation:
    """Confidence-check gate (Path B enforcement). When
    CodeIndex is available, plans without enough file citations
    bounce back to the agent with specific feedback; bounded by
    ``_MAX_PLAN_ATTEMPTS`` so we don't ping-pong forever."""

    def _session_with_codeindex(self):
        session = Session.__new__(Session)
        session.plan_store = PlanStore()
        session.todo_store = TodoStore()
        session.broadcast_bus = _BroadcastBus()
        session._codeindex_available = True
        session._plan_mode_attempt = 0
        session.permission_evaluator = PermissionEvaluator.from_strings(mode="default")
        session.main_team = None  # no spawn target, enter_plan_mode falls through
        return session

    @pytest.mark.asyncio
    async def test_thin_plan_rejected_when_codeindex_available(self):
        """A plan with NO file citations should bounce on the
        first attempt when CodeIndex is available."""
        session = self._session_with_codeindex()
        tool = PlanTool(session)
        result = tool.exit_plan_mode("Refactor the auth thing. It will be great.")
        assert "Plan rejected" in result
        assert "research pass 1/3" in result
        assert "codeindex_query" in result.lower()
        # Plan store NOT mutated on rejection.
        assert session.plan_store.latest == ""
        # Attempt counter incremented.
        assert session._plan_mode_attempt == 1

    @pytest.mark.asyncio
    async def test_grounded_plan_accepted(self):
        """A plan that cites at least 2 specific file paths
        passes the gate on the first try."""
        session = self._session_with_codeindex()
        tool = PlanTool(session)
        result = tool.exit_plan_mode(
            "## Refactor\n\n"
            "Touch `src/ember_code/core/auth/middleware.py` and "
            "`src/ember_code/backend/server.py` to swap session for JWT."
        )
        assert "Plan submitted" in result
        assert "Refactor" in session.plan_store.latest
        # No attempt counter bump on acceptance.
        assert session._plan_mode_attempt == 0

    @pytest.mark.asyncio
    async def test_validation_skipped_without_codeindex(self):
        """When CodeIndex isn't available, the agent
        legitimately can't do as deep a research pass — the
        gate skips so the conversation keeps moving."""
        session = self._session_with_codeindex()
        session._codeindex_available = False  # override
        tool = PlanTool(session)
        result = tool.exit_plan_mode("thin plan, no files cited.")
        assert "Plan submitted" in result
        assert session.plan_store.latest == "thin plan, no files cited."

    @pytest.mark.asyncio
    async def test_attempt_cap_accepts_thin_plan_eventually(self):
        """After ``_MAX_PLAN_ATTEMPTS`` rejections the gate
        gives up and accepts whatever came in — better to
        surface a thin plan to the user (who can refine) than
        infinite-loop the agent."""
        session = self._session_with_codeindex()
        session._plan_mode_attempt = _MAX_PLAN_ATTEMPTS - 1  # last allowed attempt
        tool = PlanTool(session)
        result = tool.exit_plan_mode("Still thin, no citations.")
        # On the last attempt the gate accepts.
        assert "Plan submitted" in result
        assert session.plan_store.latest.startswith("Still thin")

    @pytest.mark.asyncio
    async def test_enter_plan_mode_resets_attempt_counter(self):
        """A fresh ``enter_plan_mode`` starts the validation
        loop clean — even if the previous plan-mode session
        burned through all 3 attempts, the next gets all 3 back."""
        session = self._session_with_codeindex()
        session._plan_mode_attempt = 2  # near the cap
        tool = PlanTool(session)
        await tool.enter_plan_mode("new task")
        assert session._plan_mode_attempt == 0


class TestEnterPlanModeSpawnsResearcher:
    """Path B core: ``enter_plan_mode(task=...)`` spawns the
    plan_researcher sub-agent via OrchestrateTools and returns
    its report."""

    def _session(self):
        session = Session.__new__(Session)
        session.plan_store = PlanStore()
        session.todo_store = TodoStore()
        session.broadcast_bus = _BroadcastBus()
        session.permission_evaluator = PermissionEvaluator.from_strings(mode="default")
        session._plan_mode_attempt = 0
        return session

    @pytest.mark.asyncio
    async def test_no_task_skips_spawn(self):
        """Without ``task=``, ``enter_plan_mode`` falls back to
        the prior behavior (mode flip + return guidance text).
        Lets the agent enter plan mode without forcing a spawn."""
        session = self._session()
        tool = PlanTool(session)
        result = await tool.enter_plan_mode("auth refactor")
        # No researcher REPORT in the reply (the marker the
        # FE looks for when a spawn happened).
        assert "Entered plan mode" in result
        assert "plan_researcher sub-agent report follows" not in result
        # Tip points the agent at next-call usage.
        assert "task=" in result

    @pytest.mark.asyncio
    async def test_task_provided_spawns_researcher(self):
        """With ``task=``, the tool spawns the plan_researcher
        through OrchestrateTools and returns its report in the
        reply."""
        session = self._session()

        # Build a real-shaped fake — use a subclass of OrchestrateTools
        # via ``__class__`` patching so the isinstance check in
        # ``_run_plan_researcher`` matches. spec= would restrict
        # attribute access; we want full mock access.
        fake_orch = MagicMock()
        fake_orch.__class__ = OrchestrateTools
        fake_orch.spawn_agent = AsyncMock(
            return_value="## Codebase Findings\nFound stuff in `src/x.py`",
        )
        fake_orch.pool.get.return_value = MagicMock()  # registered
        team = MagicMock()
        team.tools = [fake_orch]
        session.main_team = team

        tool = PlanTool(session)
        result = await tool.enter_plan_mode("auth refactor", task="Refactor session→JWT")
        assert "plan_researcher sub-agent report follows" in result
        assert "src/x.py" in result
        fake_orch.spawn_agent.assert_awaited_once_with(
            task="Refactor session→JWT", agent_name="plan_researcher"
        )

    @pytest.mark.asyncio
    async def test_spawn_failure_falls_through_gracefully(self):
        """If OrchestrateTools isn't wired or the agent isn't
        registered, the spawn returns empty and ``enter_plan_mode``
        falls back to the "manual research" path — never crashes
        the conversation."""
        session = self._session()
        # No main_team → no OrchestrateTools to find.
        session.main_team = None
        tool = PlanTool(session)
        result = await tool.enter_plan_mode("reason", task="a task")
        # Gracefully degrades — no report, but plan mode IS active.
        assert "Entered plan mode" in result
        assert session.permission_evaluator.mode is PermissionMode.PLAN


class TestTodoWriteBroadcastsState:
    @pytest.mark.asyncio
    async def test_todo_write_broadcasts_todos_updated(self):
        """Every ``todo_write`` call fires a ``todos_updated``
        push so the FE's PlanCard checklist ticks off in place
        as the agent executes."""
        session = Session.__new__(Session)
        session.todo_store = TodoStore()
        session.broadcast_bus = _BroadcastBus()
        captured: list[tuple[str, dict]] = []
        session.register_broadcast_callback(lambda ch, p: captured.append((ch, p)))
        tool = TodoTools(session)
        await tool.todo_write(
            [
                {"content": "A", "status": "in_progress", "activeForm": "Doing A"},
                {"content": "B", "status": "pending"},
            ]
        )
        evt = next(p for ch, p in captured if ch == "todos_updated")
        assert len(evt["todos"]) == 2
        assert evt["todos"][0]["status"] == "in_progress"
        assert evt["todos"][0]["activeForm"] == "Doing A"

    @pytest.mark.asyncio
    async def test_clear_also_broadcasts(self):
        """``todo_write([])`` (clear) ALSO fires the push so the
        FE empties the checklist — otherwise a "clear" mid-
        execution leaves the UI showing stale tasks."""
        session = Session.__new__(Session)
        session.todo_store = TodoStore()
        session.broadcast_bus = _BroadcastBus()
        captured: list[tuple[str, dict]] = []
        session.register_broadcast_callback(lambda ch, p: captured.append((ch, p)))
        tool = TodoTools(session)
        await tool.todo_write([])
        evt = next(p for ch, p in captured if ch == "todos_updated")
        assert evt["todos"] == []


class TestEnterPlanMode:
    """Agent-facing entry into plan mode (the asymmetric half of
    the security envelope — entering is safe to expose, exiting
    isn't)."""

    def _session_with_real_set_mode(self):
        """Build a stand-in session that has a real evaluator +
        the real ``set_permission_mode`` bound. ``PlanTool`` calls
        ``set_permission_mode`` and ``broadcast`` on the session."""
        session = Session.__new__(Session)
        session.permission_evaluator = PermissionEvaluator.from_strings(mode="default")
        session.broadcast_bus = _BroadcastBus()
        session.plan_store = PlanStore()
        return session

    @pytest.mark.asyncio
    async def test_enter_plan_mode_flips_mode_to_plan(self):
        """Calling the tool transitions the live evaluator
        ``default → plan`` — the agent self-disciplines into the
        sandbox before doing work on a complex task."""
        session = self._session_with_real_set_mode()
        tool = PlanTool(session)
        result = await tool.enter_plan_mode("multi-file refactor")
        assert "Entered plan mode" in result
        assert session.permission_evaluator.mode is PermissionMode.PLAN

    @pytest.mark.asyncio
    async def test_enter_plan_mode_reason_in_reply(self):
        session = self._session_with_real_set_mode()
        tool = PlanTool(session)
        result = await tool.enter_plan_mode("auth subsystem touches 5 services")
        assert "auth subsystem" in result

    @pytest.mark.asyncio
    async def test_enter_plan_mode_broadcasts_with_agent_source(self):
        """Both the initial flip AND the follow-up reason-broadcast
        carry ``permission_mode_changed`` on the same channel; the
        follow-up carries ``source: "agent"`` so the FE can render
        the "Agent entered plan mode — <reason>" banner without
        guessing who triggered it."""
        session = self._session_with_real_set_mode()
        captured: list[tuple[str, dict]] = []
        session.register_broadcast_callback(lambda ch, p: captured.append((ch, p)))
        tool = PlanTool(session)
        await tool.enter_plan_mode("refactor sweep")
        # set_permission_mode fires the basic flip; enter_plan_mode
        # follows up with the agent-attributed payload.
        assert any(p.get("source") == "agent" for _, p in captured)
        agent_evt = next(p for _, p in captured if p.get("source") == "agent")
        assert agent_evt["mode"] == "plan"
        assert agent_evt["reason"] == "refactor sweep"

    @pytest.mark.asyncio
    async def test_enter_plan_mode_empty_reason_still_works(self):
        """Reason is optional — the model can call
        ``enter_plan_mode()`` without an argument when the user's
        request already makes the rationale obvious."""
        session = self._session_with_real_set_mode()
        tool = PlanTool(session)
        result = await tool.enter_plan_mode()
        assert "Entered plan mode" in result
        assert session.permission_evaluator.mode is PermissionMode.PLAN

    @pytest.mark.asyncio
    async def test_enter_plan_mode_no_session_set_mode_returns_error(self):
        """Defensive: a session stand-in without
        ``set_permission_mode`` doesn't crash the tool — it
        returns an error string for the agent."""
        session = MagicMock(spec=["plan_store"])
        session.plan_store = PlanStore()
        tool = PlanTool(session)
        result = await tool.enter_plan_mode("x")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_enter_then_exit_keeps_mode_in_plan(self):
        """``exit_plan_mode`` does NOT pair with ``enter_plan_mode``
        to revert — the asymmetric design holds. After the agent
        enters AND submits a plan, the mode is still PLAN until
        the user clicks Approve."""
        session = self._session_with_real_set_mode()
        tool = PlanTool(session)
        await tool.enter_plan_mode("complex")
        tool.exit_plan_mode("step 1, step 2")
        assert session.permission_evaluator.mode is PermissionMode.PLAN


# ── Session.set_permission_mode ───────────────────────────


class TestSetPermissionMode:
    def _session_with_evaluator(self, initial="default"):
        session = Session.__new__(Session)
        session.broadcast_bus = _BroadcastBus()
        session.permission_evaluator = PermissionEvaluator.from_strings(mode=initial)
        return session

    def test_flips_mode_to_plan(self):
        session = self._session_with_evaluator("default")
        msg = session.set_permission_mode("plan")
        assert "default" in msg and "plan" in msg
        assert session.permission_evaluator.mode is PermissionMode.PLAN

    def test_idempotent_when_already_in_mode(self):
        session = self._session_with_evaluator("plan")
        msg = session.set_permission_mode("plan")
        assert "already" in msg.lower()
        assert session.permission_evaluator.mode is PermissionMode.PLAN

    def test_rejects_unknown_mode(self):
        session = self._session_with_evaluator()
        msg = session.set_permission_mode("turbo")
        assert "Error" in msg
        # Mode unchanged.
        assert session.permission_evaluator.mode is PermissionMode.DEFAULT

    def test_no_evaluator_raises_attribute_error(self):
        # Session's public contract requires __init__ to run — the
        # coordinator no longer soft-fails on partial construction.
        # A test session built via ``__new__`` without an evaluator
        # correctly raises rather than pretending to work.
        session = Session.__new__(Session)
        session.broadcast_bus = _BroadcastBus()
        with pytest.raises(AttributeError):
            session.set_permission_mode("plan")


# ── /plan slash command ───────────────────────────────────


class TestPlanSlashCommand:
    def _make_session(self, initial_mode="default"):
        session = Session.__new__(Session)
        session.broadcast_bus = _BroadcastBus()
        session.permission_evaluator = PermissionEvaluator.from_strings(mode=initial_mode)
        # Bind ``set_permission_mode`` since we constructed via
        # __new__ without running __init__.
        return session

    @pytest.mark.asyncio
    async def test_bare_plan_toggles_into_plan_mode(self):
        session = self._make_session("default")
        handler = CommandHandler(session)
        result = await handler.handle("/plan")
        assert "plan mode" in result.content.lower()
        assert session.permission_evaluator.mode is PermissionMode.PLAN

    @pytest.mark.asyncio
    async def test_bare_plan_toggles_back_out(self):
        session = self._make_session("plan")
        handler = CommandHandler(session)
        result = await handler.handle("/plan")
        assert session.permission_evaluator.mode is PermissionMode.DEFAULT
        assert "exited" in result.content.lower() or "default" in result.content.lower()

    @pytest.mark.asyncio
    async def test_plan_on_enables(self):
        session = self._make_session("default")
        handler = CommandHandler(session)
        await handler.handle("/plan on")
        assert session.permission_evaluator.mode is PermissionMode.PLAN

    @pytest.mark.asyncio
    async def test_plan_off_disables(self):
        session = self._make_session("plan")
        handler = CommandHandler(session)
        await handler.handle("/plan off")
        assert session.permission_evaluator.mode is PermissionMode.DEFAULT

    @pytest.mark.asyncio
    async def test_plan_status_does_not_change_mode(self):
        session = self._make_session("plan")
        handler = CommandHandler(session)
        result = await handler.handle("/plan status")
        # Mode unchanged.
        assert session.permission_evaluator.mode is PermissionMode.PLAN
        assert "plan" in result.content.lower()

    @pytest.mark.asyncio
    async def test_unknown_argument_returns_error(self):
        session = self._make_session("default")
        handler = CommandHandler(session)
        result = await handler.handle("/plan turbo")
        assert "Error" in result.content or "Unknown" in result.content
        assert session.permission_evaluator.mode is PermissionMode.DEFAULT

    @pytest.mark.asyncio
    async def test_plan_from_non_plan_non_default_mode_enters_plan(self):
        """From bypassPermissions (or any other non-plan mode),
        bare ``/plan`` enters plan mode. The toggle rule is
        "if you're not in plan, go to plan; otherwise go to
        default" — symmetric for the common cases."""
        session = self._make_session("bypassPermissions")
        handler = CommandHandler(session)
        await handler.handle("/plan")
        assert session.permission_evaluator.mode is PermissionMode.PLAN

    @pytest.mark.asyncio
    async def test_entering_plan_arms_research_nudge(self):
        # The slash command sets ``_plan_research_armed`` so the
        # UserMessage handler can prepend a ``<system-context>``
        # nudge on the next turn — telling the agent to call
        # ``enter_plan_mode(task=...)`` first. Without this, the
        # agent has no signal that the user (not the agent) just
        # entered plan mode and skips the researcher.
        session = self._make_session("default")
        assert getattr(session, "_plan_research_armed", False) is False
        handler = CommandHandler(session)
        await handler.handle("/plan")
        assert session._plan_research_armed is True

    @pytest.mark.asyncio
    async def test_re_entering_plan_does_not_re_arm(self):
        # Already in plan mode → ``/plan`` is a no-op for the
        # mode but must NOT re-arm the nudge. Re-arming would
        # cause a wasted researcher run on the next user message
        # (the researcher already ran the first time).
        session = self._make_session("plan")
        session._plan_research_armed = False  # explicitly disarmed
        handler = CommandHandler(session)
        await handler.handle("/plan on")
        # Still in plan mode AND still not armed.
        assert session.permission_evaluator.mode is PermissionMode.PLAN
        assert session._plan_research_armed is False

    @pytest.mark.asyncio
    async def test_leaving_plan_disarms_pending_nudge(self):
        # User types ``/plan`` then immediately ``/plan off``
        # before sending a follow-up. The pending researcher
        # nudge must clear so a later non-plan-mode message
        # doesn't get a stale ``<system-context>`` injection.
        session = self._make_session("default")
        handler = CommandHandler(session)
        await handler.handle("/plan")
        assert session._plan_research_armed is True
        await handler.handle("/plan off")
        assert session._plan_research_armed is False

    @pytest.mark.asyncio
    async def test_status_command_does_not_change_armed_flag(self):
        # ``/plan status`` is a read-only query — it returns the
        # current mode without flipping anything and must not
        # arm/disarm the researcher nudge either.
        session = self._make_session("plan")
        session._plan_research_armed = True
        handler = CommandHandler(session)
        await handler.handle("/plan status")
        assert session._plan_research_armed is True


class TestAcceptSlashCommand:
    """The ``/accept`` toggle for acceptEdits mode (row 51).
    Mirrors the ``/plan`` shape — toggle / on / off / status."""

    def _make_session(self, initial_mode="default"):
        session = Session.__new__(Session)
        session.permission_evaluator = PermissionEvaluator.from_strings(mode=initial_mode)
        session.broadcast_bus = _BroadcastBus()
        return session

    @pytest.mark.asyncio
    async def test_bare_accept_toggles_into_acceptedits(self):
        session = self._make_session("default")
        handler = CommandHandler(session)
        result = await handler.handle("/accept")
        assert session.permission_evaluator.mode is PermissionMode.ACCEPT_EDITS
        assert "accept" in result.content.lower()

    @pytest.mark.asyncio
    async def test_bare_accept_toggles_back_out(self):
        session = self._make_session("acceptEdits")
        handler = CommandHandler(session)
        result = await handler.handle("/accept")
        assert session.permission_evaluator.mode is PermissionMode.DEFAULT
        assert "exited" in result.content.lower() or "default" in result.content.lower()

    @pytest.mark.asyncio
    async def test_accept_on(self):
        session = self._make_session("default")
        handler = CommandHandler(session)
        await handler.handle("/accept on")
        assert session.permission_evaluator.mode is PermissionMode.ACCEPT_EDITS

    @pytest.mark.asyncio
    async def test_accept_off(self):
        session = self._make_session("acceptEdits")
        handler = CommandHandler(session)
        await handler.handle("/accept off")
        assert session.permission_evaluator.mode is PermissionMode.DEFAULT

    @pytest.mark.asyncio
    async def test_accept_status_does_not_change_mode(self):
        session = self._make_session("acceptEdits")
        handler = CommandHandler(session)
        result = await handler.handle("/accept status")
        # Mode unchanged.
        assert session.permission_evaluator.mode is PermissionMode.ACCEPT_EDITS
        assert "acceptedits" in result.content.lower()

    @pytest.mark.asyncio
    async def test_unknown_argument_returns_error(self):
        session = self._make_session("default")
        handler = CommandHandler(session)
        result = await handler.handle("/accept turbo")
        assert "Error" in result.content or "Unknown" in result.content
        assert session.permission_evaluator.mode is PermissionMode.DEFAULT

    @pytest.mark.asyncio
    async def test_accept_from_plan_mode_enters_acceptedits(self):
        """From any non-acceptEdits mode (including plan), bare
        ``/accept`` enters acceptEdits. Useful for the workflow
        where the user approves a plan and immediately wants
        edits to auto-approve during execution."""
        session = self._make_session("plan")
        handler = CommandHandler(session)
        await handler.handle("/accept")
        assert session.permission_evaluator.mode is PermissionMode.ACCEPT_EDITS

    @pytest.mark.asyncio
    async def test_accept_broadcasts_mode_change(self):
        """The mode flip broadcasts ``permission_mode_changed``
        so the FE badge updates without polling — same
        plumbing as ``/plan``."""
        session = self._make_session("default")
        captured: list[tuple[str, dict]] = []
        session.register_broadcast_callback(lambda ch, p: captured.append((ch, p)))
        handler = CommandHandler(session)
        await handler.handle("/accept on")
        modes = [p for ch, p in captured if ch == "permission_mode_changed"]
        assert any(p.get("mode") == "acceptEdits" for p in modes)


class TestBypassSlashCommand:
    """The ``/bypass`` toggle for bypassPermissions mode — the
    one-click "continue all work without asking" footer switch.
    Mirrors ``/accept`` / ``/plan`` shape (toggle / on / off /
    status) since they're sibling controls over the same
    ``PermissionEvaluator.mode``."""

    def _make_session(self, initial_mode="default"):
        session = Session.__new__(Session)
        session.permission_evaluator = PermissionEvaluator.from_strings(mode=initial_mode)
        session.broadcast_bus = _BroadcastBus()
        return session

    @pytest.mark.asyncio
    async def test_bare_bypass_toggles_into_bypass(self):
        session = self._make_session("default")
        handler = CommandHandler(session)
        result = await handler.handle("/bypass")
        assert session.permission_evaluator.mode is PermissionMode.BYPASS_PERMISSIONS
        assert "bypass" in result.content.lower()

    @pytest.mark.asyncio
    async def test_bare_bypass_toggles_back_out(self):
        session = self._make_session("bypassPermissions")
        handler = CommandHandler(session)
        result = await handler.handle("/bypass")
        assert session.permission_evaluator.mode is PermissionMode.DEFAULT
        assert "exited" in result.content.lower() or "default" in result.content.lower()

    @pytest.mark.asyncio
    async def test_bypass_on(self):
        session = self._make_session("default")
        handler = CommandHandler(session)
        await handler.handle("/bypass on")
        assert session.permission_evaluator.mode is PermissionMode.BYPASS_PERMISSIONS

    @pytest.mark.asyncio
    async def test_bypass_off(self):
        session = self._make_session("bypassPermissions")
        handler = CommandHandler(session)
        await handler.handle("/bypass off")
        assert session.permission_evaluator.mode is PermissionMode.DEFAULT

    @pytest.mark.asyncio
    async def test_bypass_status_does_not_change_mode(self):
        session = self._make_session("bypassPermissions")
        handler = CommandHandler(session)
        result = await handler.handle("/bypass status")
        assert session.permission_evaluator.mode is PermissionMode.BYPASS_PERMISSIONS
        assert "bypasspermissions" in result.content.lower()

    @pytest.mark.asyncio
    async def test_unknown_argument_returns_error(self):
        session = self._make_session("default")
        handler = CommandHandler(session)
        result = await handler.handle("/bypass turbo")
        assert "Error" in result.content or "Unknown" in result.content
        assert session.permission_evaluator.mode is PermissionMode.DEFAULT

    @pytest.mark.asyncio
    async def test_bypass_from_plan_mode_enters_bypass(self):
        """From any non-bypass mode (including plan), bare
        ``/bypass`` enters bypassPermissions — the footer switch
        is meant to be a one-tap escape hatch regardless of where
        you started."""
        session = self._make_session("plan")
        handler = CommandHandler(session)
        await handler.handle("/bypass")
        assert session.permission_evaluator.mode is PermissionMode.BYPASS_PERMISSIONS

    @pytest.mark.asyncio
    async def test_bypass_broadcasts_mode_change(self):
        """The flip broadcasts ``permission_mode_changed`` — that's
        what the FE switch listens for to clear its optimistic
        override and lock in the new state."""
        session = self._make_session("default")
        captured: list[tuple[str, dict]] = []
        session.register_broadcast_callback(lambda ch, p: captured.append((ch, p)))
        handler = CommandHandler(session)
        await handler.handle("/bypass on")
        modes = [p for ch, p in captured if ch == "permission_mode_changed"]
        assert any(p.get("mode") == "bypassPermissions" for p in modes)

    @pytest.mark.asyncio
    async def test_bypass_registered_in_dispatch_table(self):
        """``/bypass`` must be in the builtin registry so the slash
        dispatcher routes it. Catches a wiring regression even
        if the method itself is correct."""
        assert "bypass" in CommandHandler.builtin_names()


# ── GET_LATEST_PLAN RPC ───────────────────────────────────


class TestGetLatestPlanRpc:
    def test_returns_empty_when_no_plan(self):
        # The RPC carries ``tasks`` (from the todo store) and
        # ``state`` for restore. State no longer infers from
        # permission mode — see ``test_get_latest_plan_pending_when_plan_present``
        # in test_plan_rehydrate.py for the new semantics.
        session = MagicMock(spec=["plan_store", "todo_store"])
        session.plan_store = PlanStore()
        session.todo_store = None
        backend = BackendServer.__new__(BackendServer)
        backend._session = session
        snap = backend.get_latest_plan()
        assert snap.latest == ""
        assert snap.history == []
        assert snap.tasks == []
        # No plan → state is empty (nothing to render).
        assert snap.state == ""

    def test_returns_latest_and_history(self):
        session = MagicMock()
        session.plan_store = PlanStore()
        session.plan_store.set_plan("first plan")
        session.plan_store.set_plan("second plan")
        backend = BackendServer.__new__(BackendServer)
        backend._session = session
        snap = backend.get_latest_plan()
        assert snap.latest == "second plan"
        assert snap.history == ["first plan"]

    def test_no_plan_store_does_not_crash(self):
        """Defensive: legacy / partially-initialised sessions
        without a ``plan_store`` attribute must return empty
        rather than crash."""
        session = MagicMock(spec=[])  # no attributes
        backend = BackendServer.__new__(BackendServer)
        backend._session = session
        snap = backend.get_latest_plan()
        assert snap.latest == ""
        assert snap.history == []
        assert snap.tasks == []
        assert snap.state == ""

    def test_dispatch_table_routes_get_latest_plan(self):
        session = MagicMock()
        session.plan_store = PlanStore()
        session.plan_store.set_plan("test plan")
        backend = BackendServer.__new__(BackendServer)
        backend._session = session

        table = _build_rpc_table(backend, transport=MagicMock(), login_state={})
        handler = table.get(RpcMethod.GET_LATEST_PLAN)
        assert handler is not None
        result = handler({})
        assert result.latest == "test plan"


# ── End-to-end: plan-mode lifecycle ───────────────────────


class TestPlanModeLifecycle:
    @pytest.mark.asyncio
    async def test_full_flow_toggle_in_submit_toggle_out(self):
        """Enter plan mode via ``/plan``, submit a plan via the
        tool, then exit via ``/plan``. Verifies the three pieces
        work as one workflow."""
        session = Session.__new__(Session)
        session.broadcast_bus = _BroadcastBus()
        session.permission_evaluator = PermissionEvaluator.from_strings(mode="default")
        session.plan_store = PlanStore()
        handler = CommandHandler(session)

        await handler.handle("/plan")
        assert session.permission_evaluator.mode is PermissionMode.PLAN

        tool = PlanTool(session)
        tool.exit_plan_mode("1. Read spec\n2. Implement\n3. Test")
        # Plan stored, mode UNCHANGED (agent can't exit on its own).
        assert session.plan_store.latest == "1. Read spec\n2. Implement\n3. Test"
        assert session.permission_evaluator.mode is PermissionMode.PLAN

        await handler.handle("/plan off")
        assert session.permission_evaluator.mode is PermissionMode.DEFAULT
        # And the plan is still in the store for the user / UI.
        assert session.plan_store.latest == "1. Read spec\n2. Implement\n3. Test"
