"""MCP client — connects to external MCP servers.

For stdio transport, we bypass Agno's default ``MCPTools.__aenter__``
and connect manually using the MCP SDK's ``stdio_client`` with
``errlog`` redirected to a file.  This avoids Textual rendering
corruption caused by subprocess stderr mixing with Textual's output.
"""

import logging
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

# Optional deps — ``agno[mcp]`` isn't a hard requirement of the base
# install. The module still loads without them; ``connect`` reports
# a clean "not installed" error when the manager tries to use them.
# Matches the ``pwd`` pattern in ``frontend/tui/app.py`` (iter 22).
try:
    from agno.tools.mcp import MCPTools
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
except ImportError:  # pragma: no cover — exercised by test_connect_import_error
    MCPTools = None  # type: ignore[assignment,misc]
    ClientSession = None  # type: ignore[assignment,misc]
    StdioServerParameters = None  # type: ignore[assignment,misc]
    stdio_client = None  # type: ignore[assignment]

from ember_code.core.mcp.approval import MCPApprovalManager
from ember_code.core.mcp.config import MCPConfigLoader, MCPPolicy
from ember_code.core.mcp.tool_state import MCPToolStateStore

logger = logging.getLogger(__name__)

_MCP_ERRLOG_PATH = os.path.join(tempfile.gettempdir(), "ember_mcp_stderr.log")


class MCPClientManager:
    """Manages connections to external MCP servers."""

    def __init__(self, project_dir=None, *, policy: MCPPolicy | None = None):
        self.configs = MCPConfigLoader(project_dir).load()
        self._clients: dict[str, Any] = {}
        # Snapshot of every server's full ``functions`` dict at the
        # time we first connected. We restore from here whenever the
        # disabled set changes, so a re-enabled tool comes back
        # without a reconnect.
        self._original_functions: dict[str, dict[str, Any]] = {}
        self._errors: dict[str, str] = {}
        self._approval = MCPApprovalManager()
        self._policy: MCPPolicy = (
            policy if policy is not None else MCPPolicy.from_managed_settings()
        )
        self._project_dir: Path | None = Path(project_dir) if project_dir else None
        # Disabled-tools persistence extracted to ``MCPToolStateStore``
        # so this manager stays focused on connection lifecycle.
        self._tool_state = MCPToolStateStore(self._project_dir)
        self._disabled_tools: dict[str, set[str]] = self._tool_state.load()

    def _check_connect_gate(self, name: str, config: Any) -> str | None:
        """Run every "should this connect proceed" check. Returns
        ``None`` on green-light or the reason string if any gate
        rejects. Gates fire in order: managed-policy deny, managed-
        policy not-allowed, user first-use approval, MCP SDK
        availability. Callers record the reason on ``self._errors``
        and short-circuit."""
        if self._policy.is_denied(name):
            logger.warning("MCP '%s' blocked by managed policy (denied)", name)
            return f"Server '{name}' is blocked by admin policy"

        if not self._policy.is_allowed(name):
            logger.warning("MCP '%s' blocked by managed policy (not allowed)", name)
            return f"Server '{name}' is not in the allowed list"

        if not self._approval.check_approval(name, config.source_path):
            logger.info("MCP '%s' denied by user approval prompt", name)
            return "User denied MCP server connection"

        if MCPTools is None:
            logger.warning("MCP connect '%s' failed: missing dependencies", name)
            return "MCP dependencies not installed (pip install agno[mcp])"
        return None

    async def connect(self, name: str) -> Any | None:
        """Connect to an MCP server by name.

        Returns Agno MCPTools instance or None if connection fails.
        """
        if name in self._clients:
            return self._clients[name]

        config = self.configs.get(name)
        if not config:
            self._errors[name] = "No config found"
            return None

        gate_error = self._check_connect_gate(name, config)
        if gate_error is not None:
            self._errors[name] = gate_error
            return None

        try:
            if config.type == "sse":
                if not config.url:
                    self._errors[name] = "SSE transport requires a 'url' field"
                    return None
                mcp_tools = MCPTools(url=config.url, transport="sse")
                await mcp_tools.__aenter__()
            elif config.type == "stdio":
                mcp_tools = await self._connect_stdio(name, config)
            else:
                self._errors[name] = f"Unsupported MCP type: {config.type}"
                return None

            # Verify the MCP server actually provides tools
            functions = getattr(mcp_tools, "functions", None) or {}
            if not functions:
                self._errors[name] = (
                    "MCP server connected but returned no tools. "
                    "Ensure the IDE has MCP support enabled."
                )
                logger.warning("MCP '%s' connected but has no tools — closing", name)
                await mcp_tools.__aexit__(None, None, None)
                return None

            # Cache the full functions dict so we can restore an
            # individually-disabled tool without reconnecting later.
            funcs = getattr(mcp_tools, "functions", None)
            if isinstance(funcs, dict):
                self._original_functions[name] = dict(funcs)
            self._clients[name] = mcp_tools
            self._apply_disabled(name)
            return mcp_tools
        except Exception as exc:
            self._errors[name] = str(exc)
            logger.warning("MCP connect '%s' failed: %s", name, exc)
            return None

    async def _connect_stdio(self, name: str, config: Any) -> Any:
        """Connect to an MCP stdio server with errlog redirected.

        Bypasses Agno's ``MCPTools.__aenter__`` and connects manually
        using the MCP SDK's ``stdio_client`` with ``errlog`` sent to a
        log file instead of ``sys.stderr`` (which Textual uses for
        TUI rendering).
        """
        errlog = open(_MCP_ERRLOG_PATH, "a")  # noqa: SIM115 — must stay open for MCP session lifetime
        params = StdioServerParameters(
            command=config.command,
            args=config.args or [],
            env=config.env if config.env else None,
        )

        mcp_tools = MCPTools(
            server_params=params,
            transport="stdio",
            tool_name_prefix=f"mcp_{name}",
        )

        # Connect using MCP SDK directly with errlog redirected
        mcp_tools._context = stdio_client(params, errlog=errlog)  # type: ignore[assignment]
        session_params = await mcp_tools._context.__aenter__()
        mcp_tools._active_contexts = [mcp_tools._context]
        read, write = session_params[0:2]

        timeout = getattr(mcp_tools, "timeout_seconds", 30) or 30
        mcp_tools._session_context = ClientSession(  # type: ignore[assignment]
            read, write, read_timeout_seconds=timedelta(seconds=timeout)
        )
        mcp_tools.session = await mcp_tools._session_context.__aenter__()
        mcp_tools._active_contexts.append(mcp_tools._session_context)

        # Initialize Agno tool functions from MCP session.
        # tool_name_prefix ensures MCP tools don't collide with built-in tools
        # (e.g. read_file → mcp_filesystem_read_file)
        await mcp_tools.initialize()
        mcp_tools._errlog = errlog

        return mcp_tools

    def get_error(self, name: str) -> str:
        """Return the last connection error for a server, or empty string."""
        return self._errors.get(name, "")

    async def disconnect_all(self):
        """Disconnect from all MCP servers.

        SSE connections use anyio task groups internally. During shutdown
        the exit may run in a different task than the entry, causing
        RuntimeError from anyio's cancel scope.  For SSE clients we
        skip __aexit__ entirely — the connection is abandoned and the
        OS cleans up the socket on process exit.
        """
        for name, client in list(self._clients.items()):
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
        self._clients.clear()

    async def disconnect_one(self, name: str) -> bool:
        """Disconnect a single MCP server by name. Returns True if disconnected."""
        client = self._clients.pop(name, None)
        self._errors.pop(name, None)
        if client is None:
            return False
        # MCP client __aexit__ triggers anyio cancel scope errors when called
        # from a different task than it was created in. Just abandon the
        # connection — the OS cleans up the subprocess/socket on process exit.
        logger.debug("MCP '%s' — dropping connection reference", name)
        return True

    def get_tools(self, name: str) -> list[str]:
        """Return tool names provided by a connected MCP server.

        Returns the full set including individually-disabled tools so
        the panel can show them with a disabled state. Use
        ``get_disabled_tools(name)`` for the per-tool toggle state.
        """
        funcs = self._original_functions.get(name)
        if funcs:
            return list(funcs.keys())
        client = self._clients.get(name)
        if client is None:
            return []
        return list((getattr(client, "functions", None) or {}).keys())

    def get_disabled_tools(self, name: str) -> list[str]:
        """Tools the user has individually disabled on this server."""
        return sorted(self._disabled_tools.get(name, set()))

    def get_tool_descriptions(self, name: str) -> dict[str, str]:
        """Return {tool_name: description} for a connected MCP server."""
        funcs = self._original_functions.get(name)
        if funcs:
            return {
                fname: (getattr(func, "description", "") or "") for fname, func in funcs.items()
            }
        client = self._clients.get(name)
        if client is None:
            return {}
        functions = getattr(client, "functions", None) or {}
        return {
            fname: func.description or ""
            for fname, func in functions.items()
            if hasattr(func, "description")
        }

    async def get_resources(self, name: str) -> list[dict[str, str]]:
        """Resources exposed by a connected MCP server.

        Servers that don't implement the resources capability raise
        "Method not found" — treated as an empty list.
        """
        session = getattr(self._clients.get(name), "session", None)
        if session is None:
            return []
        try:
            result = await session.list_resources()
        except Exception as exc:
            logger.debug("MCP '%s' list_resources failed: %s", name, exc)
            return []
        return [
            {
                "uri": str(r.uri),
                "name": r.name or "",
                "description": r.description or "",
                "mime_type": r.mimeType or "",
            }
            for r in result.resources or []
        ]

    async def get_prompts(self, name: str) -> list[dict[str, Any]]:
        """Prompts exposed by a connected MCP server."""
        session = getattr(self._clients.get(name), "session", None)
        if session is None:
            return []
        try:
            result = await session.list_prompts()
        except Exception as exc:
            logger.debug("MCP '%s' list_prompts failed: %s", name, exc)
            return []
        return [
            {
                "name": p.name,
                "description": p.description or "",
                "arguments": [a.name for a in (p.arguments or [])],
            }
            for p in result.prompts or []
        ]

    def list_servers(self) -> list[str]:
        """List available MCP server names."""
        return list(self.configs.keys())

    def list_connected(self) -> list[str]:
        """List currently connected MCP server names."""
        return list(self._clients.keys())

    def list_required(self) -> list[str]:
        """List servers required by admin policy that are not yet connected."""
        connected = set(self._clients.keys())
        return [s for s in self._policy.required if s not in connected]

    # ── per-tool enable/disable ───────────────────────────────────
    # File-backed disabled-tools list lives in ``self._tool_state``
    # (a :class:`MCPToolStateStore`). This section only owns the
    # in-memory application logic that filters an active MCP
    # client's live ``functions`` dict.

    def _apply_disabled(self, name: str) -> None:
        """Filter the live MCPTools.functions dict to hide disabled
        tools from the agent. Re-applies the full original set first
        so a previously-disabled tool that's been re-enabled comes
        back."""
        client = self._clients.get(name)
        if client is None:
            return
        live = getattr(client, "functions", None)
        if not isinstance(live, dict):
            return
        # Lazily snapshot the original set on first use — covers
        # servers that were connected before this code path existed.
        if name not in self._original_functions:
            self._original_functions[name] = dict(live)
        original = self._original_functions[name]
        disabled = self._disabled_tools.get(name, set())
        live.clear()
        for fname, func in original.items():
            if fname not in disabled:
                live[fname] = func

    def set_tool_enabled(self, server: str, tool: str, enabled: bool) -> None:
        """Toggle a single tool on a server. Persists state and
        re-applies the filter so the change is visible to the next
        agent run."""
        disabled = self._disabled_tools.setdefault(server, set())
        if enabled:
            disabled.discard(tool)
        else:
            disabled.add(tool)
        if not disabled:
            self._disabled_tools.pop(server, None)
        self._tool_state.save(self._disabled_tools)
        self._apply_disabled(server)
