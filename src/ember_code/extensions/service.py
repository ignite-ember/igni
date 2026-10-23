"""Reaching an extension's own backend.

An authorised ``ext.<id>.<method>`` call has to arrive somewhere. That
somewhere is the extension's service: the sidecar igni supervises, or
the endpoint somebody else runs. Both speak MCP — a sidecar over
stdio, an endpoint over SSE — because EXTENSIBILITY's first principle
is that we do not invent a second tool transport, and an extension
that already exposes tools to the agent should not have to stand up a
second server to answer its own UI.

**The same server, two grant lists.** ``permissions.tools`` is what
the *agent* may call; ``permissions.rpc`` is what the extension's own
*UI* may call. They are granted by the same administrator and
enforced in different places, and an extension commonly wants one and
not the other: a headless extension has no ``rpc``, a UI-only one has
no ``tools``.

:class:`ExtensionService` is the seam. It exists because the sidecar
supervisor does not, and a test that needs an extension to answer
should not need a subprocess to get one.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class ExtensionServiceError(Exception):
    """The extension's service could not answer."""


@runtime_checkable
class ExtensionService(Protocol):
    """Whatever answers one extension's calls."""

    async def call(self, method: str, args: dict[str, Any]) -> Any:
        """Run ``method``, returning a JSON-serialisable result.

        Raises :class:`ExtensionServiceError` when the service refuses
        or fails. The message reaches the extension's own UI, so it is
        written for its author.
        """
        ...


class McpExtensionService:
    """An extension's service, reached as MCP tool calls.

    Holds the manager rather than a session because a connection comes
    and goes: a sidecar restarts, an endpoint redeploys, and resolving
    the session per call means the next call after a reconnect works
    instead of failing against a handle captured at install.
    """

    def __init__(self, *, manager: Any, server_name: str) -> None:
        self._manager = manager
        self._server_name = server_name

    @property
    def server_name(self) -> str:
        return self._server_name

    async def call(self, method: str, args: dict[str, Any]) -> Any:
        client = self._manager.get_client(self._server_name)
        if client is None:
            raise ExtensionServiceError(
                f"the service for '{self._server_name}' is not connected; "
                "it is starting, or it failed to start"
            )
        try:
            session = await client.get_session_for_run()
            result = await session.call_tool(method, args)
        except ExtensionServiceError:
            raise
        except Exception as exc:
            logger.error("extension call %s.%s failed", self._server_name, method, exc_info=True)
            raise ExtensionServiceError(
                f"'{method}' on '{self._server_name}' failed: {exc}"
            ) from exc

        if getattr(result, "isError", False):
            raise ExtensionServiceError(f"'{method}' reported: {self.unwrap(result)}")
        return self.unwrap(result)

    @staticmethod
    def unwrap(result: Any) -> Any:
        """A tool result as a value a UI can use.

        MCP answers with a content list built for a model to read, and
        a UI wants data. ``structuredContent`` is the right answer
        when a server sends it. Otherwise the text blocks are joined,
        and parsed as JSON when they are JSON — which is what a server
        written for a UI sends, and refusing to parse it would hand
        every extension the same three lines to write.

        Text that is not JSON comes back as text rather than as an
        error: a tool that returns a sentence is a tool, not a fault.
        """
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return structured

        blocks = getattr(result, "content", None) or []
        texts = [block.text for block in blocks if getattr(block, "type", None) == "text"]
        if not texts:
            # Image or resource blocks only. Nothing a UI can be handed
            # as a value, and silently returning None would look like a
            # successful empty answer.
            raise ExtensionServiceError(
                "the service answered with no text content, which a UI cannot use"
            )

        joined = "\n".join(texts)
        try:
            return json.loads(joined)
        except json.JSONDecodeError:
            return joined
