"""Minimal LSP client — JSON-RPC over stdio with Content-Length
framing. Spec: https://microsoft.github.io/language-server-protocol/

Scope is deliberately small: enough to launch a language server,
complete the ``initialize`` / ``initialized`` handshake, send
arbitrary request / notification methods, and shut down cleanly.
Higher-level features (workspace events, didOpen tracking,
diagnostics streaming) are left for future work — the agent
drives the protocol explicitly via ``lsp_query``.

Threading note: each ``LspClient`` owns a single subprocess and a
single reader task that drains its stdout. Requests/responses are
correlated by integer ``id``; out-of-band notifications from the
server are logged and dropped — we don't surface ``window/log``,
``textDocument/publishDiagnostics``, etc. yet.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from ember_code.core.lsp.config import LspServerConfig

logger = logging.getLogger(__name__)


# Default timeout for an LSP request. Pyright's first ``initialize``
# can take a few seconds on a cold start; subsequent requests are
# fast (sub-100ms typical). 30 s leaves a generous ceiling without
# letting a stalled server pin the agent forever.
_DEFAULT_REQUEST_TIMEOUT = 30.0


class LspClientError(RuntimeError):
    """Raised on protocol-level failures (server crash, malformed
    response, server-side JSON-RPC error). The agent sees the
    string form via the ``lsp_query`` tool."""


class LspClient:
    """One language server's lifecycle + protocol surface."""

    def __init__(self, config: LspServerConfig, project_dir: Path) -> None:
        self.config = config
        self.project_dir = project_dir
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader_task: asyncio.Task | None = None
        self._initialized = False
        self._send_lock = asyncio.Lock()

    # ── Lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        """Spawn the server, kick off the reader, complete the
        initialize handshake. Idempotent — second call is a no-op
        when the server is already up."""
        if self._proc is not None:
            return
        env = {**os.environ, **self.config.env}
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.config.command,
                *self.config.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.project_dir),
                env=env,
            )
        except (OSError, FileNotFoundError) as exc:
            raise LspClientError(f"failed to launch {self.config.command!r}: {exc}") from exc
        self._reader_task = asyncio.create_task(self._reader_loop(), name=f"lsp-{self.config.name}")
        # The LSP initialize request includes the workspace root
        # so the server can scan its corpus before answering
        # other queries. We pass the project_dir as the root URI
        # unless the config pinned an explicit one.
        root_uri = self.config.root_uri or f"file://{self.project_dir.resolve()}"
        params = {
            "processId": os.getpid(),
            "rootUri": root_uri,
            "capabilities": {
                # Minimal — we don't subscribe to workspace events.
                # Server still functions; just won't push diagnostics.
                "workspace": {},
                "textDocument": {},
            },
            "initializationOptions": self.config.initialization_options,
        }
        try:
            await self.request("initialize", params)
        except Exception:
            await self.shutdown()
            raise
        # ``initialized`` notification (note the past tense — it's
        # the FE's signal that handshake is complete and the
        # server may begin asynchronous work).
        await self.notify("initialized", {})
        self._initialized = True

    async def shutdown(self) -> None:
        """Best-effort graceful shutdown. The LSP spec requires
        ``shutdown`` → ``exit`` in that order; we do both with a
        short timeout, then force-terminate the process if it
        doesn't exit on its own."""
        if self._proc is None:
            return
        try:
            if self._initialized:
                # ``shutdown`` is a request, ``exit`` a notification.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.request("shutdown", None), timeout=2.0)
                with contextlib.suppress(Exception):
                    await self.notify("exit", None)
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except TimeoutError:
                self._proc.terminate()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._proc.wait(), timeout=1.0)
                if self._proc.returncode is None:
                    self._proc.kill()
        finally:
            if self._reader_task and not self._reader_task.done():
                self._reader_task.cancel()
                with contextlib.suppress(Exception):
                    await self._reader_task
            self._proc = None
            self._reader_task = None
            self._initialized = False
            # Fail any pending futures so callers don't hang.
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(LspClientError("LSP server shut down"))
            self._pending.clear()

    # ── Protocol ────────────────────────────────────────────────

    async def request(
        self,
        method: str,
        params: Any,
        timeout: float = _DEFAULT_REQUEST_TIMEOUT,
    ) -> Any:
        """Send a JSON-RPC request and await its response.

        Raises ``LspClientError`` on server-side errors or timeout.
        The server's ``result`` is returned verbatim — callers are
        responsible for interpreting the LSP shape.
        """
        req_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future
        try:
            await self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            raise LspClientError(
                f"{self.config.name}: {method} timed out after {timeout}s"
            ) from exc
        finally:
            self._pending.pop(req_id, None)

    async def notify(self, method: str, params: Any) -> None:
        """Send a JSON-RPC notification — no response expected."""
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _send(self, payload: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise LspClientError(f"{self.config.name}: not running")
        body = json.dumps(payload).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode()
        # Hold the lock across the two writes so concurrent
        # requests can't interleave header and body.
        async with self._send_lock:
            self._proc.stdin.write(header + body)
            try:
                await self._proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise LspClientError(f"{self.config.name}: server gone") from exc

    # ── Reader ──────────────────────────────────────────────────

    async def _reader_loop(self) -> None:
        """Drain server stdout, dispatch responses to pending
        futures. Out-of-band notifications and unmatched
        responses are logged at debug and dropped."""
        if self._proc is None or self._proc.stdout is None:
            return
        try:
            while True:
                msg = await self._read_message(self._proc.stdout)
                if msg is None:
                    return
                self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("LSP %s reader crashed: %s", self.config.name, exc)
            # Fail any pending futures so they don't hang.
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(LspClientError(f"reader crashed: {exc}"))
            self._pending.clear()

    async def _read_message(self, stream: asyncio.StreamReader) -> dict | None:
        """Read one ``Content-Length: N\\r\\n\\r\\n<body>`` framed
        message. Returns ``None`` on EOF."""
        content_length: int | None = None
        # Headers — one per line, terminated by an empty line.
        while True:
            line = await stream.readline()
            if not line:
                return None
            line = line.rstrip(b"\r\n")
            if not line:
                break  # end of headers
            if b":" in line:
                key, _, value = line.partition(b":")
                if key.strip().lower() == b"content-length":
                    try:
                        content_length = int(value.strip())
                    except ValueError:
                        return None
        if content_length is None or content_length < 0:
            return None
        body = await stream.readexactly(content_length)
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def _dispatch(self, msg: dict) -> None:
        """Route an incoming message: a response carries ``id``,
        a notification doesn't. Server-side requests (e.g.
        ``window/showMessageRequest``) would also lack our id —
        we ignore them for now since we declared minimal client
        capabilities in ``initialize``."""
        msg_id = msg.get("id")
        if msg_id is None:
            logger.debug("LSP %s notification: %s", self.config.name, msg.get("method"))
            return
        future = self._pending.get(msg_id)
        if future is None:
            logger.debug("LSP %s unmatched response id=%s", self.config.name, msg_id)
            return
        if "error" in msg and msg["error"]:
            err = msg["error"]
            code = err.get("code", "?")
            message = err.get("message", "")
            if not future.done():
                future.set_exception(LspClientError(f"LSP error {code}: {message}"))
        elif not future.done():
            future.set_result(msg.get("result"))
