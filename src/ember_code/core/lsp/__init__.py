"""Language Server Protocol plumbing — Claude Code plugin parity.

Plugins ship LSP server definitions in ``.lsp.json``; a session-
level :class:`LspServerManager` launches them on demand and routes
JSON-RPC requests through stdio. Agent-facing tools live in
``core.tools.lsp``.
"""

from ember_code.core.lsp.client import LspClient
from ember_code.core.lsp.config import LspServerConfig, load_lsp_config
from ember_code.core.lsp.manager import LspServerManager

__all__ = [
    "LspClient",
    "LspServerConfig",
    "LspServerManager",
    "load_lsp_config",
]
