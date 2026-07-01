"""LSP query tool — exposes plugin-declared language servers to
the agent (Claude Code parity, row 32).

Single low-level method: ``lsp_query(server, method, params)``.
Routes JSON-RPC requests to the named server through the
session's :class:`LspServerManager`, lazily launching the server
on first call. The agent supplies the LSP method name and params
directly — same shape as the LSP spec — and gets the server's
``result`` field back. Errors (server not configured, launch
failed, server-side JSON-RPC error, timeout) come back as
``Error: ...`` strings.

Convenience wrappers (``lsp_definition``, ``lsp_hover``, …) are
intentionally deferred: the model handles the LSP spec well in
practice given its method/params shape, and a low-level
passthrough keeps the surface area small. We'll add wrappers if
real-world usage shows the model tripping on common shapes.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from agno.tools import Toolkit

from ember_code.core.lsp.client import LspClientError

if TYPE_CHECKING:
    from ember_code.core.lsp.manager import LspServerManager

logger = logging.getLogger(__name__)


class LspTools(Toolkit):
    """Single-method toolkit: ``lsp_query``. Constructed with a
    reference to the session's manager so multiple tool calls
    share one set of running servers."""

    def __init__(self, manager: LspServerManager) -> None:
        super().__init__(name="ember_lsp")
        self._manager = manager
        self.register(self.lsp_query)
        self.register(self.lsp_list_servers)

    def lsp_list_servers(self) -> str:
        """Return a JSON list of configured LSP server names plus
        their declared languages. Use this to discover what's
        available before issuing ``lsp_query`` calls."""
        out = []
        for name in self._manager.list_servers():
            config = self._manager._configs[name]
            out.append(
                {
                    "name": name,
                    "languages": list(config.languages),
                    "running": self._manager.is_running(name),
                    "last_error": self._manager.last_error(name),
                }
            )
        if not out:
            return "No LSP servers configured for this session."
        return json.dumps(out, indent=2)

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
        try:
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
            except Exception as exc:  # defensive — unexpected layer
                logger.warning("lsp_query %s/%s raised: %s", server, method, exc)
                return f"Error: {exc}"
            # ``None`` is a valid LSP result for many methods — surface
            # it explicitly so the agent doesn't read "" as "no info".
            if result is None:
                return "null"
            try:
                return json.dumps(result, indent=2, default=str)
            except (TypeError, ValueError):
                return str(result)
        except Exception as exc:  # belt-and-suspenders
            logger.warning("lsp_query outer failure: %s", exc)
            return f"Error: {exc}"
