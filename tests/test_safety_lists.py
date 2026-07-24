"""Unit tests for the safety-list stages on the permission pipeline.

The ToolEventHook integration is covered by
``test_tool_hook_protected_paths.py`` + ``test_bypass_resistant_e2e.py``.
These tests pin the pure :meth:`check_sync` contracts on
:class:`ProtectedPathStage` / :class:`BlockedCommandStage` in
isolation so a future refactor of the pipeline can't silently drift
them.
"""

from ember_code.core.hooks.permission_pipeline import (
    BlockedCommandStage,
    ProtectedPathStage,
    ToolCallContext,
)


class TestProtectedPathStageCheckSync:
    def test_empty_list_returns_no_block(self):
        stage = ProtectedPathStage([])
        result = stage.check_sync(ToolCallContext.for_check("save_file", {"file_path": ".env"}))
        assert result.ok
        assert not result.blocked

    def test_non_write_tool_returns_no_block(self):
        # ``read_file`` is not a write function — the guard's whole
        # point is to gate *writes* to protected paths.
        stage = ProtectedPathStage([".env"])
        result = stage.check_sync(ToolCallContext.for_check("read_file", {"file_path": ".env"}))
        assert result.ok

    def test_no_file_path_arg_returns_no_block(self):
        stage = ProtectedPathStage([".env"])
        result = stage.check_sync(ToolCallContext.for_check("save_file", {}))
        assert result.ok

    def test_protected_write_returns_block_message(self):
        stage = ProtectedPathStage([".env"])
        result = stage.check_sync(ToolCallContext.for_check("save_file", {"file_path": ".env"}))
        assert result.blocked
        assert ".env" in result.block_message
        assert "protected" in result.block_message.lower()

    def test_glob_pattern_matches(self):
        stage = ProtectedPathStage([".env.*"])
        result = stage.check_sync(
            ToolCallContext.for_check("edit_file", {"file_path": ".env.production"})
        )
        assert result.blocked

    def test_full_path_pattern_matches(self):
        stage = ProtectedPathStage([".env"])
        result = stage.check_sync(
            ToolCallContext.for_check("save_file", {"file_path": "/project/.env"})
        )
        assert result.blocked

    def test_all_write_functions_gated(self):
        # Every write-tool function must trip the guard when the
        # path matches. Guards the invariant that adding a new
        # write tool requires updating the frozenset.
        stage = ProtectedPathStage([".env"])
        for tool in ProtectedPathStage.WRITE_TOOL_FUNCTIONS:
            result = stage.check_sync(ToolCallContext.for_check(tool, {"file_path": ".env"}))
            assert result.blocked, f"{tool} should be gated by protected_paths"

    def test_applies_to_write_tools(self):
        # The ``applies_to`` classmethod encapsulates the
        # ``in WRITE_TOOL_FUNCTIONS`` membership test.
        for tool in ProtectedPathStage.WRITE_TOOL_FUNCTIONS:
            assert ProtectedPathStage.applies_to(tool)
        assert not ProtectedPathStage.applies_to("read_file")
        assert not ProtectedPathStage.applies_to("run_shell_command")


class TestBlockedCommandStageCheckSync:
    def test_empty_list_returns_no_block(self):
        stage = BlockedCommandStage([])
        result = stage.check_sync(
            ToolCallContext.for_check("run_shell_command", {"args": ["rm", "-rf", "/"]})
        )
        assert result.ok

    def test_non_shell_tool_returns_no_block(self):
        stage = BlockedCommandStage(["rm -rf /"])
        result = stage.check_sync(
            ToolCallContext.for_check("save_file", {"args": ["rm", "-rf", "/"]})
        )
        assert result.ok

    def test_matching_command_returns_block_message(self):
        stage = BlockedCommandStage(["rm -rf /"])
        result = stage.check_sync(
            ToolCallContext.for_check("run_shell_command", {"args": ["rm", "-rf", "/"]})
        )
        assert result.blocked
        assert "rm -rf /" in result.block_message

    def test_substring_match(self):
        # The check is substring-in-joined-args — a blocked
        # pattern anywhere in the command trips the guard.
        stage = BlockedCommandStage(["rm -rf /"])
        result = stage.check_sync(
            ToolCallContext.for_check(
                "run_shell_command",
                {"args": ["bash", "-c", "cd /tmp && rm -rf /"]},
            )
        )
        assert result.blocked

    def test_string_args_supported(self):
        # Some callers pass a single string instead of a list.
        stage = BlockedCommandStage(["rm -rf /"])
        result = stage.check_sync(
            ToolCallContext.for_check("run_shell_command", {"args": "rm -rf /"})
        )
        assert result.blocked

    def test_no_match_returns_no_block(self):
        stage = BlockedCommandStage(["rm -rf /"])
        result = stage.check_sync(
            ToolCallContext.for_check("run_shell_command", {"args": ["ls", "-la"]})
        )
        assert result.ok

    def test_applies_to_shell_tools(self):
        for tool in BlockedCommandStage.SHELL_TOOL_FUNCTIONS:
            assert BlockedCommandStage.applies_to(tool)
        assert not BlockedCommandStage.applies_to("save_file")
        assert not BlockedCommandStage.applies_to("read_file")


class TestProtectedPathStageMatchesPattern:
    """``ProtectedPathStage.matches_pattern`` is the pure static
    predicate the ``_is_protected_path`` shim used to wrap.
    Kept on the class so external callers reach the OOP surface
    directly."""

    def test_matches_expected(self):
        assert ProtectedPathStage.matches_pattern(".env", [".env"]) is True
        assert ProtectedPathStage.matches_pattern("app.py", [".env"]) is False
