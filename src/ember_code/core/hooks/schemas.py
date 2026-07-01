"""Pydantic schemas for the hooks system."""

from typing import Any

from pydantic import BaseModel, Field


class HookDefinition(BaseModel):
    """A single hook definition.

    Five handler types modelled on Claude Code's catalog
    (``mcp_tool``, ``prompt``, and the ``agent`` type the spec
    also names):

    - ``command`` — shell command; the agent's payload goes in on
      stdin, exit codes drive control flow (2 blocks, 0 + JSON is
      the structured success path).
    - ``http`` — POST the payload to ``url`` with optional
      ``headers``; non-200 is non-blocking.
    - ``prompt`` — no side effect, just injects ``text`` back to
      the agent as a system reminder. Cheaper than the
      command-that-echoes-JSON pattern for nudge-style hooks.
    - ``mcp_tool`` — invokes the named MCP server tool with
      ``{event, payload, ...mcp_args}`` as input; the tool's
      stringified return becomes the hook's system message.
    """

    type: str  # "command", "http", "prompt", "mcp_tool"
    command: str = ""
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    # ``prompt`` handler: static text injected as the system
    # reminder when this hook fires.
    text: str = ""
    # ``mcp_tool`` handler: which MCP server + tool to call, plus
    # any static args merged into the call alongside
    # ``{event, payload}``.
    mcp_server: str = ""
    mcp_tool: str = ""
    mcp_args: dict[str, Any] = Field(default_factory=dict)
    matcher: str = ""
    timeout: int = 10000
    background: bool = False  # fire-and-forget, don't block the agent
    # ``asyncRewake``: hook runs in the background (like
    # ``background``), but if it exits with code 2 the combined
    # stderr+stdout is queued as a system reminder for the next
    # ``handle_message`` turn. Lets long-running hooks "wake" the
    # agent later with context. Settings.json may use either
    # ``asyncRewake`` (camelCase, CC-compatible) or
    # ``async_rewake`` — loader accepts both.
    async_rewake: bool = False


class HookResult(BaseModel):
    """Result from a hook execution.

    ``permission_decision`` is the CC-compatible structured
    envelope for ``PreToolUse``-event hooks. When set, it
    overrides the boolean ``should_continue`` for permission
    routing — the four values map onto the same
    ``PermissionDecision`` enum the evaluator uses:

    - ``"allow"`` → skip the rest of the permission pipeline
      and run the tool (the hook approved it).
    - ``"deny"`` → block the tool call, fire
      ``PermissionDenied``.
    - ``"ask"`` → fire ``PermissionRequest``, treat as deny
      until the ``canUseTool`` bridge lands.
    - ``"defer"`` (or empty) → no opinion, fall through to the
      rest of the pipeline (legacy + evaluator + tool call).

    Other event types continue to use ``should_continue`` only.
    """

    should_continue: bool = True
    message: str = ""
    permission_decision: str = ""
