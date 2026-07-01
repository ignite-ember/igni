"""Tests for tui/hitl_handler.py — HITL pure functions and permission flow."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ember_code.frontend.tui.hitl_handler import (
    HITLHandler,
    _build_pattern_rule,
    _build_rule,
    _format_args_detail,
    _format_args_short,
)

# ── Pure function tests: _format_args_short ────────────────────


class TestFormatArgsShort:
    def test_shell_command(self):
        result = _format_args_short({"args": ["git", "status"]})
        assert result == "git status"

    def test_shell_command_single(self):
        result = _format_args_short({"args": ["ls"]})
        assert result == "ls"

    def test_path_key(self):
        result = _format_args_short({"path": "/tmp/foo.py"})
        assert result == "/tmp/foo.py"

    def test_file_path_key(self):
        result = _format_args_short({"file_path": "src/main.py"})
        assert result == "src/main.py"

    def test_url_key(self):
        result = _format_args_short({"url": "https://example.com"})
        assert result == "https://example.com"

    def test_query_key(self):
        result = _format_args_short({"query": "search term"})
        assert result == "search term"

    def test_priority_order(self):
        result = _format_args_short({"path": "/a", "url": "http://b"})
        assert result == "/a"

    def test_fallback_to_str(self):
        result = _format_args_short({"custom": "value"})
        assert "custom" in result


# ── Pure function tests: _format_args_detail ───────────────────


class TestFormatArgsDetail:
    def test_shell_command(self):
        result = _format_args_detail({"args": ["git", "push", "--force"]})
        assert result == "$ git push --force"

    def test_file_path(self):
        result = _format_args_detail({"file_path": "src/main.py"})
        assert result == "src/main.py"

    def test_path(self):
        result = _format_args_detail({"path": "/tmp/test"})
        assert result == "/tmp/test"

    def test_mixed_args(self):
        result = _format_args_detail({"key1": "val1", "key2": "val2"})
        assert "key1" in result
        assert "key2" in result


# ── Pure function tests: _build_rule ───────────────────────────


class TestBuildRule:
    def test_with_shell_args(self):
        result = _build_rule("Bash", {"args": ["git", "status"]})
        assert result == "Bash(git status)"

    def test_with_path(self):
        result = _build_rule("Edit", {"file_path": "src/main.py"})
        assert result == "Edit(src/main.py)"

    def test_empty_args(self):
        result = _build_rule("Bash", {})
        assert result.startswith("Bash")

    def test_with_url(self):
        result = _build_rule("WebFetch", {"url": "https://example.com"})
        assert result == "WebFetch(https://example.com)"


# ── Pure function tests: _build_pattern_rule ───────────────────


class TestBuildPatternRule:
    # v0.8.2 changed the emitted format from ``Bash(git:*)`` /
    # ``Edit(path:src/*)`` to ``Bash(git *)`` / ``Edit(src/*)`` so
    # the running session's PermissionEvaluator (raw fnmatch) can
    # match them. The colon-prefix form only worked under the
    # legacy ``ToolPermissions._match_rule_args`` code path, which
    # is why "Allow similar" clicked in the web dialog kept
    # re-prompting on the next call.
    def test_shell_pattern(self):
        result = _build_pattern_rule("Bash", {"args": ["git", "push"]})
        assert result == "Bash(git *)"

    def test_shell_single_arg(self):
        result = _build_pattern_rule("Bash", {"args": ["npm"]})
        assert result == "Bash(npm *)"

    def test_shell_empty_args_list(self):
        result = _build_pattern_rule("Bash", {"args": []})
        assert result == "Bash"

    def test_file_path_pattern(self):
        result = _build_pattern_rule("Edit", {"file_path": "src/ember_code/tui/app.py"})
        assert result == "Edit(src/ember_code/tui/*)"

    def test_path_pattern(self):
        result = _build_pattern_rule("Read", {"path": "/home/user/project/main.py"})
        assert result == "Read(/home/user/project/*)"

    def test_path_root_file(self):
        result = _build_pattern_rule("Read", {"path": "file.py"})
        assert result == "Read"

    def test_url_domain_pattern(self):
        result = _build_pattern_rule("WebFetch", {"url": "https://api.example.com/v1/data"})
        assert result == "WebFetch(domain:api.example.com)"

    def test_url_no_domain(self):
        result = _build_pattern_rule("WebFetch", {"url": "not-a-url"})
        assert result == "WebFetch"

    def test_no_matching_keys(self):
        result = _build_pattern_rule("Custom", {"custom_key": "value"})
        assert result == "Custom"

    def test_args_not_list_falls_through(self):
        result = _build_pattern_rule("Bash", {"args": "string", "path": "src/foo.py"})
        assert "src/*" in result


# ── HITLHandler.handle_protocol tests (RPC-based) ─────────────


class TestHandleProtocol:
    @pytest.mark.asyncio
    async def test_auto_allow_by_rpc(self):
        """If BE RPC returns 'allow', auto-confirm."""
        app = MagicMock()
        app.backend._rpc = AsyncMock(return_value="allow")

        handler = HITLHandler(app=app, conversation=MagicMock())
        req = MagicMock()
        req.friendly_name = "Read"
        req.tool_name = "read_file"
        req.tool_args = {"file_path": "test.py"}

        action, choice = await handler.handle_protocol(req)
        assert action == "confirm"
        assert choice == "once"

    @pytest.mark.asyncio
    async def test_auto_deny_by_rpc(self):
        """If BE RPC returns 'deny', auto-reject."""
        app = MagicMock()
        app.backend._rpc = AsyncMock(return_value="deny")

        handler = HITLHandler(app=app, conversation=MagicMock())
        req = MagicMock()
        req.friendly_name = "Bash"
        req.tool_name = "run_shell_command"
        req.tool_args = {"args": ["rm", "-rf"]}

        action, choice = await handler.handle_protocol(req)
        assert action == "reject"

    @pytest.mark.asyncio
    async def test_session_approval_skips_dialog(self):
        """If tool was approved in this session, auto-confirm."""
        app = MagicMock()
        app.backend._rpc = AsyncMock(return_value="ask")

        handler = HITLHandler(app=app, conversation=MagicMock())
        handler._session_approvals.add("Bash(git status)")

        req = MagicMock()
        req.friendly_name = "Bash"
        req.tool_name = "run_shell_command"
        req.tool_args = {"args": ["git", "status"]}

        action, choice = await handler.handle_protocol(req)
        assert action == "confirm"

    @pytest.mark.asyncio
    async def test_dialog_approve_once(self):
        """Dialog approve with 'once' adds to session approvals."""
        app = MagicMock()
        app.backend._rpc = AsyncMock(return_value="ask")
        app.mount = AsyncMock()

        dialog_mock = MagicMock()
        dialog_mock.wait_for_decision = AsyncMock(return_value=True)
        dialog_mock.last_choice = "once"

        handler = HITLHandler(app=app, conversation=MagicMock())

        req = MagicMock()
        req.friendly_name = "Write"
        req.tool_name = "save_file"
        req.tool_args = {"file_path": "test.py"}

        with patch(
            "ember_code.frontend.tui.hitl_handler.PermissionDialog",
            return_value=dialog_mock,
        ):
            action, choice = await handler.handle_protocol(req)

        assert action == "confirm"
        assert choice == "once"
        assert "Write(test.py)" in handler._session_approvals

    @pytest.mark.asyncio
    async def test_dialog_approve_always_saves_rule(self):
        """Dialog approve with 'always' persists via RPC."""
        app = MagicMock()
        app.backend._rpc = AsyncMock(return_value="ask")
        app.mount = AsyncMock()

        dialog_mock = MagicMock()
        dialog_mock.wait_for_decision = AsyncMock(return_value=True)
        dialog_mock.last_choice = "always"

        handler = HITLHandler(app=app, conversation=MagicMock())

        req = MagicMock()
        req.friendly_name = "Bash"
        req.tool_name = "run_shell_command"
        req.tool_args = {"args": ["git", "push"]}

        with patch(
            "ember_code.frontend.tui.hitl_handler.PermissionDialog",
            return_value=dialog_mock,
        ):
            action, choice = await handler.handle_protocol(req)

        assert action == "confirm"
        assert choice == "always"
        # Verify RPC was called to save the rule
        app.backend._rpc.assert_any_call(
            "save_permission_rule", rule="Bash(git push)", level="allow"
        )

    @pytest.mark.asyncio
    async def test_dialog_deny_saves_rule(self):
        """Dialog deny saves deny rule via RPC."""
        app = MagicMock()
        app.backend._rpc = AsyncMock(return_value="ask")
        app.mount = AsyncMock()

        dialog_mock = MagicMock()
        dialog_mock.wait_for_decision = AsyncMock(return_value=False)

        handler = HITLHandler(app=app, conversation=MagicMock())

        req = MagicMock()
        req.friendly_name = "Bash"
        req.tool_name = "run_shell_command"
        req.tool_args = {"args": ["rm", "-rf", "/tmp"]}

        with patch(
            "ember_code.frontend.tui.hitl_handler.PermissionDialog",
            return_value=dialog_mock,
        ):
            action, choice = await handler.handle_protocol(req)

        assert action == "reject"
        assert choice == "deny"
        app.backend._rpc.assert_any_call(
            "save_permission_rule", rule="Bash(rm -rf /tmp)", level="deny"
        )

    @pytest.mark.asyncio
    async def test_rpc_failure_falls_through_to_ask(self):
        """If RPC fails, fall through to showing dialog."""
        app = MagicMock()
        app.backend._rpc = AsyncMock(side_effect=Exception("connection lost"))
        app.mount = AsyncMock()

        dialog_mock = MagicMock()
        dialog_mock.wait_for_decision = AsyncMock(return_value=True)
        dialog_mock.last_choice = "once"

        handler = HITLHandler(app=app, conversation=MagicMock())

        req = MagicMock()
        req.friendly_name = "Bash"
        req.tool_name = "run_shell_command"
        req.tool_args = {"args": ["ls"]}

        with patch(
            "ember_code.frontend.tui.hitl_handler.PermissionDialog",
            return_value=dialog_mock,
        ):
            action, choice = await handler.handle_protocol(req)

        assert action == "confirm"
