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

Wire shapes are modeled in :mod:`ember_code.core.lsp.schemas` —
this module builds outgoing payloads by constructing typed
Pydantic models and dumps them onto stdin; inbound bytes are
parsed via :func:`parse_inbound_message` and dispatched by
envelope type rather than key-presence heuristics.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Union

from pydantic import BaseModel

from ember_code.core.lsp.schemas import (
    ClientCapabilities,
    InitializeParams,
    JsonRpcNotification,
    JsonRpcParams,
    JsonRpcRequest,
    JsonRpcResponse,
    JsonRpcServerRequest,
    LspServerConfig,
    parse_inbound_message,
)

logger = logging.getLogger(__name__)


# Default timeout for an LSP request. Pyright's first ``initialize``
# can take a few seconds on a cold start; subsequent requests are
# fast (sub-100ms typical). 30 s leaves a generous ceiling without
# letting a stalled server pin the agent forever.
_DEFAULT_REQUEST_TIMEOUT = 30.0


# What callers may hand us for ``params``. A ``BaseModel`` gets
# dumped via ``model_dump(exclude_none=True)``; a mapping / list /
# ``None`` is passed through verbatim. This is a strict superset
# of what the manager and ``LspTools`` pass today (JSON-decoded
# LLM output — dicts, lists, or ``None``).
# ``Union`` (not ``|``) because ``BaseModel | JsonRpcParams`` mixes
# a metaclass and a ``typing.Union`` alias at module scope, which
# is unsupported on Python <3.12 outside of annotations.
RequestParams = Union[BaseModel, JsonRpcParams]  # noqa: UP007


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
        init_params = InitializeParams(
            processId=os.getpid(),
            rootUri=root_uri,
            # Minimal capabilities — we don't subscribe to
            # workspace events. Server still functions; just
            # won't push diagnostics.
            capabilities=ClientCapabilities(),
            initializationOptions=dict(self.config.initialization_options),
        )
        # BaseException so we clean up the subprocess even on
        # asyncio.CancelledError / validation failures — narrowing
        # to LspClientError would leak a running process.
        try:
            await self.request("initialize", init_params)
            # ``initialized`` notification (note the past tense —
            # it's the FE's signal that handshake is complete and
            # the server may begin asynchronous work).
            await self.notify("initialized", {})
        except BaseException:
            await self.shutdown()
            raise
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
            self._fail_pending("LSP server shut down")

    # ── Protocol ────────────────────────────────────────────────

    async def request(
        self,
        method: str,
        params: RequestParams,
        timeout: float = _DEFAULT_REQUEST_TIMEOUT,
    ) -> Any:
        """Send a JSON-RPC request and await its response.

        Raises ``LspClientError`` on server-side errors or timeout.
        The server's ``result`` is returned verbatim — callers are
        responsible for interpreting the LSP shape.
        """
        req_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        envelope = JsonRpcRequest(
            id=req_id,
            method=method,
            params=self._encode_params(params),
        )
        try:
            await self._send_envelope(envelope)
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            raise LspClientError(
                f"{self.config.name}: {method} timed out after {timeout}s"
            ) from exc
        finally:
            self._pending.pop(req_id, None)

    async def notify(self, method: str, params: RequestParams) -> None:
        """Send a JSON-RPC notification — no response expected."""
        envelope = JsonRpcNotification(
            method=method,
            params=self._encode_params(params),
        )
        await self._send_envelope(envelope)

    @staticmethod
    def _encode_params(params: RequestParams) -> JsonRpcParams:
        """Coerce a caller-supplied ``params`` into a JSON-friendly
        shape. Pydantic models get dumped with camelCase aliases
        (LSP wire shape) and ``exclude_none`` so optional fields
        drop out cleanly."""
        if isinstance(params, BaseModel):
            return params.model_dump(by_alias=True, exclude_none=True)
        return params

    async def _send_envelope(self, envelope: JsonRpcRequest | JsonRpcNotification) -> None:
        """Serialize one envelope model and write it on stdin.

        ``exclude_none=False`` is deliberate: JSON-RPC ``shutdown``
        request and ``exit`` notification both carry
        ``params=null`` and some LSP servers require the key to be
        present. The envelope models use spec-lowercase field names
        so no ``by_alias`` is required here.
        """
        if self._proc is None or self._proc.stdin is None:
            raise LspClientError(f"{self.config.name}: not running")
        payload = envelope.model_dump(exclude_none=False)
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
            self._fail_pending(f"reader crashed: {exc}")

    async def _read_message(
        self, stream: asyncio.StreamReader
    ) -> JsonRpcResponse | JsonRpcNotification | JsonRpcServerRequest | None:
        """Read one ``Content-Length: N\\r\\n\\r\\n<body>`` framed
        message and parse it into an envelope model. Returns
        ``None`` on EOF, malformed framing, or an unparseable
        body — the reader drops the message and continues."""
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
            raw = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return parse_inbound_message(raw)

    def _dispatch(self, msg: JsonRpcResponse | JsonRpcNotification | JsonRpcServerRequest) -> None:
        """Route a parsed inbound message.

        * :class:`JsonRpcResponse` → resolve/fail the pending
          future for its ``id``.
        * :class:`JsonRpcNotification` → log at debug and drop
          (we don't surface ``window/log`` etc. yet).
        * :class:`JsonRpcServerRequest` → log at debug and drop
          (we declared minimal client capabilities in
          ``initialize`` so servers shouldn't send these).
        """
        if isinstance(msg, JsonRpcNotification):
            logger.debug("LSP %s notification: %s", self.config.name, msg.method)
            return
        if isinstance(msg, JsonRpcServerRequest):
            logger.debug(
                "LSP %s server request id=%s method=%s (ignored)",
                self.config.name,
                msg.id,
                msg.method,
            )
            return
        # JsonRpcResponse path.
        future = self._pending.get(msg.id)
        if future is None:
            logger.debug("LSP %s unmatched response id=%s", self.config.name, msg.id)
            return
        if msg.error is not None:
            err = msg.error
            if not future.done():
                future.set_exception(LspClientError(f"LSP error {err.code}: {err.message}"))
            return
        if not future.done():
            future.set_result(msg.result)

    # ── Internals ────────────────────────────────────────────────

    def _fail_pending(self, reason: str) -> None:
        """Fail every pending future with :class:`LspClientError`
        so waiters don't hang after a shutdown or reader crash."""
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(LspClientError(reason))
        self._pending.clear()
