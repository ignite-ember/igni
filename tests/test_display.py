"""Tests for utils/display.py — terminal output utilities."""

from io import StringIO

from rich.console import Console

from ember_code.core.utils.display import DisplayManager


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
        mgr.print_tool_call("edit_file", {"path": "/tmp/test.py"})
        output = buf.getvalue()
        assert "edit_file" in output
        assert "path" in output

    def test_print_tool_call_no_args(self):
        mgr, buf = self._make()
        mgr.print_tool_call("git_status")
        assert "git_status" in buf.getvalue()

    def test_print_tool_call_truncates_long_args(self):
        mgr, buf = self._make()
        mgr.print_tool_call("edit", {"content": "x" * 200})
        assert "..." in buf.getvalue()

    def test_print_run_stats_seconds(self):
        mgr, buf = self._make()
        mgr.print_run_stats(elapsed_seconds=3.5)
        assert "3.5s" in buf.getvalue()

    def test_print_run_stats_minutes(self):
        mgr, buf = self._make()
        mgr.print_run_stats(elapsed_seconds=125.0)
        assert "2m" in buf.getvalue()

    def test_print_run_stats_with_tokens(self):
        mgr, buf = self._make()
        mgr.print_run_stats(
            elapsed_seconds=5.0,
            input_tokens=100,
            output_tokens=50,
            model="test-model",
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
