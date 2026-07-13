"""Tests for TUI widgets."""

import time

import pytest

from ember_code.frontend.tui.widgets import (
    SPINNER_FRAMES,
    AgentTreeWidget,
    InputHistory,
    MCPCallWidget,
    MessageWidget,
    QueuePanel,
    RunStatsWidget,
    SessionInfo,
    SessionPickerWidget,
    SpinnerWidget,
    StatusBar,
    StreamingMessageWidget,
    TokenBadge,
    ToolCallLiveWidget,
)
from ember_code.frontend.tui.widgets._codeindex_panel import CodeIndexStatusInfo


class TestMessageWidget:
    def test_short_message_not_long(self):
        w = MessageWidget("short text", role="user")
        assert w._is_long is False
        assert w.expanded is False

    def test_long_message_is_long(self):
        content = "\n".join(f"Line {i}" for i in range(20))
        w = MessageWidget(content, role="assistant")
        assert w._is_long is True
        assert w.expanded is False

    def test_threshold_boundary(self):
        exactly_10 = "\n".join(f"Line {i}" for i in range(10))
        w = MessageWidget(exactly_10, role="assistant")
        assert w._is_long is False

        eleven = "\n".join(f"Line {i}" for i in range(11))
        w = MessageWidget(eleven, role="assistant")
        assert w._is_long is True

    def test_set_expanded_on_long(self):
        content = "\n".join(f"Line {i}" for i in range(20))
        w = MessageWidget(content, role="assistant")
        assert w.expanded is False
        # Can't fully test toggle without Textual app context,
        # but we can test the state tracking
        w.expanded = True
        assert w.expanded is True

    def test_set_expanded_noop_on_short(self):
        w = MessageWidget("short", role="user")
        w.set_expanded(True)
        assert w.expanded is False  # Stays false since not long

    def test_role_stored(self):
        w = MessageWidget("hello", role="assistant")
        assert w._role == "assistant"

    def test_content_stored(self):
        w = MessageWidget("some content", role="user")
        assert w._content == "some content"


class TestStreamingMessageWidget:
    def test_initial_empty(self):
        w = StreamingMessageWidget()
        assert w.text == ""
        assert w._chunks == []

    def test_append_chunk(self):
        w = StreamingMessageWidget()
        w._chunks.append("Hello ")
        w._chunks.append("world")
        assert w.text == "Hello world"

    def test_finalize_returns_full_text(self):
        w = StreamingMessageWidget()
        w._chunks = ["a", "b", "c"]
        assert w.finalize() == "abc"

    def test_multiple_chunks_accumulate(self):
        w = StreamingMessageWidget()
        w._chunks.append("one")
        w._chunks.append("two")
        w._chunks.append("three")
        assert w.text == "onetwothree"


class TestTokenBadge:
    def test_fmt_small(self):
        assert TokenBadge._fmt(42) == "42"
        assert TokenBadge._fmt(999) == "999"

    def test_fmt_thousands(self):
        assert TokenBadge._fmt(1234) == "1.2k"
        assert TokenBadge._fmt(9999) == "10.0k"

    def test_fmt_large(self):
        assert TokenBadge._fmt(12345) == "12k"
        assert TokenBadge._fmt(100000) == "100k"

    def test_render_format(self):
        badge = TokenBadge(1500, 300)
        rendered = badge.render_text()
        assert "1.5k" in rendered
        assert "300" in rendered
        assert "in:" in rendered
        assert "out:" in rendered

    def test_fmt_zero(self):
        assert TokenBadge._fmt(0) == "0"

    def test_fmt_boundary(self):
        assert TokenBadge._fmt(1000) == "1.0k"
        assert TokenBadge._fmt(10000) == "10k"


class TestStatusBar:
    def test_context_usage(self):
        bar = StatusBar()
        bar.set_context_usage(context_tokens=42_000, max_context=100_000)
        assert bar.context_used_pct == pytest.approx(42.0)

    def test_context_usage_zero(self):
        bar = StatusBar()
        bar.set_context_usage(context_tokens=0, max_context=100_000)
        assert bar.context_used_pct == 0.0

    def test_initial_context_pct(self):
        bar = StatusBar()
        assert bar.context_used_pct == 0.0

    # ── CodeIndex slot (always rendered) ───────────────────────────

    def test_codeindex_badge_checking_by_default(self):
        """Before the first poll lands, the slot reads 'checking…'
        rather than 'offline' (which read like a network outage)
        or being hidden. The eager refresh in ``on_mount`` replaces
        this within ~1s — only visible during session warm-up."""
        bar = StatusBar()
        badge = bar._codeindex_badge()
        assert "CodeIndex" in badge
        assert "checking" in badge

    def test_codeindex_badge_indexed_green_check(self):
        bar = StatusBar()
        bar.set_codeindex_status(
            CodeIndexStatusInfo(
                local_sha="abc",
                head_indexed=True,
                install_state="installed",
            )
        )
        badge = bar._codeindex_badge()
        assert "[green]" in badge
        assert "\u2713" in badge

    def test_codeindex_badge_syncing(self):
        bar = StatusBar()
        bar.set_codeindex_status(
            CodeIndexStatusInfo(
                local_sha="abc",
                sync_in_progress=True,
                sync_progress_pct=42,  # ignored on purpose — no % in the bar
                install_state="installed",
            )
        )
        badge = bar._codeindex_badge()
        assert "syncing" in badge
        # The bar is tight; the panel covers progress detail. Make
        # sure we don't render the percent here.
        assert "42" not in badge
        assert "%" not in badge

    def test_codeindex_badge_uninstalled_when_needs_install(self):
        """The user asked for 'uninstalled' wording specifically when
        the GitHub App isn't installed for this repo. Yellow tone so
        it's actionable but not alarming."""
        bar = StatusBar()
        bar.set_codeindex_status(CodeIndexStatusInfo(install_state="needs_install"))
        badge = bar._codeindex_badge()
        assert "uninstalled" in badge
        assert "[yellow]" in badge

    def test_codeindex_badge_inactive_when_state_unknown(self):
        """``unknown`` install_state (resolver not initialised — no
        cloud auth, no git remote, or feature disabled) renders as
        'inactive'. Earlier wording was 'offline' but that read as
        a network outage, which isn't what's actually going on."""
        bar = StatusBar()
        bar.set_codeindex_status(CodeIndexStatusInfo(install_state="unknown"))
        badge = bar._codeindex_badge()
        assert "inactive" in badge
        # Regression guard against the old, ambiguous wording.
        assert "offline" not in badge

    def test_codeindex_badge_indexed_wins_over_unknown_install_state(self):
        """``head_indexed`` must outrank ``install_state == "unknown"``.

        The v0.5.16 sync short-circuit returns before
        ``resolver.resolve()`` when the target sha is already
        indexed, so the resolver cache stays cold all session and
        ``install_state`` reports ``unknown``. The local index is
        fully usable in that state — the badge showing ``inactive``
        while the panel said ``indexed`` was the reported bug."""
        bar = StatusBar()
        bar.set_codeindex_status(
            CodeIndexStatusInfo(
                local_sha="abc",
                head_indexed=True,
                install_state="unknown",
            )
        )
        badge = bar._codeindex_badge()
        assert "[green]" in badge
        assert "✓" in badge
        assert "inactive" not in badge

    def test_codeindex_badge_error_red(self):
        bar = StatusBar()
        bar.set_codeindex_status(
            CodeIndexStatusInfo(
                local_sha="abc",
                sync_error="boom",
                install_state="installed",
            )
        )
        badge = bar._codeindex_badge()
        assert "[red]" in badge

    def test_codeindex_badge_not_indexed_fallback(self):
        """HEAD exists, install state is fine, no sync running, but
        ``head_indexed`` is False — fall through to 'not indexed'."""
        bar = StatusBar()
        bar.set_codeindex_status(
            CodeIndexStatusInfo(
                local_sha="abc",
                head_indexed=False,
                install_state="installed",
            )
        )
        badge = bar._codeindex_badge()
        assert "not indexed" in badge

    def test_codeindex_badge_set_none_keeps_previous(self):
        """A None update must not blank the badge — transient poll
        failures shouldn't erase the last known good state."""
        bar = StatusBar()
        bar.set_codeindex_status(
            CodeIndexStatusInfo(
                local_sha="abc",
                head_indexed=True,
                install_state="installed",
            )
        )
        bar.set_codeindex_status(None)
        badge = bar._codeindex_badge()
        assert "\u2713" in badge

    # ── Two-line layout ────────────────────────────────────────────

    def test_render_splits_identity_and_activity_across_two_lines(self):
        """The bar renders identity (model, session, cloud) on the
        top row and live activity (timer, context, CodeIndex) on the
        bottom row. A single ``|``-joined line truncated on most
        terminals once the slot count grew; the split anchors the
        eye on the stable top row while the bottom row ticks."""
        bar = StatusBar()
        bar.update_model("gpt-4o")
        bar.set_session_id("abc12345")
        bar.set_context_usage(context_tokens=10_000, max_context=100_000)
        text = bar.render().plain
        rows = text.split("\n")
        # Exactly two rows — neither blank.
        assert len(rows) == 2, f"expected 2 rows, got: {rows!r}"
        top, bottom = rows
        # Identity on top.
        assert "gpt-4o" in top
        assert "abc12345" in top
        # Activity on the bottom — context + CodeIndex (always shown).
        assert "Context" in bottom
        assert "CodeIndex" in bottom

    def test_render_empty_identity_keeps_bottom_row(self):
        """If the identity row is empty (e.g. model not yet loaded),
        the bottom row still renders — the CodeIndex slot is always
        present, so the bar is never blank."""
        bar = StatusBar()
        text = bar.render().plain
        # No model + no session + no cloud → top row is empty, but
        # the CodeIndex badge anchors the bottom row.
        assert "CodeIndex" in text

    def test_render_no_slots_falls_back_to_ready(self):
        """The all-empty edge case (no model, no CodeIndex status
        yet) falls back to a 'Ready' marker so the user never sees
        a blank bar at startup."""
        bar = StatusBar()
        # Disable the CodeIndex badge so we exercise the truly-empty
        # branch — set every slot to its zero state.
        bar._codeindex_status = None
        # Patch out the badge to simulate "no slots at all".
        bar._codeindex_badge = lambda: ""  # type: ignore[method-assign]
        text = bar.render().plain
        assert "Ready" in text


class TestToolCallLiveWidget:
    def test_initial_running(self):
        w = ToolCallLiveWidget("read_file", "path=main.py", status="running")
        rendered = w.render_text()
        assert "read_file" in rendered
        assert "main.py" in rendered

    def test_mark_done(self):
        w = ToolCallLiveWidget("read_file", status="running")
        assert w.is_running()
        w.mark_done()
        rendered = w.render_text()
        assert "dim" in rendered  # done uses dim style

    def test_running_icon(self):
        w = ToolCallLiveWidget("test_tool", status="running")
        rendered = w.render_text()
        assert "⏳" in rendered

    def test_done_icon(self):
        w = ToolCallLiveWidget("test_tool", status="done")
        rendered = w.render_text()
        assert "✓" in rendered

    def test_no_args(self):
        w = ToolCallLiveWidget("test_tool", status="running")
        rendered = w.render_text()
        assert "test_tool" in rendered


class TestSpinnerWidget:
    def test_initial_state(self):
        w = SpinnerWidget(label="Thinking")
        assert w._label == "Thinking"
        assert w._frame == 0
        assert w._tokens == 0

    def test_render_contains_label(self):
        w = SpinnerWidget(label="Planning")
        rendered = w.render_text()
        assert "Planning" in rendered

    def test_set_label(self):
        w = SpinnerWidget(label="Thinking")
        w._label = "Executing"
        rendered = w.render_text()
        assert "Executing" in rendered

    def test_set_tokens(self):
        w = SpinnerWidget()
        w._tokens = 1500
        rendered = w.render_text()
        assert "1.5k" in rendered
        assert "tokens" in rendered

    def test_no_tokens_no_display(self):
        w = SpinnerWidget()
        rendered = w.render_text()
        assert "tokens" not in rendered

    def test_tick_advances_frame(self):
        w = SpinnerWidget()
        assert w._frame == 0
        w._frame = (w._frame + 1) % len(SPINNER_FRAMES)
        assert w._frame == 1

    def test_frame_wraps(self):
        w = SpinnerWidget()
        w._frame = len(SPINNER_FRAMES) - 1
        w._frame = (w._frame + 1) % len(SPINNER_FRAMES)
        assert w._frame == 0

    def test_render_uses_spinner_frame(self):
        w = SpinnerWidget()
        rendered = w.render_text()
        assert SPINNER_FRAMES[0] in rendered


class TestInputHistory:
    def test_empty_history(self):
        h = InputHistory()
        assert h.history == []
        assert h.navigate_up() is None
        assert h.navigate_down() is None

    def test_push_and_navigate(self):
        h = InputHistory()
        h.push("first")
        h.push("second")
        assert h.history == ["first", "second"]
        assert h.navigate_up() == "second"
        assert h.navigate_up() == "first"
        assert h.navigate_up() is None  # at top

    def test_navigate_down_restores_draft(self):
        h = InputHistory()
        h.push("old")
        entry = h.navigate_up(current_text="draft text")
        assert entry == "old"
        restored = h.navigate_down()
        assert restored == "draft text"

    def test_navigate_down_without_navigating(self):
        h = InputHistory()
        h.push("entry")
        assert h.navigate_down() is None

    def test_no_consecutive_duplicates(self):
        h = InputHistory()
        h.push("same")
        h.push("same")
        assert h.history == ["same"]

    def test_non_consecutive_duplicates_allowed(self):
        h = InputHistory()
        h.push("a")
        h.push("b")
        h.push("a")
        assert h.history == ["a", "b", "a"]

    def test_empty_push_ignored(self):
        h = InputHistory()
        h.push("")
        h.push("  ")
        assert h.history == []

    def test_max_size(self):
        h = InputHistory(max_size=3)
        h.push("a")
        h.push("b")
        h.push("c")
        h.push("d")
        assert len(h.history) == 3
        assert h.history == ["b", "c", "d"]

    def test_is_navigating(self):
        h = InputHistory()
        h.push("entry")
        assert h.is_navigating is False
        h.navigate_up()
        assert h.is_navigating is True

    def test_push_resets_navigation(self):
        h = InputHistory()
        h.push("first")
        h.navigate_up()
        assert h.is_navigating is True
        h.push("second")
        assert h.is_navigating is False

    def test_navigate_up_down_cycle(self):
        h = InputHistory()
        h.push("a")
        h.push("b")
        h.push("c")
        assert h.navigate_up() == "c"
        assert h.navigate_up() == "b"
        assert h.navigate_down() == "c"
        assert h.navigate_down() == ""  # draft was empty


class TestAgentTreeWidget:
    def test_stores_attributes(self):
        w = AgentTreeWidget(
            team_name="test-team",
            team_mode="coordinate",
            agent_names=["editor", "reviewer"],
            reasoning="Need both for review",
        )
        assert w._team_name == "test-team"
        assert w._team_mode == "coordinate"
        assert w._agent_names == ["editor", "reviewer"]
        assert "review" in w._reasoning


class TestMCPCallWidget:
    def test_stores_attributes(self):
        w = MCPCallWidget(
            server_name="github",
            tool_name="list_issues",
            args={"repo": "ember-code"},
        )
        assert w._server_name == "github"
        assert w._tool_name == "list_issues"
        assert w._args == {"repo": "ember-code"}

    def test_default_args(self):
        w = MCPCallWidget(server_name="test", tool_name="ping")
        assert w._args == {}

    def test_default_result(self):
        w = MCPCallWidget(server_name="test", tool_name="ping")
        assert w._result == ""


class TestSessionInfo:
    def test_display_time_today(self):
        now_ts = int(time.time())
        info = SessionInfo(session_id="abc", updated_at=now_ts)
        # Should show HH:MM format for today
        assert ":" in info.display_time

    def test_display_time_unknown(self):
        info = SessionInfo(session_id="abc")
        assert info.display_time == "unknown"

    def test_display_time_old(self):
        old_ts = int(time.time()) - 86400 * 30  # 30 days ago
        info = SessionInfo(session_id="abc", updated_at=old_ts)
        # Should show YYYY-MM-DD format
        assert "-" in info.display_time

    def test_display_time_yesterday(self):
        yesterday_ts = int(time.time()) - 86400
        info = SessionInfo(session_id="abc", updated_at=yesterday_ts)
        assert info.display_time == "yesterday"

    def test_display_name_with_name(self):
        info = SessionInfo(session_id="abc", name="My Session")
        assert info.display_name == "My Session"

    def test_display_name_fallback_to_id(self):
        info = SessionInfo(session_id="abc123")
        assert info.display_name == "abc123"

    def test_label_minimal(self):
        info = SessionInfo(session_id="abc123")
        label = info.label
        assert "abc123" in label

    def test_label_full(self):
        info = SessionInfo(
            session_id="sess-1",
            name="Refactor auth module",
            updated_at=int(time.time()),
            run_count=5,
            summary="Worked on feature X",
            agent_name="editor",
        )
        label = info.label
        assert "Refactor auth module" in label
        assert "5 runs" in label
        assert "Worked on feature X" in label

    def test_label_with_summary_is_multiline(self):
        info = SessionInfo(
            session_id="s1",
            summary="Fixed the login bug",
        )
        label = info.label
        assert "\n" in label
        assert "Fixed the login bug" in label

    def test_label_without_summary_is_single_line(self):
        info = SessionInfo(session_id="s1")
        assert "\n" not in info.label

    def test_label_truncates_long_summary(self):
        info = SessionInfo(
            session_id="s1",
            summary="x" * 100,
        )
        label = info.label
        assert "..." in label

    def test_display_time_days_ago(self):
        three_days_ago = int(time.time()) - 86400 * 3
        info = SessionInfo(session_id="abc", updated_at=three_days_ago)
        assert info.display_time == "3d ago"


class TestQueuePanel:
    def test_initial_state(self):
        panel = QueuePanel()
        assert panel._items == []

    def test_message_types(self):
        deleted = QueuePanel.ItemDeleted(2)
        assert deleted.index == 2

        edit = QueuePanel.ItemEditRequested(1, "hello")
        assert edit.index == 1
        assert edit.text == "hello"

        closed = QueuePanel.PanelClosed()
        assert isinstance(closed, QueuePanel.PanelClosed)

    def test_preview_truncation(self):
        # Long text should be truncated to 50 chars preview
        long_text = "x" * 100
        first_line = long_text.split("\n", 1)[0].strip()
        preview = first_line if len(first_line) <= 50 else first_line[:47] + "..."
        assert len(preview) == 50  # 47 chars + "..."
        assert preview.endswith("...")

    def test_preview_short_text_not_truncated(self):
        text = "Short message"
        first_line = text.split("\n", 1)[0].strip()
        preview = first_line if len(first_line) <= 50 else first_line[:47] + "..."
        assert preview == "Short message"

    def test_preview_multiline(self):
        # Multiline: only first line shown
        text = "First line here\nSecond line\nThird line"
        first_line = text.split("\n", 1)[0].strip()
        assert first_line == "First line here"

    def test_preview_exactly_50_chars(self):
        text = "x" * 50
        first_line = text.split("\n", 1)[0].strip()
        preview = first_line if len(first_line) <= 50 else first_line[:47] + "..."
        assert preview == text  # exactly 50, no truncation


class TestRunStatsWidget:
    def test_live_widget(self):
        w = RunStatsWidget()
        w.update_tokens(100, 50)
        rendered = w.render_text()
        assert "100" in rendered

    def test_finalize(self):
        w = RunStatsWidget()
        w.update_tokens(100, 50)
        w.finalize(elapsed_override=2.3)
        rendered = w.render_text()
        assert "2.3s" in rendered

    def test_with_model(self):
        w = RunStatsWidget(model="gpt-4o")
        w.update_tokens(500, 200)
        rendered = w.render_text()
        assert "gpt-4o" in rendered

    def test_no_tokens_no_token_section(self):
        w = RunStatsWidget()
        w.finalize(elapsed_override=1.0)
        rendered = w.render_text()
        assert "Tokens:" not in rendered

    def test_fmt_time_static(self):
        assert RunStatsWidget._fmt_time(0.8) == "0.8s"
        assert RunStatsWidget._fmt_time(59.9) == "59.9s"
        assert RunStatsWidget._fmt_time(60.0) == "1m 0s"
        assert RunStatsWidget._fmt_time(90.0) == "1m 30s"


class TestSessionPickerWidget:
    def test_creates_with_sessions(self):
        sessions = [
            SessionInfo(session_id="s1", run_count=3),
            SessionInfo(session_id="s2", run_count=1),
        ]
        picker = SessionPickerWidget(sessions)
        assert picker._sessions == sessions
        assert picker.selected_index == 0

    def test_creates_with_empty_list(self):
        picker = SessionPickerWidget([])
        assert picker._sessions == []

    def test_selected_message_type(self):
        msg = SessionPickerWidget.Selected("sess-123")
        assert msg.session_id == "sess-123"

    def test_cancelled_message_type(self):
        msg = SessionPickerWidget.Cancelled()
        assert isinstance(msg, SessionPickerWidget.Cancelled)
