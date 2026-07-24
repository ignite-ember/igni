"""Tests for :meth:`AgnoToolEventFormatter.extract_result` — the
summary-computation branch (post-diff path).

The method builds a ``ToolResultData`` for any non-Edit tool
event. Visible fields:

  * ``summary`` — one-line label shown on the collapsed tool
    card. Truncated to 80 chars + "..." for single-line
    results; "N lines of output" for multi-line; "completed in
    <timing>" fallback when there's no content to summarize.
  * ``full_result`` — the unmodified result text (for the
    expanded view).

The ``test_tool_error_rendering.py`` file covers the
error-detection path. This file covers the summary computation
itself plus the MCP-tool ``None``/``null``/``undefined`` string
normalization.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from ember_code.protocol.agno_tool_formatter import AgnoToolEventFormatter

# One formatter instance drives every test — no diff renderer is
# injected because these tests deliberately exercise the non-diff
# branch (the diff branch has its own test module).
_formatter = AgnoToolEventFormatter()


def extract_result(event: Any):
    """Shim keeping the pre-refactor call sites unchanged.

    The migration moved the free function onto a coordinator
    class; wrapping it here means the assertions below stay
    focused on behavior, not on API rewiring.
    """
    return _formatter.extract_result(event)


def _event(
    tool_name: str = "shell",
    result: Any = "",
    duration: float | None = None,
    tool_args: dict | None = None,
) -> Any:
    """Build a minimal Agno-event stand-in for extract_result."""
    metrics = SimpleNamespace(duration=duration) if duration is not None else None
    tool = SimpleNamespace(
        tool_name=tool_name,
        result=result,
        metrics=metrics,
        tool_args=tool_args or {},
    )
    return SimpleNamespace(tool=tool)


class TestMcpStringNormalisation:
    """MCP tools often return the literal string "None" / "null"
    / "undefined" for empty responses (the language they came
    from). The summary treats those as no-result so the tool
    card doesn't show a misleading "None" pill."""

    def test_literal_None_string_treated_as_empty(self):
        # The agent's perspective: this tool ran successfully
        # but produced nothing. Showing "None" as a result
        # would look like the agent got back a Python None
        # repr.
        result = extract_result(_event(result="None"))
        assert result.full_result == ""
        assert result.summary == ""

    def test_literal_null_string_treated_as_empty(self):
        # MCP servers ported from JS land emit "null".
        result = extract_result(_event(result="null"))
        assert result.full_result == ""

    def test_literal_undefined_string_treated_as_empty(self):
        # And "undefined" from older JS callsites.
        result = extract_result(_event(result="undefined"))
        assert result.full_result == ""

    def test_actual_None_value_handled(self):
        # Distinguish: ``result=None`` (Python None) should
        # also be empty — but via the ``not result`` early
        # path, not the string-equality fallback.
        result = extract_result(_event(result=None))
        assert result.full_result == ""

    def test_strings_with_None_substring_not_normalised(self):
        # ``"None of the above"`` is real content — must NOT
        # be stripped to empty just because it starts with
        # "None". Pin the equality (not contains) semantics.
        result = extract_result(_event(result="None of the above"))
        assert "None of the above" in result.full_result
        assert result.full_result != ""


class TestSingleLineSummary:
    def test_short_single_line_passes_through(self):
        # Under 80 chars, no ellipsis.
        result = extract_result(_event(result="quick output"))
        assert result.summary == "quick output"
        assert "..." not in result.summary

    def test_long_single_line_truncates_at_80_chars(self):
        # The summary shows on the collapsed tool card; long
        # lines would overflow the terminal column. Truncate
        # to 80 + ellipsis. Pin both the cap and the marker.
        long_text = "x" * 200
        result = extract_result(_event(result=long_text))
        # Format is ``<80 chars>...`` (= 83 chars total).
        assert result.summary == "x" * 80 + "..."

    def test_exactly_80_chars_no_truncation(self):
        # Off-by-one boundary: 80 chars exactly should NOT
        # get the ellipsis. The source uses ``len > 80``.
        result = extract_result(_event(result="x" * 80))
        assert result.summary == "x" * 80
        assert "..." not in result.summary

    def test_81_chars_truncates(self):
        # And 81 → truncated (off-by-one cover).
        result = extract_result(_event(result="x" * 81))
        assert result.summary.endswith("...")
        assert len(result.summary) == 83  # 80 + 3 ellipsis chars

    def test_full_result_preserves_untrimmed_content(self):
        # Only the SUMMARY is truncated. The full_result is
        # the unmodified text (modulo whitespace strip) so
        # the expanded view shows everything.
        long_text = "x" * 200
        result = extract_result(_event(result=long_text))
        assert result.full_result == long_text
        assert len(result.full_result) == 200


class TestMultiLineSummary:
    def test_multi_line_becomes_line_count(self):
        # Multi-line output doesn't try to extract a useful
        # first line — the count is more honest about "this
        # produced N lines of output, click to expand".
        result = extract_result(_event(result="line 1\nline 2\nline 3"))
        assert result.summary == "3 lines of output"

    def test_two_lines_uses_count(self):
        # Even 2 lines goes to "N lines" rather than showing
        # both — the collapsed card is single-line.
        result = extract_result(_event(result="alpha\nbeta"))
        assert result.summary == "2 lines of output"

    def test_full_result_preserves_newlines(self):
        # The expanded view needs the original line breaks.
        result = extract_result(_event(result="a\nb\nc"))
        assert result.full_result == "a\nb\nc"
        assert result.full_result.count("\n") == 2


class TestTimingSuffix:
    def test_timing_appended_to_summary(self):
        # When the tool reports duration metrics, the summary
        # gets ", X.XXs" appended so the user can spot slow
        # tools at a glance.
        result = extract_result(_event(result="ok", duration=1.234))
        assert result.summary == "ok, 1.23s"

    def test_no_timing_when_duration_missing(self):
        # No metrics → no timing suffix.
        result = extract_result(_event(result="ok"))
        assert result.summary == "ok"
        assert "s" not in result.summary or result.summary[-1] == "s"

    def test_completed_in_timing_when_no_summary_text(self):
        # No result text but we have timing — show "completed
        # in <timing>" instead of an empty pill (would look
        # broken). Pinned because the alternative branch
        # produces different copy.
        result = extract_result(_event(result="", duration=0.5))
        assert result.summary == "completed in 0.50s"

    def test_completed_in_timing_handles_MCP_None_string(self):
        # The MCP normalization happens before the timing
        # branch — "None" string + duration → "completed in
        # <timing>" (NOT "None, <timing>"). Pin the ordering.
        result = extract_result(_event(result="None", duration=0.25))
        assert result.summary == "completed in 0.25s"

    def test_timing_format_two_decimal_places(self):
        # The format spec is ``{duration:.2f}s`` — two decimal
        # places. Pin so a future refactor that uses ``.1f``
        # or ``int(duration)`` doesn't quietly change the
        # display.
        result = extract_result(_event(result="ok", duration=3.0))
        assert "3.00s" in result.summary


class TestSentinels:
    def test_no_tool_returns_empty_data(self):
        # Defensive — Agno events sometimes lack the ``tool``
        # attribute (early in run lifecycle, or stub events).
        # The function should produce an empty
        # ``ToolResultData`` rather than crash.
        event = SimpleNamespace(tool=None)
        result = extract_result(event)
        assert result.summary == ""
        assert result.full_result == ""

    def test_no_metrics_no_timing(self):
        # ``tool.metrics`` may be None. The timing block
        # branches on that — pin no-crash.
        tool = SimpleNamespace(tool_name="x", result="ok", metrics=None, tool_args={})
        result = extract_result(SimpleNamespace(tool=tool))
        assert result.summary == "ok"

    def test_metrics_without_duration_no_timing(self):
        # ``metrics.duration is None`` — same path: no timing
        # suffix.
        metrics = SimpleNamespace(duration=None)
        tool = SimpleNamespace(tool_name="x", result="ok", metrics=metrics, tool_args={})
        result = extract_result(SimpleNamespace(tool=tool))
        assert result.summary == "ok"

    def test_result_stripped_of_whitespace(self):
        # The source applies ``.strip()`` so the summary
        # doesn't have leading/trailing whitespace.
        result = extract_result(_event(result="  ok  "))
        assert result.summary == "ok"
        assert result.full_result == "ok"
