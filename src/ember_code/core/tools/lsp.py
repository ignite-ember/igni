"""LSP tools — expose plugin-declared language servers to the
agent (Claude Code parity, row 32).

Two methods: ``lsp_list_servers`` for discovery, and
``lsp_query(server, method, params)`` for the raw JSON-RPC
passthrough. Both route through the session's
:class:`LspServerManager`, lazily launching a server on first
call. The agent supplies the LSP method name and params directly
— same shape as the LSP spec — and gets the server's ``result``
field back. Errors (server not configured, launch failed,
server-side JSON-RPC error, timeout) come back as ``Error: ...``
strings.

Convenience wrappers (``lsp_definition``, ``lsp_hover``, …) are
intentionally deferred: the model handles the LSP spec well in
practice given its method/params shape, and a low-level
passthrough keeps the surface area small.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from agno.tools import Toolkit

from ember_code.core.lsp.client import LspClientError

if TYPE_CHECKING:
    from ember_code.core.lsp.manager import LspServerManager


class LspTools(Toolkit):
    """Two-method toolkit: ``lsp_list_servers`` and ``lsp_query``.

    Constructed with a reference to the session's manager so
    multiple tool calls share one set of running servers.
    """

    def __init__(self, manager: LspServerManager) -> None:
        super().__init__(name="ember_lsp")
        self._manager = manager
        self.register(self.lsp_query)
        self.register(self.lsp_list_servers)

    def lsp_list_servers(self) -> str:
        """Return a JSON list of configured LSP server names plus
        their declared languages. Use this to discover what's
        available before issuing ``lsp_query`` calls."""
        infos = self._manager.all_server_info()
        if not infos:
            return "No LSP servers configured for this session."
        return json.dumps([info.model_dump() for info in infos], indent=2)

    async def lsp_query(self, server: str, method: str, params: str = "") -> str:
        """Send a JSON-RPC request to ``server`` and return the
        result as a JSON string.

        Args:
            server: Configured LSP server name (see
                ``lsp_list_servers``). Plugin-bundled servers are
                addressed as ``<plugin>:<name>``.
            method: LSP method name — ``textDocument/definition``,
                ``textDocument/references``, ``textDocument/hover``,
                ``workspace/symbol``, etc. See
                https://microsoft.github.io/language-server-protocol/
                for the full method list and shapes.
            params: JSON-encoded ``params`` object. Empty string is
                treated as no params (``null``). Pass the LSP shape
                verbatim — e.g.
                ``{"textDocument": {"uri": "file:///path"},
                   "position": {"line": 10, "character": 4}}``.

        Returns the ``result`` field of the JSON-RPC response as
        a JSON string. ``Error: ...`` on any failure path.
        """
        parsed_params: Any = None
        if params:
            try:
                parsed_params = json.loads(params)
            except json.JSONDecodeError as exc:
                return f"Error: params is not valid JSON — {exc}"
        try:
            result = await self._manager.query(server, method, parsed_params)
        except LspClientError as exc:
            return f"Error: {exc}"
        # ``None`` is a valid LSP result for many methods — surface
        # it explicitly so the agent doesn't read "" as "no info".
        if result is None:
            return "null"
        try:
            return json.dumps(result, indent=2, default=str)
        except (TypeError, ValueError):
            return str(result)
