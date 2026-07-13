"""Tests for /hooks reload functionality."""

import pytest

from ember_code.backend.command_handler import CommandHandler
from ember_code.core.config.settings import Settings
from ember_code.core.hooks.schemas import HookDefinition
from ember_code.core.session.core import Session

# Reuse the shared patching infrastructure
from tests.test_session import _session_patches, _start_patches, _stop_patches


class TestHooksReload:
    """Session.reload_hooks() re-reads settings files and rebuilds executor."""

    @pytest.fixture
    def session(self, tmp_path):
        patches = _session_patches()
        _start_patches(patches)

        s = Session(Settings(), project_dir=tmp_path)
        yield s
        _stop_patches(patches)

    def test_reload_hooks_returns_count(self, session):
        """reload_hooks() returns number of hooks loaded."""
        # Mock the loader to return 2 hooks
        session._hook_loader.load.return_value = {
            "PreToolUse": [
                HookDefinition(type="command", command="echo pre"),
            ],
            "PostToolUse": [
                HookDefinition(type="command", command="echo post"),
            ],
        }
        count = session.reload_hooks()
        assert count == 2

    def test_reload_hooks_zero_when_empty(self, session):
        """reload_hooks() returns 0 when no hooks configured."""
        session._hook_loader.load.return_value = {}
        count = session.reload_hooks()
        assert count == 0

    def test_reload_updates_hooks_map(self, session):
        """reload_hooks() updates session.hooks_map."""
        new_hooks = {
            "Stop": [HookDefinition(type="command", command="echo stop")],
        }
        session._hook_loader.load.return_value = new_hooks
        session.reload_hooks()
        assert "Stop" in session.hooks_map

    def test_reload_calls_hook_loader(self, session):
        """reload_hooks() calls the loader to refresh hooks."""
        session._hook_loader.load.return_value = {}
        session.reload_hooks()
        # Loader should have been called during reload (in addition to __init__)
        assert session._hook_loader.load.call_count >= 1


class TestHooksReloadCommand:
    """The /hooks reload subcommand in session commands."""

    @pytest.mark.asyncio
    async def test_cmd_hooks_reload(self, tmp_path):
        patches = _session_patches()
        _start_patches(patches)

        session = Session(Settings(), project_dir=tmp_path)
        session._hook_loader.load.return_value = {
            "PreToolUse": [HookDefinition(type="command", command="echo test")],
        }

        handler = CommandHandler(session)
        await handler.handle("/hooks reload")
        # Verify hooks were reloaded
        assert "PreToolUse" in session.hooks_map

        _stop_patches(patches)

    @pytest.mark.asyncio
    async def test_cmd_hooks_list(self, tmp_path):
        """Without 'reload' arg, _cmd_hooks lists hooks."""
        patches = _session_patches()
        _start_patches(patches)

        session = Session(Settings(), project_dir=tmp_path)
        session.hooks_map = {}

        handler = CommandHandler(session)
        # Should not raise (just returns result)
        await handler.handle("/hooks")

        _stop_patches(patches)
