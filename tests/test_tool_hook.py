"""Tests for tool event hooks."""

import json
import tempfile
from pathlib import Path

import pytest

from ember_code.core.hooks.executor import HookExecutor
from ember_code.core.hooks.schemas import HookDefinition
from ember_code.core.hooks.tool_hook import ToolEventHook, _preview, _safe_args


class TestToolEventHookPassthrough:
    @pytest.mark.asyncio
    async def test_no_hooks_passes_through(self):
        hook = ToolEventHook(HookExecutor({}), session_id="test")
        result = await hook(
            name="read_file", func=lambda path="x": f"content of {path}", args={"path": "foo.py"}
        )
        assert result == "content of foo.py"

    @pytest.mark.asyncio
    async def test_propagates_exception(self):
        hook = ToolEventHook(HookExecutor({}), session_id="test")
        with pytest.raises(ValueError, match="boom"):
            await hook(name="bad", func=lambda: (_ for _ in ()).throw(ValueError("boom")), args={})

    @pytest.mark.asyncio
    async def test_none_func_returns_none(self):
        result = await ToolEventHook(HookExecutor({}), session_id="test")(
            name="noop", func=None, args={}
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_awaits_async_func(self):
        hook = ToolEventHook(HookExecutor({}), session_id="test")

        async def async_func(**kwargs):
            return "async ok"

        result = await hook(name="test", func=async_func, args={})
        assert result == "async ok"


class TestPreToolUse:
    @pytest.mark.asyncio
    async def test_blocks_tool(self):
        hooks = {"PreToolUse": [HookDefinition(type="command", command="exit 2")]}
        hook = ToolEventHook(HookExecutor(hooks), session_id="test")
        called = []
        result = await hook(name="tool", func=lambda: called.append(True) or "ran", args={})
        assert "Blocked" in str(result)
        assert called == []

    @pytest.mark.asyncio
    async def test_non_matching_passes(self):
        hooks = {
            "PreToolUse": [HookDefinition(type="command", command="exit 2", matcher="dangerous")]
        }
        hook = ToolEventHook(HookExecutor(hooks), session_id="test")
        result = await hook(name="safe", func=lambda: "ok", args={})
        assert result == "ok"


class TestPostToolUse:
    @pytest.mark.asyncio
    async def test_fires_for_matching(self):
        outfile = Path(tempfile.mktemp(suffix=".json"))
        hooks = {
            "PostToolUse": [
                HookDefinition(
                    type="command",
                    command=f"cat > {outfile}",
                    # CC-compatible exact matcher: bare alphanumeric
                    # identifiers match the tool name exactly (no
                    # substring). Use a pipe-list like
                    # ``"edit_file|save_file"`` to match multiple,
                    # or a regex like ``"^edit"`` for substring.
                    matcher="edit_file",
                )
            ]
        }
        hook = ToolEventHook(HookExecutor(hooks), session_id="s1")
        result = await hook(name="edit_file", func=lambda: "edited", args={})
        assert result == "edited"
        assert outfile.exists()
        data = json.loads(outfile.read_text())
        assert data["tool_name"] == "edit_file"
        outfile.unlink()


class TestPostToolUseFailure:
    @pytest.mark.asyncio
    async def test_fires_on_error(self):
        outfile = Path(tempfile.mktemp(suffix=".json"))
        hooks = {"PostToolUseFailure": [HookDefinition(type="command", command=f"cat > {outfile}")]}
        hook = ToolEventHook(HookExecutor(hooks), session_id="s1")

        def failing():
            raise RuntimeError("disk full")

        with pytest.raises(RuntimeError):
            await hook(name="save_file", func=failing, args={})
        assert outfile.exists()
        data = json.loads(outfile.read_text())
        assert "disk full" in data["error"]
        outfile.unlink()


class TestHelpers:
    def test_safe_args_truncates(self):
        assert len(_safe_args({"big": "x" * 1000})["big"]) == 500

    def test_preview_none(self):
        assert _preview(None) == ""

    def test_preview_truncates(self):
        assert len(_preview("x" * 1000)) == 500
