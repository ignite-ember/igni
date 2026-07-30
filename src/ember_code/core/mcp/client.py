"""MCP client manager — orchestrates connections to external MCP servers.

Reference subsystem manager (cited in ``CODE_STANDARDS.md``).
Owns the lifecycle: config lookup → gate checks →
transport-specific open → tool-filter application → cache the
live handle. Every previously-inlined concern lives in a
focused collaborator now:

* :class:`~ember_code.core.mcp.config.MCPConfigLoader` — loads
  ``.mcp.json`` / managed-settings config.
* :class:`~ember_code.core.mcp.approval.MCPApprovalManager` —
  first-use user-approval prompt.
* :class:`~ember_code.core.mcp.tool_filter.MCPToolFilter` —
  per-tool enable/disable (composed as :attr:`_tool_filter`).
* :class:`~ember_code.core.mcp.stdio_binding.StdioMCPBinding` —
  the stdio adapter that encapsulates every Agno
  :class:`MCPTools` private-attr write behind one named seam.
* :class:`~ember_code.core.mcp.schemas.MCPConnectResult` —
  the explicit Result type :meth:`connect` returns; callers
  branch on ``result.ok`` and read ``result.reason`` for the
  failure explanation.

Wire shapes returned by :meth:`get_resources` /
:meth:`get_prompts` / :meth:`list_tool_info` are the Pydantic
models in :mod:`ember_code.core.mcp.schemas` — raw ``dict``
returns no longer cross the RPC boundary.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

# Optional deps — ``agno[mcp]`` isn't a hard requirement of the base
# install. The module still loads without them; ``connect`` reports
# a clean "not installed" error when the manager tries to use them.
# Matches the ``pwd`` pattern in ``frontend/tui/app.py`` (iter 22).
try:
    from agno.tools.mcp import MCPTools as _MCPTools
except ImportError:  # pragma: no cover — exercised by test_connect_import_error
    _MCPTools = None  # type: ignore[assignment,misc]

from ember_code.core.mcp.approval import MCPApprovalManager
from ember_code.core.mcp.config import MCPConfigLoader, MCPPolicy, MCPServerConfig
from ember_code.core.mcp.schemas import (
    MCPConnectResult,
    MCPPrompt,
    MCPResource,
    MCPToolInfo,
)
from ember_code.core.mcp.stdio_binding import StdioMCPBinding
from ember_code.core.mcp.tool_filter import MCPToolFilter
from ember_code.core.mcp.tool_state import MCPToolStateStore

if TYPE_CHECKING:
    from agno.tools.mcp import MCPTools

logger = logging.getLogger(__name__)


class MCPClientManager:
    """Manages connections to external MCP servers.

    Focused surface after the extraction refactor: connection
    lifecycle (:meth:`connect`, :meth:`disconnect_all`,
    :meth:`disconnect_one`), client lookup
    (:meth:`get_client`, :meth:`all_clients`), server
    enumeration (:meth:`list_servers`, :meth:`list_connected`,
    :meth:`list_required`), and per-tool state delegation to
    the composed :class:`MCPToolFilter`.
    """

    def __init__(
        self,
        project_dir: Path | str | None = None,
        *,
        policy: MCPPolicy | None = None,
        stdio_binding: StdioMCPBinding | None = None,
    ):
        self.configs = MCPConfigLoader(project_dir).load()
        self._clients: dict[str, MCPTools] = {}
        self._approval = MCPApprovalManager()
        self._policy: MCPPolicy = (
            policy if policy is not None else MCPPolicy.from_managed_settings()
        )
        self._project_dir: Path | None = Path(project_dir) if project_dir else None
        # Disabled-tools persistence extracted to ``MCPToolStateStore``
        # so this manager stays focused on connection lifecycle.
        # The store is composed by ``MCPToolFilter`` below.
        tool_state = MCPToolStateStore(self._project_dir)
        self._tool_filter = MCPToolFilter(tool_state)
        # Stdio adapter — every ``mcp_tools._*`` private-attr
        # write lives inside :meth:`StdioMCPBinding.open`.
        self._stdio_binding = stdio_binding or StdioMCPBinding(
            StdioMCPBinding.default_errlog_path()
        )

    # ── Connection gate ───────────────────────────────────────────

    def _check_connect_gate(self, name: str, config: MCPServerConfig) -> str | None:
        """Run every "should this connect proceed" check.

        Returns ``None`` on green-light or the reason string if
        any gate rejects. Gates fire in order: managed-policy
        deny, managed-policy not-allowed, user first-use
        approval, MCP SDK availability. Callers wrap the
        returned reason in an :class:`MCPConnectResult.failure`
        and short-circuit.
        """
        if self._policy.is_denied(name):
            logger.warning("MCP '%s' blocked by managed policy (denied)", name)
            return f"Server '{name}' is blocked by admin policy"

        if not self._policy.is_allowed(name):
            logger.warning("MCP '%s' blocked by managed policy (not allowed)", name)
            return f"Server '{name}' is not in the allowed list"

        if not self._approval.check_approval(name, config.source_path):
            logger.info("MCP '%s' denied by user approval prompt", name)
            return "User denied MCP server connection"

        if _MCPTools is None:
            logger.warning("MCP connect '%s' failed: missing dependencies", name)
            return "MCP dependencies not installed (pip install agno[mcp])"
        return None

    # ── Connection lifecycle ──────────────────────────────────────

    async def connect(self, name: str) -> MCPConnectResult:
        """Connect to an MCP server by name.

        Returns an :class:`MCPConnectResult`: on success,
        ``result.ok is True`` and ``result.client`` is the live
        :class:`MCPTools` handle; on failure, ``result.ok is
        False`` and ``result.reason`` is the human-readable
        explanation. Callers should cache the Result at
        connect time — there's no post-hoc ``get_error``
        accessor.
        """
        cached = self._clients.get(name)
        if cached is not None:
            return MCPConnectResult.success(cached)

        config = self.configs.get(name)
        if not config:
            return MCPConnectResult.failure("No config found")

        gate_error = self._check_connect_gate(name, config)
        if gate_error is not None:
            return MCPConnectResult.failure(gate_error)

        try:
            mcp_tools = await self._open_transport(name, config)
        except MCPConnectError as exc:
            return MCPConnectResult.failure(str(exc))
        except Exception as exc:
            logger.warning("MCP connect '%s' failed: %s", name, exc)
            return MCPConnectResult.failure(str(exc))

        if mcp_tools is None:
            # _open_transport already logged and reported via the
            # exception path; this branch keeps the type-checker
            # happy for the "unsupported transport" case where we
            # return None instead of raising.
            return MCPConnectResult.failure(f"Unsupported MCP type: {config.type}")

        # Verify the MCP server actually provides tools.
        functions = getattr(mcp_tools, "functions", None) or {}
        if not functions:
            logger.warning("MCP '%s' connected but has no tools — closing", name)
            try:
                await mcp_tools.__aexit__(None, None, None)
            except Exception as exc:  # pragma: no cover — cleanup best-effort
                logger.debug("MCP '%s' cleanup after empty-tools failed: %s", name, exc)
            return MCPConnectResult.failure(
                "MCP server connected but returned no tools. "
                "Ensure the IDE has MCP support enabled."
            )

        # Cache the full functions dict so we can restore an
        # individually-disabled tool without reconnecting later.
        self._tool_filter.snapshot(name, mcp_tools)
        self._clients[name] = mcp_tools
        self._tool_filter.apply(name, mcp_tools)
        return MCPConnectResult.success(mcp_tools)

    async def _open_transport(self, name: str, config: MCPServerConfig) -> MCPTools | None:
        """Dispatch to the transport-specific opener.

        Raises :class:`MCPConnectError` with a caller-facing
        reason string for config-level failures (missing
        ``url``, empty tool set). Returns ``None`` for the
        unsupported-type case so :meth:`connect` can build the
        Result with the transport type interpolated.
        """
        transport = str(getattr(config.type, "value", config.type))
        if transport == "sse":
            if not config.url:
                raise MCPConnectError("SSE transport requires a 'url' field")
            mcp_tools = _MCPTools(url=config.url, transport="sse")
            await mcp_tools.__aenter__()
            return mcp_tools
        if transport == "stdio":
            return await self._stdio_binding.open(name, config)
        return None

    async def disconnect_all(self) -> None:
        """Disconnect from all MCP servers.

        SSE connections use anyio task groups internally.
        During shutdown the exit may run in a different task
        than the entry, causing RuntimeError from anyio's
        cancel scope. For SSE clients we skip ``__aexit__``
        entirely — the connection is abandoned and the OS
        cleans up the socket on process exit.
        """
        for name, client in list(self._clients.items()):
            # ``MCPTransport`` is a str-mixin enum, so ``==`` against the
            # string literal works for both enum members and raw strings
            # (config dicts loaded from disk). ``str(...)`` would render
            # as ``"MCPTransport.sse"`` on the enum branch — wrong.
            transport = getattr(self.configs.get(name), "type", "")
            if transport == "sse":
                # SSE async generators can't be closed across tasks.
                # Just drop the reference — the OS reclaims the socket.
                logger.debug("MCP '%s' (SSE) — abandoning connection", name)
                continue
            try:
                await client.__aexit__(None, None, None)
            except BaseException as exc:
                logger.debug("MCP '%s' disconnect error (safe to ignore): %s", name, exc)
        # Drop per-server tool-filter snapshots — a subsequent reconnect
        # must re-snapshot the live functions dict, not reuse the stale
        # pre-disconnect one.
        for name in list(self._clients.keys()):
            self._tool_filter.forget(name)
        self._clients.clear()

    async def disconnect_one(self, name: str) -> bool:
        """Disconnect a single MCP server by name.

        Returns ``True`` if the server was previously connected
        and its reference has been dropped, ``False`` if the
        server was never in the connected set. MCP client
        ``__aexit__`` triggers anyio cancel scope errors when
        called from a different task than it was created in —
        we abandon the connection and let the OS reclaim the
        subprocess / socket on process exit.
        """
        client = self._clients.pop(name, None)
        if client is None:
            return False
        self._tool_filter.forget(name)
        logger.debug("MCP '%s' — dropping connection reference", name)
        return True

    # ── Client lookup ─────────────────────────────────────────────

    def get_client(self, name: str) -> MCPTools | None:
        """Return the connected MCP client for ``name``, or
        ``None``.

        Public accessor for the internal ``_clients`` dict —
        lets callers (the main-agent builder, plugin wiring
        code) look up an active client without reaching into a
        private attribute. Returns ``None`` for both "never
        connected" and "disconnected" so callers get a single
        missing-client idiom.
        """
        return self._clients.get(name)

    def all_clients(self) -> dict[str, MCPTools]:
        """Return a shallow copy of the ``name → client``
        mapping.

        Public accessor over the internal ``_clients`` dict —
        lets collaborators (:class:`MCPToolResolver`,
        :class:`CodeIndexAvailabilityRefresher`) walk every
        live client without reaching into a private
        attribute. The returned dict is a copy so external
        mutations don't leak into the manager's own state.
        """
        return dict(self._clients)

    # ── Server enumeration ────────────────────────────────────────

    def list_servers(self) -> list[str]:
        """List available MCP server names."""
        return list(self.configs.keys())

    def list_connected(self) -> list[str]:
        """List currently connected MCP server names."""
        return list(self._clients.keys())

    def list_required(self) -> list[str]:
        """List servers required by admin policy that are not
        yet connected."""
        connected = set(self._clients.keys())
        return [s for s in self._policy.required if s not in connected]

    def is_policy_denied(self, name: str) -> bool:
        """Whether the managed policy explicitly denies this
        server. Public wrapper around ``self._policy.is_denied``
        so panel controllers don't have to reach into the
        manager's private ``_policy`` attribute."""
        return self._policy.is_denied(name)

    # ── Per-tool enable/disable (thin delegates) ─────────────────
    # In-memory + on-disk state lives on ``self._tool_filter``.
    # These delegates keep :class:`MCPServerSnapshot` and the
    # ``/mcp`` command path stable without needing to know
    # about the filter type.

    def get_tools(self, name: str) -> list[str]:
        """Return tool names provided by a connected MCP server.

        Returns the full set including individually-disabled
        tools so the panel can show them with a disabled state.
        Use :meth:`get_disabled_tools` for the per-tool toggle
        state.
        """
        return self._tool_filter.list_tools(name, self._clients.get(name))

    def get_disabled_tools(self, name: str) -> list[str]:
        """Tools the user has individually disabled on this
        server."""
        return self._tool_filter.list_disabled(name)

    def get_tool_descriptions(self, name: str) -> dict[str, str]:
        """Return ``{tool_name: description}`` for a connected
        MCP server."""
        return self._tool_filter.tool_descriptions(name, self._clients.get(name))

    def list_tool_info(self, name: str) -> list[MCPToolInfo]:
        """Return the packaged name+description+enabled rows
        for every tool on server ``name``.

        One typed call replaces the pre-refactor three-way
        stitch (``get_tools`` + ``get_tool_descriptions`` +
        ``get_disabled_tools``) in
        :class:`~ember_code.backend.schemas_mcp.MCPServerSnapshot`.
        """
        return self._tool_filter.list_tool_info(name, self._clients.get(name))

    def set_tool_enabled(self, server: str, tool: str, enabled: bool) -> None:
        """Toggle a single tool on a server. Persists state and
        re-applies the filter so the change is visible to the
        next agent run."""
        self._tool_filter.set_enabled(server, tool, enabled, self._clients.get(server))

    # ── Resources / prompts (typed wire shapes) ─────────────────

    async def get_resources(self, name: str) -> list[MCPResource]:
        """Resources exposed by a connected MCP server.

        Servers that don't implement the resources capability
        raise "Method not found" — treated as an empty list.
        Return shape is documented by :class:`MCPResource`.
        """
        session = getattr(self._clients.get(name), "session", None)
        if session is None:
            return []
        try:
            result = await session.list_resources()
        except Exception as exc:
            logger.debug("MCP '%s' list_resources failed: %s", name, exc)
            return []
        return [MCPResource.from_sdk(r) for r in (result.resources or [])]

    async def get_prompts(self, name: str) -> list[MCPPrompt]:
        """Prompts exposed by a connected MCP server.

        Return shape is documented by :class:`MCPPrompt`.
        """
        session = getattr(self._clients.get(name), "session", None)
        if session is None:
            return []
        try:
            result = await session.list_prompts()
        except Exception as exc:
            logger.debug("MCP '%s' list_prompts failed: %s", name, exc)
            return []
        return [MCPPrompt.from_sdk(p) for p in (result.prompts or [])]


class MCPConnectError(Exception):
    """Raised by :meth:`MCPClientManager._open_transport` when a
    transport-level configuration error should surface as an
    :class:`MCPConnectResult.failure` reason (rather than as an
    uncaught exception in the ``except`` path). Kept private to
    this module — external callers branch on
    :attr:`MCPConnectResult.ok` instead."""
