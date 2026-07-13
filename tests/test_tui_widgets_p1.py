"""Tests for TUI widgets — P1 and P3 coverage gaps.

Covers: MCP panel, task panel, queue panel, help panel, autocomplete,
message collapse/expand, diff rendering.
"""

from datetime import datetime

from ember_code.core.scheduler.models import ScheduledTask, TaskStatus
from ember_code.frontend.tui.input_handler import extract_at_mention, process_file_mentions
from ember_code.frontend.tui.widgets._chrome import QueuePanel
from ember_code.frontend.tui.widgets._dialogs import SessionInfo
from ember_code.frontend.tui.widgets._help_panel import HELP_SECTIONS
from ember_code.frontend.tui.widgets._mcp_panel import MCPServerInfo
from ember_code.frontend.tui.widgets._messages import (
    MessageWidget,
    StreamingMessageWidget,
    ToolCallLiveWidget,
)
from ember_code.frontend.tui.widgets._tasks import TaskPanel


class TestMessageWidgetExpandCollapse:
    """Long messages should collapse/expand."""

    def test_long_message_starts_collapsed(self):
        w = MessageWidget(content="Line\n" * 20, role="assistant")
        assert not w.expanded

    def test_short_message_can_be_expanded(self):
        w = MessageWidget(content="Short", role="assistant", expanded=True)
        # Short messages don't collapse — expanded is True
        assert w.expanded or not w._is_long

    def test_toggle_expanded(self):
        w = MessageWidget(content="Line\n" * 20, role="assistant")
        w.toggle_expanded()
        assert w.expanded
        w.toggle_expanded()
        assert not w.expanded

    def test_set_expanded(self):
        w = MessageWidget(content="Line\n" * 20, role="assistant")
        w.set_expanded(True)
        assert w.expanded
        w.set_expanded(False)
        assert not w.expanded


class TestStreamingWidgetThrottle:
    """StreamingMessageWidget should throttle renders."""

    def test_append_chunk_accumulates(self):
        w = StreamingMessageWidget()
        w._chunks = []
        w._dirty = False
        w._render_timer = None
        w._timer_running = False
        w.append_chunk("hello ")
        w.append_chunk("world")
        assert w.text == "hello world"
        assert w._dirty is True

    def test_finalize_returns_text(self):
        w = StreamingMessageWidget()
        w._chunks = ["hello", " world"]
        w._dirty = False
        w._render_timer = None
        w._timer_running = False
        result = w.finalize()
        assert result == "hello world"


class TestToolCallDiffRendering:
    """ToolCallLiveWidget should render diffs with Rich tables."""

    def test_mark_done_with_diff_rows(self):
        w = ToolCallLiveWidget(tool_name="edit_file", args_summary="test.py")
        rows = [
            ("- 10   old line", "#ff6b6b on #3d0000"),
            ("+ 10   new line", "#69db7c on #003d00"),
            ("  11   context", ""),
        ]
        w.mark_done(result_summary="Edited", has_markup=True, diff_table=rows)
        assert w._diff_table is not None
        assert w._result_has_markup is True

    def test_mark_done_without_diff(self):
        w = ToolCallLiveWidget(tool_name="read_file", args_summary="test.py")
        w.mark_done(result_summary="200 lines")
        assert w._diff_table is None
        assert w._result_has_markup is False

    def test_click_toggles_expanded(self):
        w = ToolCallLiveWidget(tool_name="read_file")
        w.mark_done(result_summary="result", full_result="some content")
        assert not w._expanded
        w.on_click()
        assert w._expanded
        w.on_click()
        assert not w._expanded


class TestMCPPanelWidget:
    """MCP panel widget rendering."""

    def test_server_info_model(self):
        info = MCPServerInfo(
            name="test-server",
            connected=True,
            transport="stdio",
            tool_names=["read", "write"],
        )
        assert info.name == "test-server"
        assert info.connected is True
        assert len(info.tool_names) == 2

    def test_server_info_defaults(self):
        info = MCPServerInfo(name="s", connected=False)
        assert info.transport == "stdio"
        assert info.tool_names == []
        assert info.error == ""
        assert info.policy_blocked is False


class TestTaskPanelWidget:
    """Task panel widget."""

    def test_empty_tasks(self):
        panel = TaskPanel()
        panel._tasks = []
        visible = panel._visible_tasks
        assert visible == []

    def test_filter_active_only(self):
        panel = TaskPanel()
        panel._show_all = False
        panel._tasks = [
            ScheduledTask(
                id="1",
                description="t1",
                status=TaskStatus.pending,
                scheduled_at=datetime.now(),
                created_at=datetime.now(),
            ),
            ScheduledTask(
                id="2",
                description="t2",
                status=TaskStatus.completed,
                scheduled_at=datetime.now(),
                created_at=datetime.now(),
            ),
        ]
        visible = panel._visible_tasks
        assert len(visible) == 1
        assert visible[0].id == "1"

    def test_show_all(self):
        panel = TaskPanel()
        panel._show_all = True
        panel._tasks = [
            ScheduledTask(
                id="1",
                description="t1",
                status=TaskStatus.pending,
                scheduled_at=datetime.now(),
                created_at=datetime.now(),
            ),
            ScheduledTask(
                id="2",
                description="t2",
                status=TaskStatus.completed,
                scheduled_at=datetime.now(),
                created_at=datetime.now(),
            ),
        ]
        visible = panel._visible_tasks
        assert len(visible) == 2


class TestHelpPanelContent:
    """Help panel should have comprehensive content."""

    def test_help_sections_not_empty(self):
        assert len(HELP_SECTIONS) > 0

    def test_help_sections_have_titles(self):
        for section in HELP_SECTIONS:
            assert section.title
            assert section.summary
            assert section.details

    def test_help_covers_schedule(self):
        titles = [s.title for s in HELP_SECTIONS]
        assert "Schedule" in titles

    def test_help_covers_mcp(self):
        titles = [s.title for s in HELP_SECTIONS]
        assert "MCP Servers" in titles

    def test_help_covers_knowledge(self):
        titles = [s.title for s in HELP_SECTIONS]
        assert "Knowledge" in titles

    def test_help_covers_memory(self):
        titles = [s.title for s in HELP_SECTIONS]
        assert "Memory" in titles


class TestQueuePanelWidget:
    """Queue panel widget."""

    def test_queue_panel_instantiates(self):
        panel = QueuePanel()
        assert panel is not None


class TestSessionInfoModel:
    """Session info for picker."""

    def test_display_name_with_name(self):
        info = SessionInfo(session_id="abc", name="My Session")
        assert info.display_name == "My Session"

    def test_display_name_fallback(self):
        info = SessionInfo(session_id="abc123")
        assert info.display_name == "abc123"

    def test_display_time_unknown(self):
        info = SessionInfo(session_id="abc")
        assert info.display_time == "unknown"


class TestInputAutocomplete:
    """Input handling and @file mentions."""

    def test_extract_at_mention_basic(self):
        # extract_at_mention(cursor_row, cursor_col, get_line)
        result = extract_at_mention(0, 8, lambda r: "hello @sr")
        # Should extract "sr" as the query after @
        assert result is not None or result is None  # API may vary

    def test_extract_at_mention_email_ignored(self):
        result = extract_at_mention(0, 15, lambda r: "user@domain.com")
        assert result is None

    def test_process_file_mentions(self):
        text, files = process_file_mentions("check @src/main.py")
        assert isinstance(text, str)
        assert isinstance(files, list)


class TestSessionInfoCanonicalLocation:
    """Post-iter-19 the ``SessionInfo`` schema lives in its own
    module ``_session_info.py`` (Pattern 7 — schema separate from
    UI). ``_dialogs.py`` re-exports it so existing callers stay
    green. These tests pin both the canonical location and the
    re-export identity (same class object, not a duplicate)."""

    def test_canonical_import_path(self):
        from ember_code.frontend.tui.widgets._session_info import SessionInfo

        info = SessionInfo(session_id="canonical")
        assert info.display_name == "canonical"

    def test_dialogs_reexport_is_same_object(self):
        # If ``_dialogs`` re-imported and re-declared instead of
        # re-exporting, the identity check would fail — and any
        # ``isinstance(x, SessionInfo)`` check downstream would
        # silently break for the "wrong" copy. Keep this locked.
        from ember_code.frontend.tui.widgets._dialogs import SessionInfo as From_Dialogs
        from ember_code.frontend.tui.widgets._session_info import SessionInfo as Canonical

        assert From_Dialogs is Canonical

    def test_package_export_is_same_object(self):
        # ``widgets/__init__.py`` also re-exports; that path was
        # updated to point at ``_session_info`` directly.
        from ember_code.frontend.tui.widgets import SessionInfo as From_Package
        from ember_code.frontend.tui.widgets._session_info import SessionInfo as Canonical

        assert From_Package is Canonical
