"""LspServerManager — per-session lifecycle of LSP clients.

Pattern mirrors :class:`MCPManager`: holds configs at construction
time, launches each server lazily on first query, shuts them all
down on session close. Idempotent everywhere — concurrent first-
queries on the same server share one launch via a per-server lock.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ember_code.core.lsp.client import LspClient, LspClientError
from ember_code.core.lsp.schemas import LspServerConfig, LspServerInfo

logger = logging.getLogger(__name__)


class LspServerManager:
    """Per-session registry + lifecycle for LSP servers.

    Construct once at session init with the merged config dict.
    The first ``query`` for a given server triggers
    ``LspClient.start``; subsequent queries reuse the running
    server. Concurrent first-queries are serialized via a per-
    server ``asyncio.Lock`` so we never spawn the same server
    twice."""

    def __init__(
        self,
        servers: dict[str, LspServerConfig],
        project_dir: Path,
    ) -> None:
        self._configs = dict(servers)
        self._project_dir = project_dir
        self._clients: dict[str, LspClient] = {}
        self._launch_locks: dict[str, asyncio.Lock] = {}
        self._launch_errors: dict[str, str] = {}

    def list_servers(self) -> list[str]:
        """Configured server names (regardless of running state)."""
        return sorted(self._configs.keys())

    def is_running(self, name: str) -> bool:
        return name in self._clients

    def last_error(self, name: str) -> str:
        """Last launch error for ``name``, or empty string. Helpful
        for the panel to show "this server failed to start because
        X" without forcing the user to retry."""
        return self._launch_errors.get(name, "")

    def server_info(self, name: str) -> LspServerInfo:
        """Public snapshot of a single configured server. Raises
        ``LspClientError`` for unknown names — matches
        :meth:`ensure`'s failure vocabulary so callers get a
        single error type for "no such server"."""
        if name not in self._configs:
            raise LspClientError(f"LSP server not configured: {name!r}")
        config = self._configs[name]
        return LspServerInfo(
            name=name,
            languages=list(config.languages),
            running=self.is_running(name),
            last_error=self.last_error(name),
        )

    def all_server_info(self) -> list[LspServerInfo]:
        """Public snapshots of every configured server, sorted by
        name. Used by :class:`LspTools.lsp_list_servers` and any
        future panel/UI reader — the manager owns projection of
        its own private state onto a Pydantic wire shape."""
        return [self.server_info(name) for name in self.list_servers()]

    async def ensure(self, name: str) -> LspClient:
        """Lazy-launch (if needed) and return the client. Raises
        ``LspClientError`` if the server isn't configured or fails
        to start. Concurrent callers asking for the same server
        wait on a single launch attempt."""
        if name not in self._configs:
            raise LspClientError(f"LSP server not configured: {name!r}")
        if name in self._clients:
            return self._clients[name]
        lock = self._launch_locks.setdefault(name, asyncio.Lock())
        async with lock:
            # Double-check inside the lock — another waiter may
            # have completed the launch while we waited.
            if name in self._clients:
                return self._clients[name]
            client = LspClient(self._configs[name], project_dir=self._project_dir)
            try:
                await client.start()
            except LspClientError as exc:
                self._launch_errors[name] = str(exc)
                raise
            self._launch_errors.pop(name, None)
            self._clients[name] = client
            return client

    async def query(self, name: str, method: str, params: Any) -> Any:
        """Send a single LSP request and return the result.

        Convenience wrapper — equivalent to
        ``(await manager.ensure(name)).request(method, params)``,
        plus error normalisation so callers get
        ``LspClientError`` for every failure path including
        "server not configured" and "server failed to start"."""
        client = await self.ensure(name)
        return await client.request(method, params)

    async def shutdown_all(self) -> None:
        """Gracefully shut down every running client. Safe to call
        multiple times — already-stopped clients are no-ops."""
        clients, self._clients = self._clients, {}
        for name, client in clients.items():
            try:
                await client.shutdown()
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("LSP %s shutdown raised: %s", name, exc)
