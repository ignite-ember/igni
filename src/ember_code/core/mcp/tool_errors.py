"""An MCP tool that fails is a failed call, with its own words as the error.

Agno turns an MCP result with ``isError`` into an ordinary *successful*
result whose text is ``Error from MCP tool 'x': [TextContent(type='text',
text='...', annotations=None, meta=None)]`` — the content list's Python
repr. The model read that repr, and nothing downstream (the transcript's
``is_error``, Anthropic's ``is_error`` on a tool result, a ``PostToolUse``
hook) knew the call had failed, because Agno marks a call failed only
when its entrypoint raises.

So each MCP function's entrypoint is wrapped, once, when its connection
opens: the session records what went wrong (:func:`record_failure`, from
:class:`~ember_code.core.mcp.request_meta.RequestMetaSession`), and the
wrapper raises :class:`MCPToolFailed` with it — the result's text parts
joined, which is what the server meant to say. A context variable carries
it from the session to the wrapper because Agno's own ``call_tool`` sits
between them and swallows every exception into a string.
"""

from __future__ import annotations

import functools
from contextvars import ContextVar
from typing import Any

#: Set by the wrapper for the length of one call; ``None`` outside one.
_failures: ContextVar[list[tuple[str, bool]] | None] = ContextVar("mcp_tool_failures", default=None)


class MCPToolFailed(Exception):
    """Raised for a tool call that failed; its message is the server's text.

    ``answered`` says whether the server answered (a tool reporting an
    error of its own) or did not (a timeout, a closed connection). Only
    the second is a fault in the server: a tool that says "no such
    ticket" is working.
    """

    def __init__(self, message: str, *, answered: bool) -> None:
        super().__init__(message)
        self.answered = answered


def error_text(result: Any) -> str:
    """The text of a failed :class:`mcp.types.CallToolResult`, joined.

    Non-text parts are named rather than dropped, so a failure that is
    only an image still says something.
    """
    parts: list[str] = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
        else:
            parts.append(f"[{getattr(item, 'type', 'non-text')} content]")
    return "\n".join(p for p in parts if p.strip()) or "the tool reported an error and said nothing"


def record_failure(message: str, *, answered: bool) -> None:
    """Note that the call in progress failed, with ``message``."""
    holder = _failures.get()
    if holder is not None:
        holder.append((message, answered))


def raise_on_failure(mcp_tools: Any) -> None:
    """Wrap each of ``mcp_tools``'s functions so a failure raises.

    Idempotent: a function already wrapped is left alone.
    """
    for function in (getattr(mcp_tools, "functions", None) or {}).values():
        entrypoint = getattr(function, "entrypoint", None)
        if entrypoint is None or getattr(entrypoint, "_igni_raises_on_failure", False):
            continue
        function.entrypoint = _wrap(entrypoint)


def _wrap(entrypoint: Any) -> Any:
    async def call(*args: Any, **kwargs: Any) -> Any:
        holder: list[tuple[str, bool]] = []
        token = _failures.set(holder)
        try:
            result = await entrypoint(*args, **kwargs)
        finally:
            _failures.reset(token)
        if holder:
            message, answered = holder[-1]
            raise MCPToolFailed(message, answered=answered)
        return result

    # ``update_wrapper`` sets ``__wrapped__``, so Agno's ``signature()``
    # still sees the ``_agno_*`` parameters it injects.
    functools.update_wrapper(call, entrypoint)
    call._igni_raises_on_failure = True  # type: ignore[attr-defined]
    return call


__all__ = ["MCPToolFailed", "error_text", "raise_on_failure", "record_failure"]
