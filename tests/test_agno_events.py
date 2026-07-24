"""Tests for the Agno-event → TUI-string translation layer.

The two pure surfaces worth pinning:

  * :meth:`AgnoToolEventFormatter.args_summary` (formerly the free
    ``format_tool_args``) — generates the one-line argument summary
    that lands in the tool-call header. Gets called on every tool
    invocation; bugs here are LOUD (the header is the first thing
    the user sees when a tool fires) but easy to introduce
    silently — wrong arg key, missed truncation, leaked markdown.

  * :attr:`ToolCallFormatterRegistry.friendly_names` (formerly the
    module-level ``TOOL_NAMES`` dict) — friendly-name map that
    turns ``run_shell_command`` into ``Bash`` etc. The TUI shows
    the friendly name; if a tool drops out of the map the header
    reverts to the snake_case internal name, which reads as a
    regression.
"""

from __future__ import annotations

from ember_code.protocol.agno_tool_formatter import AgnoToolEventFormatter

# Shared coordinator + friendly-name view — one instance per test
# module is enough since neither has mutable per-call state.
_formatter = AgnoToolEventFormatter()
TOOL_NAMES = _formatter.registry.friendly_names


def format_tool_args(tool_args, tool_name: str = ""):
    """Shim onto :meth:`AgnoToolEventFormatter.args_summary`.

    Keeps the test bodies unchanged post-refactor while proving
    the class method matches the pre-refactor free-function
    contract byte-for-byte.
    """
    return _formatter.args_summary(tool_name, tool_args)


class TestFormatToolArgsSentinels:
    """Empty / None / non-dict inputs must not crash. The function is
    in the hot path of tool rendering — a TypeError here would kill
    the streaming display."""

    def test_none_returns_empty(self):
        assert format_tool_args(None) == ""

    def test_empty_dict_returns_empty(self):
        assert format_tool_args({}) == ""

    def test_non_dict_returns_empty(self):
        # Defensive — Agno sometimes carries unexpected shapes.
        # We don't want a list of args to render as ``,1,2,3``.
        assert format_tool_args([1, 2, 3]) == ""  # type: ignore[arg-type]
        assert format_tool_args("a string") == ""  # type: ignore[arg-type]


class TestFormatToolArgsGeneric:
    """Default path — flat key=value list, capped at 3 entries with
    30-char value truncation."""

    def test_one_key(self):
        assert format_tool_args({"path": "src/x.py"}) == "path=src/x.py"

    def test_multiple_keys_join_with_comma(self):
        out = format_tool_args({"a": "1", "b": "2"})
        assert out == "a=1, b=2"

    def test_caps_at_three_keys(self):
        # The tool-call header has limited screen real estate;
        # showing more than 3 args would either truncate the
        # tail or wrap into multi-line. Cap at 3 to keep the
        # display compact.
        out = format_tool_args({"a": "1", "b": "2", "c": "3", "d": "4", "e": "5"})
        # Dict iteration order is insertion order in 3.7+, so
        # we get the first three.
        assert out == "a=1, b=2, c=3"
        assert "d" not in out
        assert "e" not in out

    def test_long_value_truncated_with_ellipsis(self):
        # Values over 30 chars get cut. The threshold is the
        # display-friendly width — long values would otherwise
        # blow out the header line.
        long_val = "x" * 100
        out = format_tool_args({"k": long_val})
        # Format: ``k=`` (2 chars) + ``xxxxxxxxxxxxxxxxxxxxxxxxxxx`` (27 chars) + ``...``
        assert out == "k=" + "x" * 27 + "..."
        # And the slice is exactly the documented cap.
        assert len(out) == 2 + 27 + 3

    def test_short_value_not_truncated(self):
        # The 30-char cap shouldn't fire on values just below.
        # Pin the exact 30-char boundary so a refactor to ``>=``
        # vs ``>`` is caught.
        boundary = "x" * 30
        out = format_tool_args({"k": boundary})
        assert out == f"k={boundary}"
        assert "..." not in out

    def test_non_string_values_stringified(self):
        # Numbers, bools, etc. are coerced via ``str(v)``. The
        # user sees the same value the agent passed.
        out = format_tool_args({"count": 42, "enabled": True})
        assert "count=42" in out
        assert "enabled=True" in out


class TestFormatToolArgsSpawnAgent:
    """``spawn_agent`` / ``spawn_team`` get a bespoke summary —
    the agent typically passes a multi-paragraph markdown brief as
    the ``task`` arg, which would drown the activity log if shown
    verbatim."""

    def test_includes_agent_name(self):
        out = format_tool_args(
            {"agent_name": "test-runner", "task": "Run the tests"},
            tool_name="spawn_agent",
        )
        assert "test-runner" in out

    def test_spawn_team_joins_agent_names_list(self):
        # spawn_team's contract is ``agent_names`` as a LIST. The
        # previous version of this code did ``parts = [agent]``
        # where ``agent`` was the list directly, then crashed in
        # ``", ".join(parts)`` with a TypeError. Pin the list-
        # coercion so a future refactor can't reintroduce the
        # crash.
        out = format_tool_args(
            {"agent_names": ["alpha", "beta"], "task": "Coordinate"},
            tool_name="spawn_team",
        )
        assert "alpha" in out and "beta" in out
        assert '"Coordinate"' in out

    def test_spawn_team_with_string_agent_names_still_works(self):
        # Defensive — if Agno or a caller passes ``agent_names``
        # as a plain string (rare but possible), don't blow up
        # trying to iterate it character by character.
        out = format_tool_args(
            {"agent_names": "solo", "task": "x"},
            tool_name="spawn_agent",
        )
        assert "solo" in out

    def test_includes_mode_when_present(self):
        # ``mode=route`` / ``mode=coordinate`` etc. is meaningful
        # context. Pin it gets surfaced.
        out = format_tool_args(
            {"agent_name": "x", "task": "y", "mode": "route"},
            tool_name="spawn_agent",
        )
        assert "mode=route" in out

    def test_mode_omitted_when_falsy(self):
        # Empty / None mode should NOT render as ``mode=``.
        out = format_tool_args(
            {"agent_name": "x", "task": "y", "mode": ""},
            tool_name="spawn_agent",
        )
        assert "mode=" not in out

    def test_task_collapses_to_first_non_empty_line(self):
        # Multi-paragraph markdown → first non-empty line wins.
        # Leading blank lines + headings are common and must
        # not produce a blank quoted snippet.
        task = "\n\n## Heading\n\nReal content here\n\nmore"
        out = format_tool_args(
            {"agent_name": "x", "task": task},
            tool_name="spawn_agent",
        )
        # The first non-empty line is the heading.
        assert '"## Heading"' in out

    def test_long_first_line_truncated_at_80_chars(self):
        # The first-line cap is 80 chars (with ellipsis tail).
        # Headers > 80 chars wrap; the truncation keeps the
        # header on one row.
        long_line = "x" * 200
        out = format_tool_args(
            {"agent_name": "n", "task": long_line},
            tool_name="spawn_agent",
        )
        # Find the quoted snippet.
        quote_start = out.find('"')
        quote_end = out.rfind('"')
        snippet = out[quote_start + 1 : quote_end]
        assert len(snippet) <= 80
        assert snippet.endswith("...")

    def test_short_task_not_truncated(self):
        out = format_tool_args(
            {"agent_name": "n", "task": "short ask"},
            tool_name="spawn_agent",
        )
        assert '"short ask"' in out
        assert "..." not in out

    def test_omits_quoted_task_when_empty(self):
        # Empty / whitespace-only task → no quoted snippet at
        # the end. Showing ``""`` would just be visual noise.
        out = format_tool_args({"agent_name": "n", "task": ""}, tool_name="spawn_agent")
        assert '""' not in out
        # Still surfaces the agent name though.
        assert "n" in out


class TestToolNamesContract:
    """The friendly-name map. Each entry turns a snake_case Agno
    tool name into the CC-style display name the TUI uses."""

    def test_core_filesystem_tools_mapped(self):
        # Filesystem tools the user sees on every session. If
        # any of these drop from the map, the header reverts to
        # the snake_case name — reads as a regression.
        assert TOOL_NAMES["read_file"] == "Read"
        assert TOOL_NAMES["save_file"] == "Write"
        assert TOOL_NAMES["edit_file"] == "Edit"
        assert TOOL_NAMES["edit_file_replace_all"] == "Edit"
        assert TOOL_NAMES["create_file"] == "Write"

    def test_shell_mapped(self):
        assert TOOL_NAMES["run_shell_command"] == "Bash"

    def test_search_tools_mapped(self):
        # All grep variants collapse to ``Grep``; ``glob_files``
        # → ``Glob``. The collapse is intentional — three
        # separate "Grep (count)", "Grep (files)" labels in the
        # header would be cluttered.
        assert TOOL_NAMES["grep"] == "Grep"
        assert TOOL_NAMES["grep_files"] == "Grep"
        assert TOOL_NAMES["grep_count"] == "Grep"
        assert TOOL_NAMES["glob_files"] == "Glob"

    def test_web_tools_collapse_to_websearch_webfetch(self):
        # DuckDuckGo search/news both render as "WebSearch";
        # fetch_url/fetch_json both render as "WebFetch".
        # Mirrors Claude Code's two web-tool surfaces.
        assert TOOL_NAMES["duckduckgo_search"] == "WebSearch"
        assert TOOL_NAMES["duckduckgo_news"] == "WebSearch"
        assert TOOL_NAMES["fetch_url"] == "WebFetch"
        assert TOOL_NAMES["fetch_json"] == "WebFetch"

    def test_orchestration_tools_mapped(self):
        # spawn_agent → Agent / spawn_team → Team / delegate →
        # Delegate. The format_tool_args spawn-special-case
        # only fires for spawn_agent and spawn_team — make sure
        # the friendly-name map agrees.
        assert TOOL_NAMES["spawn_agent"] == "Agent"
        assert TOOL_NAMES["spawn_team"] == "Team"
        assert TOOL_NAMES["delegate_task_to_member"] == "Delegate"
        assert TOOL_NAMES["delegate_task_to_members"] == "Delegate"

    def test_subsystem_tools_mapped(self):
        # Knowledge / Memory / Schedule — these are the rows
        # in the ToolKindFilter that the user can toggle.
        assert TOOL_NAMES["search_knowledge_base"] == "Knowledge"
        assert TOOL_NAMES["update_user_memory"] == "Memory"
        assert TOOL_NAMES["schedule_task"] == "Schedule"
        assert TOOL_NAMES["list_scheduled_tasks"] == "Schedule"
        assert TOOL_NAMES["cancel_scheduled_task"] == "Schedule"

    def test_all_display_names_are_titlecase_one_word(self):
        # House-style: each friendly name is a single TitleCase
        # token (``Read`` / ``Bash`` / ``WebSearch``). Spaces
        # would break terminal-column alignment in the activity
        # log.
        for tool_name, display in TOOL_NAMES.items():
            assert " " not in display, f"{tool_name}: {display!r} has a space"
            assert display[0].isupper(), f"{tool_name}: {display!r} not TitleCase"

    def test_lookup_misses_return_none(self):
        # The dict is queried with ``.get`` at call sites;
        # unknown tools fall through and the TUI uses the
        # snake_case name verbatim. Pin that no shadow default
        # value sneaks in.
        assert TOOL_NAMES.get("not_a_real_tool") is None
