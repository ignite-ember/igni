"""Tests for the ``prompt`` and ``mcp_tool`` hook handler types.

Earlier the executor only knew ``command`` and ``http``; these
two close the gap with Claude Code's 5-handler catalog (``agent``
is deliberately deferred).
"""

from __future__ import annotations

from typing import Any

import pytest

from ember_code.core.hooks.executor import HookExecutor
from ember_code.core.hooks.schemas import HookDefinition

# ── prompt handler ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prompt_handler_returns_text_as_message() -> None:
    """A ``prompt`` hook just surfaces its configured ``text`` as
    the system reminder. No subprocess, no network."""
    hook = HookDefinition(
        type="prompt",
        text="Remember to run make fmt before committing.",
    )
    executor = HookExecutor({"PreToolUse": [hook]})
    result = await executor.execute("PreToolUse", payload={})
    assert result.should_continue is True
    assert "make fmt" in result.message


@pytest.mark.asyncio
async def test_prompt_handler_with_matcher() -> None:
    """Matchers still apply to prompt handlers — the dispatcher
    runs `get_matching_hooks` before fanning out to types."""
    hook = HookDefinition(type="prompt", text="HIT", matcher="^edit_file$")
    executor = HookExecutor({"PreToolUse": [hook]})

    matched = await executor.execute("PreToolUse", payload={}, target="edit_file")
    assert "HIT" in matched.message

    not_matched = await executor.execute("PreToolUse", payload={}, target="file_read")
    assert not_matched.message == ""


# ── mcp_tool handler ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_tool_handler_calls_resolved_function() -> None:
    """The resolver returns an invoker; the executor calls it with
    ``{event, payload, ...mcp_args}`` and surfaces the result."""
    captured: dict[str, Any] = {}

    def fake_slack_send(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "msg-id-42"

    def resolver(server: str, tool: str):
        assert server == "slack"
        assert tool == "send_message"
        return fake_slack_send

    hook = HookDefinition(
        type="mcp_tool",
        mcp_server="slack",
        mcp_tool="send_message",
        mcp_args={"channel": "#alerts"},
    )
    executor = HookExecutor({"PreToolUse": [hook]}, mcp_resolver=resolver)
    payload = {"tool_name": "edit_file", "tool_args": {"file_path": "x.py"}}
    result = await executor.execute("PreToolUse", payload=payload)

    assert result.should_continue is True
    assert result.message == "msg-id-42"
    # Args passed to the tool: static mcp_args merged with the
    # canonical (event, payload) keys.
    assert captured["channel"] == "#alerts"
    assert captured["event"] == "PreToolUse"
    assert captured["payload"] == payload


@pytest.mark.asyncio
async def test_mcp_tool_handler_no_resolver_is_quiet() -> None:
    """Without a wired resolver, the hook silently degrades to a
    non-blocking pass-through. Better than crashing the tool call."""
    hook = HookDefinition(type="mcp_tool", mcp_server="slack", mcp_tool="x")
    executor = HookExecutor({"PreToolUse": [hook]})  # no resolver
    result = await executor.execute("PreToolUse", payload={})
    assert result.should_continue is True
    assert result.message == ""


@pytest.mark.asyncio
async def test_mcp_tool_handler_unknown_server_is_quiet() -> None:
    """Resolver returns None → hook degrades to non-blocking."""

    def resolver(server: str, tool: str):
        return None

    hook = HookDefinition(type="mcp_tool", mcp_server="missing", mcp_tool="x")
    executor = HookExecutor({"PreToolUse": [hook]}, mcp_resolver=resolver)
    result = await executor.execute("PreToolUse", payload={})
    assert result.should_continue is True


@pytest.mark.asyncio
async def test_mcp_tool_handler_invoker_exception_is_quiet() -> None:
    """If the resolved invoker raises, the hook absorbs it (the
    intent is "observation / cross-tool integration" — should not
    tank the agent's tool call)."""

    def explosive(**kwargs: Any) -> str:
        raise RuntimeError("boom")

    def resolver(server: str, tool: str):
        return explosive

    hook = HookDefinition(type="mcp_tool", mcp_server="x", mcp_tool="y")
    executor = HookExecutor({"PreToolUse": [hook]}, mcp_resolver=resolver)
    result = await executor.execute("PreToolUse", payload={})
    assert result.should_continue is True


@pytest.mark.asyncio
async def test_mcp_tool_handler_awaits_async_invoker() -> None:
    """Async MCP tool invokers (the common case via Agno) get
    awaited with the hook's configured timeout."""

    async def async_invoker(**kwargs: Any) -> str:
        return "async-result"

    def resolver(server: str, tool: str):
        return async_invoker

    hook = HookDefinition(type="mcp_tool", mcp_server="x", mcp_tool="y", timeout=2000)
    executor = HookExecutor({"PreToolUse": [hook]}, mcp_resolver=resolver)
    result = await executor.execute("PreToolUse", payload={})
    assert result.message == "async-result"


# ── unknown type ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_type_is_skipped() -> None:
    """A typo in the ``type`` field skips the hook instead of
    crashing the pipeline (logged at debug)."""
    hook = HookDefinition(type="commandd", command="echo x")  # typo
    executor = HookExecutor({"PreToolUse": [hook]})
    result = await executor.execute("PreToolUse", payload={})
    # No real hook ran → no message, but ``execute`` short-circuits
    # to the empty-tasks path so we still get a non-blocking result.
    assert result.should_continue is True


# ── loader picks up new fields ────────────────────────────────────


def test_loader_parses_prompt_fields(tmp_path: Any, monkeypatch: Any) -> None:
    """``HookLoader`` must persist ``text`` / ``mcp_*`` fields out
    of settings.json into ``HookDefinition`` so they're available
    when the executor dispatches."""
    import json
    from pathlib import Path

    from ember_code.core.hooks.loader import HookLoader

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".ember").mkdir()
    settings = {
        "hooks": {
            "PreToolUse": [
                {"type": "prompt", "text": "Hi"},
                {
                    "type": "mcp_tool",
                    "mcp_server": "slack",
                    "mcp_tool": "send",
                    "mcp_args": {"channel": "#x"},
                },
            ]
        }
    }
    (fake_home / ".ember" / "settings.json").write_text(json.dumps(settings))

    with monkeypatch.context() as m:
        m.setattr(Path, "home", lambda: fake_home)
        loader = HookLoader(tmp_path)
        hooks = loader.load()

    pre = hooks["PreToolUse"]
    assert pre[0].type == "prompt"
    assert pre[0].text == "Hi"
    assert pre[1].type == "mcp_tool"
    assert pre[1].mcp_server == "slack"
    assert pre[1].mcp_tool == "send"
    assert pre[1].mcp_args == {"channel": "#x"}


# ── mcp_tool envelope parsing ────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_tool_result_dict_continue_false_blocks() -> None:
    """An MCP tool that returns ``{"continue": False, ...}``
    blocks the tool call — same semantics as a ``command`` hook
    exiting with code 2. Lets an MCP server author a policy gate
    without learning the command-stdout schema."""

    def gate(**_kwargs: Any) -> dict:
        return {"continue": False, "systemMessage": "blocked by policy"}

    def resolver(_server: str, _tool: str):
        return gate

    hook = HookDefinition(type="mcp_tool", mcp_server="x", mcp_tool="y")
    executor = HookExecutor({"PreToolUse": [hook]}, mcp_resolver=resolver)
    result = await executor.execute("PreToolUse", payload={})
    assert result.should_continue is False
    assert "blocked by policy" in result.message


@pytest.mark.asyncio
async def test_mcp_tool_result_hookSpecificOutput_permission_decision() -> None:
    """``hookSpecificOutput.permissionDecision`` from an MCP tool
    routes through the same envelope as command-hook stdout —
    plugin authors can return ``allow`` / ``deny`` / ``ask``
    decisions over MCP."""

    def allow_all(**_kwargs: Any) -> dict:
        return {"hookSpecificOutput": {"permissionDecision": "allow"}}

    def resolver(_server: str, _tool: str):
        return allow_all

    hook = HookDefinition(type="mcp_tool", mcp_server="x", mcp_tool="y")
    executor = HookExecutor({"PreToolUse": [hook]}, mcp_resolver=resolver)
    result = await executor.execute("PreToolUse", payload={})
    assert result.permission_decision == "allow"


@pytest.mark.asyncio
async def test_mcp_tool_result_bare_permission_decision_fallback() -> None:
    """For ergonomics we also accept the bare top-level shape —
    matches what we do for command-hook stdout JSON."""

    def deny(**_kwargs: Any) -> dict:
        return {"permissionDecision": "deny", "systemMessage": "no"}

    def resolver(_server: str, _tool: str):
        return deny

    hook = HookDefinition(type="mcp_tool", mcp_server="x", mcp_tool="y")
    executor = HookExecutor({"PreToolUse": [hook]}, mcp_resolver=resolver)
    result = await executor.execute("PreToolUse", payload={})
    assert result.permission_decision == "deny"
    assert result.message == "no"


@pytest.mark.asyncio
async def test_mcp_tool_string_result_still_supported() -> None:
    """Non-dict return values still work — get stringified into
    ``message`` so an MCP tool that just returns text (an LLM
    reasoning step, a database lookup) still surfaces its output
    without forcing every author to wrap it in an envelope.
    Regression guard for the pre-envelope behaviour."""

    def just_a_string(**_kwargs: Any) -> str:
        return "raw-string-result"

    def resolver(_server: str, _tool: str):
        return just_a_string

    hook = HookDefinition(type="mcp_tool", mcp_server="x", mcp_tool="y")
    executor = HookExecutor({"PreToolUse": [hook]}, mcp_resolver=resolver)
    result = await executor.execute("PreToolUse", payload={})
    assert result.should_continue is True
    assert result.message == "raw-string-result"
    assert result.permission_decision == ""


@pytest.mark.asyncio
async def test_mcp_tool_none_result_is_quiet() -> None:
    """An MCP tool that returns ``None`` is treated as
    "observed, nothing to say" — non-blocking, empty message,
    no permission decision."""

    def silent(**_kwargs: Any) -> None:
        return None

    def resolver(_server: str, _tool: str):
        return silent

    hook = HookDefinition(type="mcp_tool", mcp_server="x", mcp_tool="y")
    executor = HookExecutor({"PreToolUse": [hook]}, mcp_resolver=resolver)
    result = await executor.execute("PreToolUse", payload={})
    assert result.should_continue is True
    assert result.message == ""
