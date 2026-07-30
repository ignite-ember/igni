"""Tests for protected path enforcement in ToolEventHook."""

import pytest

from ember_code.core.hooks.executor import HookExecutor
from ember_code.core.hooks.permission_pipeline import ProtectedPathStage
from ember_code.core.hooks.tool_hook import ToolEventHook


class TestIsProtectedPath:
    """Pins :meth:`ProtectedPathStage.matches_pattern` — the pure
    static predicate the pre-refactor ``_is_protected_path`` shim
    used to wrap."""

    def test_exact_match(self):
        assert ProtectedPathStage.matches_pattern(".env", [".env"])

    def test_glob(self):
        assert ProtectedPathStage.matches_pattern(".env.production", [".env.*"])

    def test_wildcard_ext(self):
        assert ProtectedPathStage.matches_pattern("server.pem", ["*.pem"])

    def test_no_match(self):
        assert not ProtectedPathStage.matches_pattern("app.py", [".env", "*.pem"])

    def test_full_path(self):
        assert ProtectedPathStage.matches_pattern("/project/.env", [".env"])

    def test_empty_patterns(self):
        assert not ProtectedPathStage.matches_pattern(".env", [])


class TestToolEventHookProtectedPaths:
    def _hook(self):
        return ToolEventHook(
            HookExecutor({}),
            session_id="test",
            protected_paths=[".env", ".env.*", "*.pem", "*.key", "credentials.*"],
        )

    @pytest.mark.asyncio
    async def test_blocks_write_to_env(self):
        result = await self._hook()(
            name="save_file", func=lambda **kw: "w", args={"file_path": "/project/.env"}
        )
        assert "Blocked" in result

    @pytest.mark.asyncio
    async def test_allows_normal_write(self):
        result = await self._hook()(
            name="save_file", func=lambda **kw: "ok", args={"file_path": "src/app.py"}
        )
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_allows_read_of_protected(self):
        result = await self._hook()(
            name="read_file", func=lambda **kw: "contents", args={"file_path": ".env"}
        )
        assert result == "contents"

    @pytest.mark.asyncio
    async def test_func_not_called_when_blocked(self):
        called = {}
        await self._hook()(
            name="save_file",
            func=lambda **kw: called.update(ran=True),
            args={"file_path": "credentials.json"},
        )
        assert "ran" not in called
