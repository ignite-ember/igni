"""Tests for the tool-call error rendering pipeline.

Reported on v0.5.11: ``Edit`` returning ``"Error: old_string not
found in /path/file.py"`` rendered with a green ``✓`` checkmark.
The agent saw the failure in its tool result and treated it as
denied, but the user saw success — so when the AI summarized
("REJECTED") the user thought the AI was lying.

Two layers, two tests:

  * Backend serializer detects ``Error:`` prefix on tool result
    strings and sets ``ToolCompleted.is_error=True``.
  * ``ToolCallLiveWidget`` renders ``✗`` with red styling when
    ``is_error=True`` (and on the ``mark_error`` toggle); the
    plain success path still renders ``✓`` in dim grey.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from agno.run.agent import ToolCallCompletedEvent

from ember_code.frontend.tui.run_controller import RunController
from ember_code.frontend.tui.widgets._messages import ToolCallLiveWidget
from ember_code.protocol.serializer import serialize_event


class TestSerializerDetectsToolError:
    """``Error:`` prefix is ember-code's tool-error convention. The
    serializer must surface it as ``is_error=True`` so the FE can
    render the ✗ glyph."""

    def _make_event_with_result(self, result: str):
        """Build a minimal Agno ToolCompletedEvent-shaped mock."""
        ev = ToolCallCompletedEvent.__new__(ToolCallCompletedEvent)
        tool = MagicMock()
        tool.tool_name = "edit_file"
        tool.tool_args = {"file_path": "x.py"}
        tool.result = result
        tool.error = None
        # ``metrics`` is touched by the serializer's timing extraction
        # (``f"{duration:.2f}s"``); ``None`` skips that branch cleanly.
        tool.metrics = None
        ev.tool = tool
        ev.run_id = "r"
        return ev

    def test_error_prefix_sets_is_error_true(self):
        ev = self._make_event_with_result(
            "Error: old_string not found in /tmp/x.py. Make sure the string matches exactly."
        )
        proto = serialize_event(ev)
        assert proto is not None
        assert proto.is_error is True

    def test_success_result_keeps_is_error_false(self):
        ev = self._make_event_with_result("Successfully edited /tmp/x.py")
        proto = serialize_event(ev)
        assert proto is not None
        assert proto.is_error is False

    def test_error_in_middle_of_result_does_not_trigger(self):
        """A grep result containing the word 'Error' in body content
        must NOT be flagged. Only the strict ``startswith`` prefix
        triggers — that's the convention ember-code's tools follow."""
        ev = self._make_event_with_result(
            "x.py:42: log.error('Error: division by zero')\nx.py:55: ..."
        )
        proto = serialize_event(ev)
        assert proto is not None
        assert proto.is_error is False

    def test_empty_result_is_not_error(self):
        ev = self._make_event_with_result("")
        proto = serialize_event(ev)
        assert proto is not None
        assert proto.is_error is False

    def test_leading_whitespace_before_error_still_detected(self):
        """Some tools indent their error output; the detection
        ``lstrip``s before checking the prefix so leading newlines
        or spaces don't hide the failure."""
        ev = self._make_event_with_result("\n  Error: file not found")
        proto = serialize_event(ev)
        assert proto is not None
        assert proto.is_error is True

    def test_shell_exited_nonzero_detected(self):
        """Shell tool wraps non-zero exits in
        ``[Exited with code N after Ts]`` — non-zero codes must
        flag as error so the UI doesn't show ✓ on a failing build."""
        ev = self._make_event_with_result(
            "[Exited with code 1 after 0.42s]\npython: command not found"
        )
        proto = serialize_event(ev)
        assert proto is not None
        assert proto.is_error is True

    def test_shell_exited_zero_is_not_error(self):
        """A successful Shell exit (code 0) must not flag as error
        even though it matches the same prefix shape."""
        ev = self._make_event_with_result("[Exited with code 0 after 1.23s]\nhello world")
        proto = serialize_event(ev)
        assert proto is not None
        assert proto.is_error is False

    def test_shell_background_exit_nonzero_detected(self):
        """Backgrounded Shell tool reports failure as
        ``Background process exited immediately (code N)`` — N!=0
        must flag."""
        ev = self._make_event_with_result(
            "Background process exited immediately (code 127)\nbash: nope: not found"
        )
        proto = serialize_event(ev)
        assert proto is not None
        assert proto.is_error is True

    def test_shell_background_exit_zero_is_not_error(self):
        ev = self._make_event_with_result(
            "Background process exited immediately (code 0)\n(no output)"
        )
        proto = serialize_event(ev)
        assert proto is not None
        assert proto.is_error is False

    def test_failed_edit_file_with_old_new_strings_still_flagged_as_error(self):
        """The original v0.5.11 bug class, surviving in the diff branch.

        When ``edit_file`` fails with ``"Error: old_string not found"``
        the tool still has ``old_string``/``new_string`` in its args
        (the LLM's *proposed* change). ``_format_edit_diff`` happily
        renders a fake diff from a change that never happened — and
        the diff branch in ``extract_result`` used to clobber
        ``full_result=""``, which hid the ``Error:`` prefix from
        ``_result_is_error``. End result: green ✓ on a failed edit,
        which is exactly the lying-UI bug we set out to kill. Pinning
        the fix here so it can't regress."""
        ev = ToolCallCompletedEvent.__new__(ToolCallCompletedEvent)
        tool = MagicMock()
        tool.tool_name = "edit_file"
        tool.tool_args = {
            "file_path": "x.py",
            "old_string": "def foo():\n    pass",
            "new_string": "def foo():\n    return 1",
        }
        tool.result = "Error: old_string not found in x.py"
        tool.error = None
        tool.metrics = None
        ev.tool = tool
        ev.run_id = "r"

        proto = serialize_event(ev)
        assert proto is not None
        assert proto.is_error is True

    def test_grep_result_with_exited_text_in_body_does_not_trigger(self):
        """A grep result with the phrase ``[Exited with code 1`` in
        body content (e.g. quoted source) must NOT be flagged — the
        regex is anchored to the start of the (lstripped) string."""
        ev = self._make_event_with_result(
            "main.py:10: print('[Exited with code 1 after Xs]')\nmain.py:11: ..."
        )
        proto = serialize_event(ev)
        assert proto is not None
        assert proto.is_error is False


class TestToolCallLiveWidgetErrorRendering:
    """Widget displays ✗ + red for errors, ✓ + dim for success.
    Asserts on ``render_text`` (the test/inspection accessor) and on
    the internal flag set by ``mark_error``."""

    def test_success_render_uses_check_mark(self):
        w = ToolCallLiveWidget("edit_file", "x.py", status="done")
        out = w.render_text()
        assert "✓" in out  # ✓
        assert "✗" not in out  # no ✗
        assert "[dim]" in out
        assert "[red]" not in out

    def test_error_render_uses_cross_mark(self):
        w = ToolCallLiveWidget("edit_file", "x.py", status="done", is_error=True)
        out = w.render_text()
        assert "✗" in out  # ✗
        assert "✓" not in out  # no ✓
        assert "[red]" in out
        assert "[dim]" not in out

    def test_mark_error_toggles_widget_into_error_state(self):
        """A widget that was created as 'running' (no is_error info
        yet) and later receives an error result must be flipped via
        ``mark_error`` before ``mark_done`` rerenders."""
        w = ToolCallLiveWidget("edit_file", "x.py", status="running")
        assert w._is_error is False

        w.mark_error("old_string not found")
        assert w._is_error is True
        assert w._result_summary == "old_string not found"

    def test_format_header_reflects_error_state(self):
        """The internal ``_format_header`` used by the rendering
        path must also produce the ✗ + red header — otherwise the
        widget would have an inconsistent display (cross in
        ``render_text`` but checkmark in the real DOM)."""
        w = ToolCallLiveWidget("edit_file", "x.py", status="done", is_error=True)
        header = w._format_header()
        assert "✗" in header
        assert "[red]" in header

    def test_running_widget_uses_hourglass_regardless_of_error_flag(self):
        """While still running, neither glyph is appropriate — the
        widget shows ⏳ until the tool completes. Setting
        ``is_error`` while ``status='running'`` shouldn't preempt
        that."""
        w = ToolCallLiveWidget("edit_file", "x.py", status="running", is_error=True)
        out = w.render_text()
        assert "⏳" in out  # ⏳
        assert "✗" not in out
        assert "✓" not in out


class TestRunControllerToolError:
    """The Agno-exception path (``ToolError`` event) used to call
    ``mark_done`` without ``mark_error`` first, leaving the widget in
    ``_is_error=False`` — so it rendered ✓ even when Agno raised. Same
    class of lying-UI bug as the original v0.5.11 issue, just on a
    different event path. Pin the fix here."""

    def test_on_tool_error_flips_widget_to_error_state(self):
        """A running ToolCallLiveWidget that gets a ToolError should
        end up with ``_is_error=True`` and a ✗ in its rendered output.
        """
        widget = ToolCallLiveWidget("edit_file", "x.py", status="running")

        # ``_mount_target`` is a property that reads from
        # ``_agent_stack`` / ``_conversation.container``; stub both so
        # the property resolves to a mock with our widget.
        conversation = MagicMock()
        conversation.container.query = MagicMock(return_value=[widget])

        controller = RunController.__new__(RunController)
        controller._agent_stack = []
        controller._conversation = conversation
        controller._spinner = None

        controller._on_tool_error("KeyError: 'foo'")

        assert widget._is_error is True
        assert widget._status == "done"
        out = widget.render_text()
        assert "✗" in out
        assert "✓" not in out
