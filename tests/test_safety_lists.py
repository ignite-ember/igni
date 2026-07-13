"""Unit tests for the safety-list helpers extracted from ``tool_hook.py``.

The ToolEventHook integration is covered by
``test_tool_hook_protected_paths.py`` + ``test_bypass_resistant_e2e.py``.
These tests pin the pure-function contracts of the helpers in
isolation so a future refactor of ``tool_hook.py`` can't silently
drift them.
"""

from ember_code.core.hooks.safety_lists import (
    _is_protected_path,
    check_blocked_commands,
    check_protected_paths,
)


class TestCheckProtectedPaths:
    def test_empty_list_returns_none(self):
        assert check_protected_paths("save_file", {"file_path": ".env"}, []) is None

    def test_non_write_tool_returns_none(self):
        # ``read_file`` is not a write function — the guard's whole
        # point is to gate *writes* to protected paths.
        assert check_protected_paths("read_file", {"file_path": ".env"}, [".env"]) is None

    def test_no_file_path_arg_returns_none(self):
        assert check_protected_paths("save_file", {}, [".env"]) is None

    def test_protected_write_returns_block_message(self):
        msg = check_protected_paths("save_file", {"file_path": ".env"}, [".env"])
        assert msg is not None
        assert ".env" in msg
        assert "protected" in msg.lower()

    def test_glob_pattern_matches(self):
        msg = check_protected_paths(
            "edit_file",
            {"file_path": ".env.production"},
            [".env.*"],
        )
        assert msg is not None

    def test_full_path_pattern_matches(self):
        msg = check_protected_paths(
            "save_file",
            {"file_path": "/project/.env"},
            [".env"],
        )
        assert msg is not None

    def test_all_write_functions_gated(self):
        # Every write-tool function must trip the guard when the
        # path matches. Guards the invariant that adding a new
        # write tool requires updating the frozenset.
        for tool in ("save_file", "edit_file", "edit_file_replace_all", "create_file"):
            assert (
                check_protected_paths(tool, {"file_path": ".env"}, [".env"]) is not None
            ), f"{tool} should be gated by protected_paths"


class TestCheckBlockedCommands:
    def test_empty_list_returns_none(self):
        assert check_blocked_commands("run_shell_command", {"args": ["rm", "-rf", "/"]}, []) is None

    def test_non_shell_tool_returns_none(self):
        assert (
            check_blocked_commands("save_file", {"args": ["rm", "-rf", "/"]}, ["rm -rf /"])
            is None
        )

    def test_matching_command_returns_block_message(self):
        msg = check_blocked_commands(
            "run_shell_command",
            {"args": ["rm", "-rf", "/"]},
            ["rm -rf /"],
        )
        assert msg is not None
        assert "rm -rf /" in msg

    def test_substring_match(self):
        # The check is substring-in-joined-args — a blocked
        # pattern anywhere in the command trips the guard.
        msg = check_blocked_commands(
            "run_shell_command",
            {"args": ["bash", "-c", "cd /tmp && rm -rf /"]},
            ["rm -rf /"],
        )
        assert msg is not None

    def test_string_args_supported(self):
        # Some callers pass a single string instead of a list.
        msg = check_blocked_commands(
            "run_shell_command",
            {"args": "rm -rf /"},
            ["rm -rf /"],
        )
        assert msg is not None

    def test_no_match_returns_none(self):
        assert (
            check_blocked_commands(
                "run_shell_command",
                {"args": ["ls", "-la"]},
                ["rm -rf /"],
            )
            is None
        )


class TestIsProtectedPathReExport:
    """``_is_protected_path`` is still importable from ``safety_lists``
    — every existing test file imports it from ``tool_hook``, but
    new callers may reach for the safety-lists module directly."""

    def test_reexport_matches_expected(self):
        assert _is_protected_path(".env", [".env"]) is True
        assert _is_protected_path("app.py", [".env"]) is False
