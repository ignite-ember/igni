"""Tests for TUI handler classes: InputHandler, CommandHandler, RunController queue, QueueInjectorHook."""

import sys
from unittest.mock import MagicMock

import pytest

from ember_code.backend.command_handler import CommandHandler, CommandResult
from ember_code.frontend.tui.format_helpers import format_tool_args
from ember_code.frontend.tui.input_handler import (
    SHORTCUT_HELP,
    AutocompleteProvider,
    InputHandler,
    extract_at_mention,
    process_file_mentions,
    shortcut_label,
)

# ── format_tool_args ─────────────────────────────────────────────


class TestFormatToolArgs:
    def test_none_args(self):
        assert format_tool_args(None) == ""

    def test_empty_dict(self):
        assert format_tool_args({}) == ""

    def test_simple_args(self):
        result = format_tool_args({"path": "main.py", "line": 42})
        assert "path=main.py" in result
        assert "line=42" in result

    def test_long_value_truncated(self):
        result = format_tool_args({"content": "a" * 50})
        assert "..." in result
        assert len(result) < 50

    def test_max_three_args(self):
        args = {f"key{i}": f"val{i}" for i in range(5)}
        result = format_tool_args(args)
        # Should only have 3 key=val pairs
        assert result.count("=") == 3


# ── AutocompleteProvider ──────────────────────────────────────────


class TestAutocompleteProvider:
    def test_empty_input(self):
        p = AutocompleteProvider()
        assert p.complete("") == []

    def test_non_slash(self):
        p = AutocompleteProvider()
        assert p.complete("hello") == []

    def test_double_slash_ignored(self):
        p = AutocompleteProvider()
        assert p.complete("//comment") == []

    def test_partial_match(self):
        p = AutocompleteProvider()
        matches = p.complete("/he")
        assert "/help" in matches

    def test_exact_match_returns_empty(self):
        p = AutocompleteProvider()
        # If user typed exact command, no suggestions needed
        assert p.complete("/help") == []

    def test_multiple_matches(self):
        p = AutocompleteProvider()
        # /q matches /quit
        matches = p.complete("/q")
        assert "/quit" in matches

    def test_max_five_results(self):
        p = AutocompleteProvider()
        # Even if somehow many match, capped at 5
        matches = p.complete("/")
        assert len(matches) <= 5


# ── InputHandler ──────────────────────────────────────────────────


class TestInputHandler:
    def test_on_submit_returns_stripped(self):
        h = InputHandler()
        assert h.on_submit("  hello  ") == "hello"

    def test_on_submit_empty_returns_none(self):
        h = InputHandler()
        assert h.on_submit("") is None
        assert h.on_submit("   ") is None

    def test_on_submit_pushes_to_history(self):
        h = InputHandler()
        h.on_submit("first")
        h.on_submit("second")
        assert h.history.history == ["first", "second"]

    def test_on_up_down(self):
        h = InputHandler()
        h.on_submit("cmd1")
        h.on_submit("cmd2")
        assert h.on_up("") == "cmd2"
        assert h.on_up("") == "cmd1"
        assert h.on_down() == "cmd2"

    def test_get_completions(self):
        h = InputHandler()
        matches = h.get_completions("/he")
        assert "/help" in matches


# ── shortcut_label ────────────────────────────────────────────────


class TestShortcutLabel:
    def test_ctrl_on_macos(self):
        if sys.platform == "darwin":
            assert shortcut_label("Ctrl+D") == "⌃D"
        else:
            assert shortcut_label("Ctrl+D") == "Ctrl+D"

    def test_plain_key_unchanged(self):
        assert shortcut_label("Enter") == "Enter"

    def test_shortcut_help_contains_keys(self):
        assert "send message" in SHORTCUT_HELP
        assert "quit" in SHORTCUT_HELP
        assert "input history" in SHORTCUT_HELP


# ── CommandResult ─────────────────────────────────────────────────


class TestCommandResult:
    def test_markdown_result(self):
        r = CommandResult.markdown("## Hello")
        assert r.kind == "markdown"
        assert r.content == "## Hello"
        assert r.action is None

    def test_info_result(self):
        r = CommandResult.info("done")
        assert r.kind == "info"

    def test_error_result(self):
        r = CommandResult.error("oops")
        assert r.kind == "error"

    def test_quit_result(self):
        r = CommandResult.quit()
        assert r.action == "quit"

    def test_clear_result(self):
        r = CommandResult.clear()
        assert r.action == "clear"


# ── CommandHandler ────────────────────────────────────────────────


class TestCommandHandler:
    """Tests for CommandHandler using a minimal mock session."""

    @pytest.fixture
    def mock_session(self):
        class MockSkillPool:
            def list_skills(self):
                return []

            def match_user_command(self, cmd):
                return None

        class MockPool:
            def list_agents(self):
                return []

            @property
            def agent_names(self):
                return []

        class MockPermissions:
            file_write = "ask"
            shell_execute = "ask"

        class MockModels:
            default = "test-model"

        class MockOrchestration:
            max_total_agents = 10
            max_nesting_depth = 3

        class MockStorage:
            backend = "sqlite"

        class MockDisplay:
            show_routing = False

        class MockMemory:
            enable_agentic_memory = True
            add_memories_to_context = True

        class MockKnowledge:
            enabled = False
            embedder = "ember"

        class MockLearning:
            enabled = False

        class MockReasoning:
            enabled = False

        class MockGuardrails:
            pii_detection = False
            prompt_injection = False
            moderation = False

        class MockSettings:
            models = MockModels()
            permissions = MockPermissions()
            orchestration = MockOrchestration()
            storage = MockStorage()
            memory = MockMemory()
            knowledge = MockKnowledge()
            learning = MockLearning()
            reasoning = MockReasoning()
            guardrails = MockGuardrails()
            display = MockDisplay()

        class MockPersistence:
            async def rename(self, name):
                pass

        class MockMemoryMgr:
            async def get_memories(self):
                return []

            async def optimize(self):
                return {
                    "count_before": 0,
                    "count_after": 0,
                    "message": "Not enough memories to optimize",
                }

        class MockCodeIndexSync:
            async def sync_now(self, *, sha=None):
                return None

        class MockSession:
            skill_pool = MockSkillPool()
            pool = MockPool()
            settings = MockSettings()
            session_id = "test-123"
            hooks_map = {}
            persistence = MockPersistence()
            memory_mgr = MockMemoryMgr()
            code_index_sync = MockCodeIndexSync()

        return MockSession()

    @pytest.mark.asyncio
    async def test_quit(self, mock_session):
        handler = CommandHandler(mock_session)
        result = await handler.handle("/quit")
        assert result.action == "quit"

    @pytest.mark.asyncio
    async def test_exit(self, mock_session):
        handler = CommandHandler(mock_session)
        result = await handler.handle("/exit")
        assert result.action == "quit"

    @pytest.mark.asyncio
    async def test_help_no_args_shows_panel(self, mock_session):
        handler = CommandHandler(mock_session)
        result = await handler.handle("/help")
        assert result.action == "help"

    @pytest.mark.asyncio
    async def test_help_with_topic(self, mock_session):
        handler = CommandHandler(mock_session)
        result = await handler.handle("/help schedule")
        assert result.kind == "markdown"
        assert "Schedule" in result.content

    @pytest.mark.asyncio
    async def test_help_unknown_topic(self, mock_session):
        handler = CommandHandler(mock_session)
        result = await handler.handle("/help nonexistent")
        assert result.kind == "error"

    @pytest.mark.asyncio
    async def test_agents(self, mock_session):
        """Bare ``/agents`` opens the TUI panel (action result) rather
        than printing markdown. The markdown listing was replaced by
        the panel — same data, richer surface."""
        handler = CommandHandler(mock_session)
        result = await handler.handle("/agents")
        assert result.kind == "action"
        assert result.action == "agents"

    @pytest.mark.asyncio
    async def test_skills(self, mock_session):
        """Bare ``/skills`` opens the TUI panel (action result).
        Mirrors the ``/agents`` change — panel replaces markdown
        listing."""
        handler = CommandHandler(mock_session)
        result = await handler.handle("/skills")
        assert result.kind == "action"
        assert result.action == "skills"

    @pytest.mark.asyncio
    async def test_hooks_empty(self, mock_session):
        """``/hooks`` opens the hooks panel — returns an action
        result, not an inline info message. The panel itself
        surfaces the "no hooks active" empty state."""
        handler = CommandHandler(mock_session)
        result = await handler.handle("/hooks")
        assert result.action == "hooks"

    @pytest.mark.asyncio
    async def test_clear(self, mock_session):
        handler = CommandHandler(mock_session)
        result = await handler.handle("/clear")
        assert result.action == "clear"

    @pytest.mark.asyncio
    async def test_config(self, mock_session):
        handler = CommandHandler(mock_session)
        result = await handler.handle("/config")
        assert result.kind == "markdown"
        assert "test-model" in result.content
        assert "Compression" in result.content

    @pytest.mark.asyncio
    async def test_sessions(self, mock_session):
        handler = CommandHandler(mock_session)
        result = await handler.handle("/sessions")
        assert result.action == "sessions"

    @pytest.mark.asyncio
    async def test_clear_rotates_session_id(self, mock_session):
        handler = CommandHandler(mock_session)
        old_id = mock_session.session_id
        await handler.handle("/clear")
        assert mock_session.session_id != old_id

    @pytest.mark.asyncio
    async def test_rename_no_args(self, mock_session):
        handler = CommandHandler(mock_session)
        result = await handler.handle("/rename")
        assert result.kind == "error"
        assert "Usage" in result.content

    @pytest.mark.asyncio
    async def test_rename_with_name(self, mock_session):
        handler = CommandHandler(mock_session)
        result = await handler.handle("/rename My Session")
        assert result.kind == "info"
        assert "My Session" in result.content

    @pytest.mark.asyncio
    async def test_memory_list_no_learning(self, mock_session):
        mock_session.main_team = MagicMock()
        mock_session.main_team.learning_machine = None
        mock_session._learning = None
        handler = CommandHandler(mock_session)
        result = await handler.handle("/memory")
        assert result.kind == "info"
        assert "not enabled" in result.content.lower()

    @pytest.mark.asyncio
    async def test_memory_list_with_learnings(self, mock_session):
        from unittest.mock import AsyncMock

        profile = MagicMock()
        profile.name = "Dmytro"
        profile.preferred_name = "Dmytro"
        profile.role = None
        profile.expertise = "Python"
        profile.preferences = "pytest"

        memories = MagicMock()
        memories.memories = [
            {"content": "Prefers Python over JavaScript"},
            {"content": "Uses pytest for testing"},
        ]

        learning = MagicMock()
        learning.arecall = AsyncMock(
            return_value={
                "user_profile": profile,
                "user_memory": memories,
            }
        )
        mock_session.main_team = MagicMock()
        mock_session.main_team.learning_machine = learning
        mock_session.user_id = "testuser"
        mock_session.session_id = "test123"

        handler = CommandHandler(mock_session)
        result = await handler.handle("/memory")
        assert result.kind == "markdown"
        assert "Dmytro" in result.content
        assert "pytest" in result.content

    @pytest.mark.asyncio
    async def test_memory_optimize(self, mock_session):
        async def mock_optimize():
            return {"count_before": 5, "count_after": 1, "message": "Optimized 5 memories into 1"}

        mock_session.memory_mgr.optimize = mock_optimize

        handler = CommandHandler(mock_session)
        result = await handler.handle("/memory optimize")
        assert result.kind == "info"
        assert "Optimized" in result.content

    @pytest.mark.asyncio
    async def test_memory_optimize_error(self, mock_session):
        async def mock_optimize():
            return {"error": "No db"}

        mock_session.memory_mgr.optimize = mock_optimize

        handler = CommandHandler(mock_session)
        result = await handler.handle("/memory optimize")
        assert result.kind == "error"

    @pytest.mark.asyncio
    async def test_unknown_command(self, mock_session):
        handler = CommandHandler(mock_session)
        result = await handler.handle("/nonexistent")
        assert result.kind == "error"
        assert "Unknown" in result.content


# ── RunController queue ──────────────────────────────────────────


class TestRunControllerQueue:
    """Tests for the message queue in RunController."""

    def _make_controller(self):
        from ember_code.frontend.tui.run_controller import RunController

        ctrl = RunController.__new__(RunController)
        ctrl._queue = []
        ctrl._processing = False
        ctrl._current_task = None
        ctrl._queue_hook = None
        ctrl._app = None
        ctrl._conversation = None
        ctrl._status = None
        ctrl._hitl = None
        ctrl._session = None
        ctrl._stream_widget = None
        ctrl._spinner = None
        ctrl._run_input_tokens = 0
        ctrl._run_output_tokens = 0
        ctrl._streamed = False
        return ctrl

    def test_enqueue_returns_position(self):
        ctrl = self._make_controller()
        # enqueue calls _sync_queue_panel which needs _app, so patch it
        ctrl._sync_queue_panel = lambda: None
        assert ctrl.enqueue("first") == 1
        assert ctrl.enqueue("second") == 2
        assert ctrl.queue_size == 2

    def test_enqueue_no_limit(self):
        ctrl = self._make_controller()
        ctrl._sync_queue_panel = lambda: None
        for i in range(100):
            ctrl.enqueue(f"msg-{i}")
        assert ctrl.queue_size == 100

    def test_dequeue_at(self):
        ctrl = self._make_controller()
        ctrl._sync_queue_panel = lambda: None
        ctrl.enqueue("a")
        ctrl.enqueue("b")
        ctrl.enqueue("c")
        removed = ctrl.dequeue_at(1)
        assert removed == "b"
        assert ctrl.queue_size == 2
        assert ctrl._queue == ["a", "c"]

    def test_dequeue_at_invalid(self):
        ctrl = self._make_controller()
        ctrl._sync_queue_panel = lambda: None
        ctrl.enqueue("a")
        assert ctrl.dequeue_at(5) is None
        assert ctrl.dequeue_at(-1) is None
        assert ctrl.queue_size == 1

    def test_queue_size_property(self):
        ctrl = self._make_controller()
        assert ctrl.queue_size == 0
        ctrl._queue.append("x")
        assert ctrl.queue_size == 1


# ── QueueInjectorHook ────────────────────────────────────────────


class TestQueueInjectorHook:
    """Tests for the tool hook that injects queued messages mid-run."""

    def _make_hook(self, queue=None, on_inject=None, on_queue_changed=None):
        from ember_code.core.queue_hook import QueueInjectorHook

        return QueueInjectorHook(
            queue=queue if queue is not None else [],
            on_inject=on_inject,
            on_queue_changed=on_queue_changed,
        )

    @pytest.mark.asyncio
    async def test_calls_next_func_and_returns_result(self):
        hook = self._make_hook()
        result = await hook(name="my_tool", func=lambda **kw: "tool_result", args={})
        assert result == "tool_result"

    @pytest.mark.asyncio
    async def test_calls_sync_next_func(self):
        hook = self._make_hook()
        result = await hook(name="my_tool", func=lambda **kw: "sync_result", args={})
        assert result == "sync_result"

    @pytest.mark.asyncio
    async def test_appends_queued_messages_to_result(self):
        queue = ["hello from user"]
        hook = self._make_hook(queue=queue)
        result = await hook(name="tool", func=lambda **kw: "ok", args={})
        assert result.startswith("ok")
        assert "hello from user" in result
        assert queue == []

    @pytest.mark.asyncio
    async def test_no_persistent_state_between_calls(self):
        # Each call drains its own queue snapshot — no leakage between calls.
        queue = ["msg1"]
        hook = self._make_hook(queue=queue)
        first = await hook(name="tool", func=lambda **kw: "first", args={})
        assert "msg1" in first

        second = await hook(name="tool", func=lambda **kw: "second", args={})
        assert second == "second"  # nothing left in queue, no augmentation

    @pytest.mark.asyncio
    async def test_on_inject_callback(self):
        injected = []
        queue = ["a", "b"]
        hook = self._make_hook(queue=queue, on_inject=lambda msg: injected.append(msg))
        await hook(name="tool", func=lambda **kw: "ok", args={})
        assert injected == ["a", "b"]

    @pytest.mark.asyncio
    async def test_on_queue_changed_callback(self):
        changed_count = []
        queue = ["x"]
        hook = self._make_hook(queue=queue, on_queue_changed=lambda: changed_count.append(1))
        await hook(name="tool", func=lambda **kw: "ok", args={})
        assert len(changed_count) == 1

    @pytest.mark.asyncio
    async def test_drains_queue_without_agent(self):
        # Tool result is what flows through to the model — agent kwarg is optional.
        queue = ["msg"]
        hook = self._make_hook(queue=queue)
        result = await hook(name="tool", func=lambda **kw: "ok", args={}, agent=None)
        assert "msg" in result
        assert queue == []

    def test_reset_is_a_noop(self):
        # Kept for API compatibility; should not raise.
        hook = self._make_hook()
        hook.reset()
        hook.reset()

    def test_create_queue_hook_factory(self):
        from ember_code.core.queue_hook import (
            QueueInjectorHook,
            QueuePersisterHook,
            create_queue_hook,
        )

        queue = []
        injector, persister = create_queue_hook(queue)
        assert isinstance(injector, QueueInjectorHook)
        assert isinstance(persister, QueuePersisterHook)
        assert injector._queue is queue
        assert persister._injector is injector


# ── extract_at_mention ───────────────────────────────────────────


class TestExtractAtMention:
    """Tests for @file mention token extraction."""

    def _line(self, text):
        """Helper: returns a get_line callable for single-line input."""
        return lambda row: text

    def test_at_start_of_line(self):
        assert extract_at_mention(0, 1, self._line("@")) == ""

    def test_at_with_query(self):
        assert extract_at_mention(0, 5, self._line("@src/")) == "src/"

    def test_at_mid_line(self):
        # "look at this file @src/utils" — @ is at pos 18, end at 28
        assert extract_at_mention(0, 28, self._line("look at this file @src/utils")) == "src/utils"

    def test_email_not_matched(self):
        # No whitespace before @ — not a mention
        assert extract_at_mention(0, 10, self._line("user@email")) is None

    def test_no_at_in_text(self):
        assert extract_at_mention(0, 5, self._line("hello")) is None

    def test_at_after_space(self):
        assert extract_at_mention(0, 8, self._line("check @f")) == "f"

    def test_cursor_at_start(self):
        assert extract_at_mention(0, 0, self._line("@test")) is None

    def test_at_with_deep_path(self):
        text = "@src/ember_code/tui/widgets/_file_picker.py"
        assert (
            extract_at_mention(0, len(text), self._line(text))
            == "src/ember_code/tui/widgets/_file_picker.py"
        )

    def test_multiple_at_picks_nearest(self):
        text = "@first @second"
        # Cursor at end — should find @second
        assert extract_at_mention(0, 14, self._line(text)) == "second"

    def test_whitespace_breaks_scan(self):
        # Cursor after a space following @mention — no active mention
        text = "@file rest"
        assert extract_at_mention(0, 10, self._line(text)) is None


# ── process_file_mentions ────────────────────────────────────────


class TestProcessFileMentions:
    """Tests for @file mention processing before sending to LLM."""

    def test_single_mention(self):
        cleaned, paths = process_file_mentions("fix @src/main.py please")
        assert paths == ["src/main.py"]
        assert "@" not in cleaned
        assert "src/main.py" in cleaned
        assert "[Referenced files:" in cleaned

    def test_multiple_mentions(self):
        cleaned, paths = process_file_mentions("compare @a.py and @b.py")
        assert paths == ["a.py", "b.py"]
        assert cleaned.count("@") == 0
        assert "a.py" in cleaned
        assert "b.py" in cleaned

    def test_no_mentions(self):
        cleaned, paths = process_file_mentions("just a normal message")
        assert paths == []
        assert cleaned == "just a normal message"
        assert "[Referenced" not in cleaned

    def test_email_not_stripped(self):
        cleaned, paths = process_file_mentions("contact user@example.com")
        assert paths == []
        assert "user@example.com" in cleaned

    def test_at_start_of_line(self):
        cleaned, paths = process_file_mentions("@src/utils/media.py has a bug")
        assert paths == ["src/utils/media.py"]
        assert "src/utils/media.py has a bug" in cleaned

    def test_hint_prepended(self):
        cleaned, paths = process_file_mentions("look at @config.yaml")
        lines = cleaned.split("\n")
        assert lines[0].startswith("[Referenced files:")
        assert "read before responding" in lines[0]

    def test_deep_path(self):
        text = "review @src/ember_code/tui/widgets/_file_picker.py"
        cleaned, paths = process_file_mentions(text)
        assert paths == ["src/ember_code/tui/widgets/_file_picker.py"]

    def test_mention_with_dots(self):
        cleaned, paths = process_file_mentions("check @pyproject.toml")
        assert paths == ["pyproject.toml"]
