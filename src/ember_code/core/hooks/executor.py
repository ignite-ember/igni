"""Hook executor — runs hooks in response to events."""

import asyncio
import inspect
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

import httpx

from ember_code.core.hooks.schemas import HookDefinition, HookResult

logger = logging.getLogger(__name__)


# Hook matcher patterns shaped like ``Edit`` or ``Edit|Write`` are
# interpreted as EXACT (or pipe-list-exact) matches — Claude Code's
# convention. Anything outside this shape (regex anchors, character
# classes, dots, etc.) is treated as a regex.
_EXACT_OR_PIPE_LIST_RE = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*(?:\|[A-Za-z_][A-Za-z_0-9]*)*$")


def _hook_result_from_envelope(result: Any) -> HookResult:
    """Translate a hook-handler return value into a ``HookResult``.

    Shared by the ``mcp_tool`` handler (which gets a Python value
    back from the invoker) and any future handler that ends up
    with a structured response. Mirrors the ``command`` handler's
    stdout-JSON parser so all handler types speak the same
    envelope:

    * ``{"continue": false, "systemMessage": "blocked"}`` →
      ``HookResult(should_continue=False, message="blocked")``.
    * ``{"hookSpecificOutput": {"permissionDecision": "allow"}}`` →
      ``HookResult(permission_decision="allow")``.
    * ``{"permissionDecision": "deny"}`` (bare, fallback) → same.
    * Anything else (str, None, list, etc.) → non-blocking,
      stringified into ``message`` so the agent still sees the
      MCP tool's payload.
    """
    if isinstance(result, dict):
        pd = ""
        hso = result.get("hookSpecificOutput")
        if isinstance(hso, dict):
            pd = str(hso.get("permissionDecision", "") or "")
        if not pd:
            pd = str(result.get("permissionDecision", "") or "")
        return HookResult(
            should_continue=bool(result.get("continue", True)),
            message=str(result.get("systemMessage", "") or ""),
            permission_decision=pd,
        )
    if result is None:
        return HookResult(should_continue=True)
    return HookResult(should_continue=True, message=str(result))


def _matcher_matches(pattern: str, target: str) -> bool:
    """Claude Code-compatible tri-mode matcher.

    - Empty or ``"*"`` → always match.
    - Alphanumeric (with optional pipe-list, e.g. ``"Edit|Write"``)
      → EXACT match against the pipe-separated alternatives.
    - Anything else → ``re.search`` (case-sensitive). Malformed
      regex is treated as "no match" rather than crashing the
      whole dispatch.
    """
    if not pattern or pattern == "*":
        return True
    if _EXACT_OR_PIPE_LIST_RE.match(pattern):
        return target in pattern.split("|")
    try:
        return re.search(pattern, target) is not None
    except re.error:
        logger.debug("Malformed hook matcher %r — treating as no-match", pattern)
        return False


# Signature: ``(server, tool) -> callable | None``. The callable
# returned is the MCP tool's invoker — sync or async — taking
# keyword args and returning the tool's result. ``None`` means
# "server or tool not connected"; the executor degrades to a
# non-blocking error message in that case.
MCPResolver = Callable[[str, str], Callable[..., Any] | Awaitable[Any] | None]


class HookExecutor:
    """Executes hooks in response to events."""

    def __init__(
        self,
        hooks: dict[str, list[HookDefinition]],
        mcp_resolver: MCPResolver | None = None,
        rewake_callback: Callable[[str], None] | None = None,
    ):
        self.hooks = hooks
        # Closure handed in by Session.__init__ that knows how to
        # find an MCP tool's invoker by ``(server, tool)``. Kept
        # behind a resolver rather than holding the manager
        # directly so unit tests can mock the lookup without
        # standing up an MCP server.
        self._mcp_resolver = mcp_resolver
        # ``asyncRewake`` payload sink — when a background hook
        # exits with code 2, the executor calls this with the
        # combined stderr+stdout so the Session can queue it as a
        # system reminder for the next turn. ``None`` (the
        # default) means async_rewake hooks degrade to plain
        # ``background`` semantics (fire-and-forget, no wake).
        self._rewake_callback = rewake_callback

    def get_matching_hooks(self, event: str, target: str = "") -> list[HookDefinition]:
        """Get hooks that match the event and target.

        Matcher syntax (CC-compatible — see ``_matcher_matches``):
        empty / ``"*"`` matches all; alphanumeric (with optional
        ``|`` alternatives) is an exact / pipe-list-exact match;
        anything else is a regex.
        """
        event_hooks = self.hooks.get(event, [])
        if not target:
            return event_hooks
        return [h for h in event_hooks if _matcher_matches(h.matcher, target)]

    async def execute(
        self,
        event: str,
        payload: dict[str, Any],
        target: str = "",
    ) -> HookResult:
        """Execute all matching hooks for an event.

        Foreground hooks run in parallel and are awaited — if ANY hook blocks
        (exit 2), the tool call is blocked. Background hooks are fire-and-forget.

        Args:
            event: The event name (e.g., "PreToolUse").
            payload: JSON payload to send to hooks.
            target: Target to match against (e.g., tool name).

        Returns:
            Combined result from foreground hooks only.
        """
        hooks = self.get_matching_hooks(event, target)
        if not hooks:
            return HookResult(should_continue=True)

        fg_hooks = [h for h in hooks if not h.background]
        bg_hooks = [h for h in hooks if h.background]

        # Fire-and-forget background hooks. ``async_rewake``
        # implies a background dispatch (a sync hook with rewake
        # would be a contradiction — the agent would block waiting
        # for itself); we coerce it here so users don't have to
        # set both flags.
        for hook in bg_hooks:
            coro = self._dispatch(hook, event, payload)
            if coro is not None:
                asyncio.create_task(coro)

        for hook in fg_hooks:
            if not hook.async_rewake:
                continue
            coro = self._dispatch(hook, event, payload)
            if coro is not None:
                asyncio.create_task(coro)

        # Run foreground (non-rewake) hooks in parallel and await results
        tasks = []
        for hook in fg_hooks:
            if hook.async_rewake:
                continue
            coro = self._dispatch(hook, event, payload)
            if coro is not None:
                tasks.append(coro)

        if not tasks:
            return HookResult(should_continue=True)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Merge results
        should_continue = True
        messages = []
        # ``permission_decision`` precedence across multiple hooks
        # on the same event: deny > ask > allow > defer > "". A
        # single "deny" anywhere wins; "allow" only wins if no
        # hook denies or asks. Mirrors the conservative side of
        # CC's pipeline where security beats convenience.
        decision_priority = {"deny": 4, "ask": 3, "allow": 2, "defer": 1, "": 0}
        merged_decision = ""

        for result in results:
            if isinstance(result, Exception):
                continue  # Non-blocking errors
            if not result.should_continue:
                should_continue = False
            if result.message:
                messages.append(result.message)
            pd = (result.permission_decision or "").lower()
            if decision_priority.get(pd, 0) > decision_priority.get(merged_decision, 0):
                merged_decision = pd

        return HookResult(
            should_continue=should_continue,
            message="\n".join(messages),
            permission_decision=merged_decision,
        )

    def _dispatch(
        self,
        hook: HookDefinition,
        event: str,
        payload: dict[str, Any],
    ) -> Awaitable[HookResult] | None:
        """Pick the per-type coroutine for ``hook`` — separated from
        the ``execute`` foreground/background fan-out so adding a
        new handler type is a single entry in ``_TYPE_HANDLERS``
        instead of two ``elif`` branches."""
        handler = self._TYPE_HANDLERS.get(hook.type)
        if handler is None:
            logger.debug("Unknown hook type %r — skipping", hook.type)
            return None
        return handler(self, hook, event, payload)

    # Per-type dispatch table. Each entry normalizes to
    # ``(self, hook, event, payload) -> Awaitable[HookResult]`` so
    # the dispatch is a plain dict lookup. Types whose handler
    # signature is narrower (``prompt`` reads only ``hook``) get a
    # lambda adapter here; the ``_run_*`` methods themselves keep
    # their focused signatures.
    _TYPE_HANDLERS: ClassVar[
        dict[str, Callable[["HookExecutor", HookDefinition, str, dict[str, Any]], Awaitable[HookResult]]]
    ] = {
        "command": lambda self, hook, event, payload: self._run_command_hook(hook, payload),
        "http": lambda self, hook, event, payload: self._run_http_hook(hook, payload),
        "prompt": lambda self, hook, event, payload: self._run_prompt_hook(hook),
        "mcp_tool": lambda self, hook, event, payload: self._run_mcp_tool_hook(hook, event, payload),
    }

    async def _run_prompt_hook(self, hook: HookDefinition) -> HookResult:
        """``prompt`` handler — no side effect, just injects the
        configured text back as a system reminder. Always
        non-blocking (``should_continue=True``)."""
        return HookResult(should_continue=True, message=hook.text)

    async def _run_mcp_tool_hook(
        self,
        hook: HookDefinition,
        event: str,
        payload: dict[str, Any],
    ) -> HookResult:
        """``mcp_tool`` handler — call the named MCP server tool
        with ``{event, payload, ...mcp_args}`` and translate the
        result into a ``HookResult``.

        Result interpretation, in priority order:

        * ``dict`` with the CC envelope shape (``continue``,
          ``systemMessage``, ``hookSpecificOutput.permissionDecision``,
          bare ``permissionDecision``) — parsed identically to the
          ``command`` handler's stdout JSON, so an MCP tool can
          gate / approve / deny tool calls without learning a
          different schema.
        * ``str`` / ``None`` / anything else — stringified into
          ``message``, non-blocking.

        Missing MCP resolver, unknown server, unknown tool, and
        invoker exceptions all degrade to non-blocking (a flaky
        MCP server shouldn't tank the agent's tool call). For
        firm gating, use a ``command`` hook with exit 2.
        """
        if self._mcp_resolver is None:
            logger.debug("mcp_tool hook fired but no MCP resolver wired")
            return HookResult(should_continue=True)
        try:
            invoker = self._mcp_resolver(hook.mcp_server, hook.mcp_tool)
        except Exception as exc:
            logger.debug("MCP resolver raised for %s/%s: %s", hook.mcp_server, hook.mcp_tool, exc)
            return HookResult(should_continue=True)
        if invoker is None:
            logger.debug(
                "mcp_tool %s/%s not connected — skipping hook",
                hook.mcp_server,
                hook.mcp_tool,
            )
            return HookResult(should_continue=True)
        # ``MCPResolver`` unions Callable + Awaitable + None. In
        # practice every wired resolver returns a callable — the
        # Awaitable branch is a defensive placeholder. Narrow to
        # Callable here so mypy stops complaining about the
        # ``invoker(**call_args)`` call below, and so a resolver
        # that ever returns a bare coroutine degrades gracefully.
        if not callable(invoker):
            logger.debug(
                "mcp_tool %s/%s resolver returned non-callable — skipping",
                hook.mcp_server,
                hook.mcp_tool,
            )
            return HookResult(should_continue=True)
        try:
            call_args = {"event": event, "payload": payload, **hook.mcp_args}
            timeout_secs = hook.timeout / 1000
            result = invoker(**call_args)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=timeout_secs)
        except asyncio.TimeoutError:
            return HookResult(should_continue=True, message="MCP tool hook timed out")
        except Exception as exc:
            logger.debug("MCP tool hook %s/%s failed: %s", hook.mcp_server, hook.mcp_tool, exc)
            return HookResult(should_continue=True)
        return _hook_result_from_envelope(result)

    async def _run_command_hook(self, hook: HookDefinition, payload: dict[str, Any]) -> HookResult:
        """Run a command hook."""
        try:
            timeout_secs = hook.timeout / 1000
            payload_json = json.dumps(payload)

            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                hook.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=payload_json.encode()),
                timeout=timeout_secs,
            )

            if proc.returncode == 2:
                # Build the wake / block message — same content,
                # different destinations depending on the hook type.
                try:
                    data = json.loads(stdout.decode())
                    msg = data.get("systemMessage", "Blocked by hook")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    msg = stderr.decode().strip() or "Blocked by hook"
                # ``async_rewake``: don't block (the agent has long
                # since moved on), queue the message as a system
                # reminder for the next turn instead.
                if hook.async_rewake and self._rewake_callback is not None:
                    try:
                        self._rewake_callback(msg)
                    except Exception as exc:
                        logger.debug("rewake_callback raised: %s", exc)
                    return HookResult(should_continue=True)
                return HookResult(should_continue=False, message=msg)

            if proc.returncode == 0:
                try:
                    data = json.loads(stdout.decode())
                    # CC-compatible ``permissionDecision`` envelope
                    # (PreToolUse only — other events ignore it).
                    # ``hookSpecificOutput.permissionDecision`` is
                    # the canonical location per docs; we accept a
                    # bare top-level ``permissionDecision`` too for
                    # convenience.
                    pd = ""
                    hso = data.get("hookSpecificOutput")
                    if isinstance(hso, dict):
                        pd = str(hso.get("permissionDecision", "") or "")
                    if not pd:
                        pd = str(data.get("permissionDecision", "") or "")
                    return HookResult(
                        should_continue=data.get("continue", True),
                        message=data.get("systemMessage", ""),
                        permission_decision=pd,
                    )
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return HookResult(should_continue=True)

            # Other exit codes — non-blocking error
            return HookResult(should_continue=True)

        except asyncio.TimeoutError:
            return HookResult(should_continue=True, message="Hook timed out")
        except Exception as exc:
            logger.debug("Command hook failed: %s", exc)
            return HookResult(should_continue=True)

    async def _run_http_hook(self, hook: HookDefinition, payload: dict[str, Any]) -> HookResult:
        """Run an HTTP hook."""
        try:
            timeout_secs = hook.timeout / 1000

            # Expand env vars in headers
            headers = {}
            for k, v in hook.headers.items():
                headers[k] = os.path.expandvars(v)

            async with httpx.AsyncClient(timeout=timeout_secs) as client:
                response = await client.post(
                    hook.url,
                    json=payload,
                    headers=headers,
                )

            if response.status_code == 200:
                try:
                    data = response.json()
                    return HookResult(
                        should_continue=data.get("continue", True),
                        message=data.get("systemMessage", ""),
                    )
                except Exception as exc:
                    logger.debug("Failed to parse HTTP hook response: %s", exc)
                    return HookResult(should_continue=True)

            return HookResult(should_continue=True)

        except Exception as exc:
            logger.debug("HTTP hook failed: %s", exc)
            return HookResult(should_continue=True)
