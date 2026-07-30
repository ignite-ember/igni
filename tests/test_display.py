"""Tests for utils/display.py — terminal output utilities."""

from io import StringIO

from rich.console import Console

from ember_code.core.utils.display import DisplayManager, RunStats, ToolCallDisplay


class TestDisplayManager:
    def _make(self):
        buf = StringIO()
        console = Console(file=buf, no_color=True, width=120)
        return DisplayManager(console=console), buf

    def test_print_error(self):
        mgr, buf = self._make()
        mgr.print_error("something broke")
        assert "something broke" in buf.getvalue()

    def test_print_warning(self):
        mgr, buf = self._make()
        mgr.print_warning("be careful")
        assert "be careful" in buf.getvalue()

    def test_print_info(self):
        mgr, buf = self._make()
        mgr.print_info("just so you know")
        assert "just so you know" in buf.getvalue()

    def test_print_response_plain(self):
        mgr, buf = self._make()
        mgr.print_response("Hello world")
        assert "Hello" in buf.getvalue()

    def test_print_response_with_agent_name(self):
        mgr, buf = self._make()
        mgr.print_response("result", agent_name="editor")
        assert "editor" in buf.getvalue()

    def test_print_tool_call(self):
        mgr, buf = self._make()
        mgr.print_tool_call(ToolCallDisplay(tool_name="edit_file", args={"path": "/tmp/test.py"}))
        output = buf.getvalue()
        assert "edit_file" in output
        assert "path" in output

    def test_print_tool_call_no_args(self):
        mgr, buf = self._make()
        mgr.print_tool_call(ToolCallDisplay(tool_name="git_status"))
        assert "git_status" in buf.getvalue()

    def test_print_tool_call_truncates_long_args(self):
        mgr, buf = self._make()
        mgr.print_tool_call(ToolCallDisplay(tool_name="edit", args={"content": "x" * 200}))
        assert "..." in buf.getvalue()

    def test_print_tool_call_uses_text_prefix_not_emoji(self):
        """Rule 3: no emoji icons in the terminal output. The
        tool-call marker must be a plain ``>`` text prefix, and
        the old ``⚡`` glyph must not appear anywhere in the
        rendered line."""
        mgr, buf = self._make()
        mgr.print_tool_call(ToolCallDisplay(tool_name="git_status"))
        output = buf.getvalue()
        assert "⚡" not in output
        assert ">" in output

    def test_print_run_stats_seconds(self):
        mgr, buf = self._make()
        mgr.print_run_stats(RunStats(elapsed_seconds=3.5))
        assert "3.5s" in buf.getvalue()

    def test_print_run_stats_minutes(self):
        mgr, buf = self._make()
        mgr.print_run_stats(RunStats(elapsed_seconds=125.0))
        assert "2m" in buf.getvalue()

    def test_print_run_stats_with_tokens(self):
        mgr, buf = self._make()
        mgr.print_run_stats(
            RunStats(
                elapsed_seconds=5.0,
                input_tokens=100,
                output_tokens=50,
                model="test-model",
            )
        )
        output = buf.getvalue()
        assert "150 tokens" in output
        assert "test-model" in output

    def test_print_welcome(self):
        mgr, buf = self._make()
        mgr.print_welcome("0.1.0")
        output = buf.getvalue()
        assert "igni" in output
        assert "0.1.0" in output

    def test_print_markdown(self):
        mgr, buf = self._make()
        mgr.print_markdown("## Hello\n\nWorld")
        output = buf.getvalue()
        assert "Hello" in output


class TestRunStats:
    """The formatting policy lives on the model — a
    non-Rich sink can reuse ``format_summary`` verbatim."""

    def test_format_summary_seconds_only(self):
        assert RunStats(elapsed_seconds=3.5).format_summary() == "3.5s"

    def test_format_summary_minutes_and_seconds(self):
        assert RunStats(elapsed_seconds=125.0).format_summary().startswith("2m")

    def test_format_summary_includes_tokens_when_present(self):
        summary = RunStats(
            elapsed_seconds=5.0,
            input_tokens=100,
            output_tokens=50,
        ).format_summary()
        assert "150 tokens" in summary
        assert "100" in summary and "50" in summary

    def test_format_summary_omits_tokens_when_zero(self):
        summary = RunStats(elapsed_seconds=5.0).format_summary()
        assert "tokens" not in summary

    def test_format_summary_appends_model_last(self):
        summary = RunStats(elapsed_seconds=1.0, model="opus-4-7").format_summary()
        assert summary.endswith("opus-4-7")


class TestToolCallDisplay:
    """Truncation and arg-rendering live on the DTO."""

    def test_format_args_empty(self):
        assert ToolCallDisplay(tool_name="foo").format_args() == ""

    def test_format_args_renders_key_value(self):
        rendered = ToolCallDisplay(tool_name="foo", args={"path": "/tmp/x"}).format_args()
        assert "path=/tmp/x" in rendered

    def test_format_args_truncates_long_values(self):
        rendered = ToolCallDisplay(tool_name="foo", args={"content": "x" * 200}).format_args()
        assert "..." in rendered
        # 50-char cap on the value part; overhead ("content=", " (", ")")
        # is fixed, so the total must stay bounded.
        assert len(rendered) < 100
