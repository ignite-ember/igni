"""Local HTTP callback server for the browser-based CLI login flow.

Home for :class:`CallbackServer` — the class that owns the
``HTTPServer``, the free-port lookup, the callback URL string, and
an *instance* ``_token`` slot plus an ``asyncio.Future`` set from
the request thread and awaited on the event loop.

Design notes:

* The old ``client.py`` implementation stored the captured token
  on a class variable (``_CallbackHandler.token``), making the
  handler a singleton — two concurrent logins would trample each
  other, and every ``start_callback_server()`` had to reset the
  classvar as an out-of-band side effect. Here each
  :class:`CallbackServer` binds a fresh handler class via a
  closure factory inside ``__init__``, so the token slot is
  instance-scoped.
* :class:`http.server.HTTPServer` invokes its handler class
  *positionally* — you can't pre-bind kwargs with
  :func:`functools.partial`. We wire ``self`` back to the handler
  by attaching it to the server instance
  (``self._server.callback_server = self``); the handler reads
  ``self.server.callback_server`` on each request.
* The ``asyncio.Future`` used to signal token arrival is created
  *lazily* inside :meth:`wait_for_token` (not in ``__init__``)
  so the constructor stays sync-safe — callers can build a
  :class:`CallbackServer` outside an async context (tests do this).
* :class:`CallbackServer` is single-use — the underlying HTTP
  server accepts one callback, then :meth:`stop` should be called.
  Create a fresh instance per login.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from types import TracebackType
from urllib.parse import parse_qs, urlparse

from ember_code.core.auth._success_page import SUCCESS_PAGE


class _OwnedHTTPServer(HTTPServer):
    """``HTTPServer`` with a typed back-pointer to its :class:`CallbackServer`.

    The handler class is invoked positionally by ``HTTPServer``, so
    the handler needs a way to reach its owner. Subclassing declares
    the ``callback_server`` attribute in the type system rather than
    monkey-patching it onto the base class.
    """

    callback_server: CallbackServer


class CallbackServer:
    """Owns a local HTTP server that captures the CLI auth token.

    Public surface:

    * :attr:`port` — the OS-assigned TCP port the server is bound to.
    * :attr:`callback_url` — the full ``http://localhost:<port>/callback`` URL.
    * :meth:`wait_for_token` — coroutine that resolves with the token
      (or ``None`` on timeout) as soon as the browser redirects.
    * :meth:`stop` — close the underlying HTTP socket.
    * ``async with CallbackServer() as cb: …`` — the context manager
      form calls :meth:`stop` on exit.

    Single-use: build a fresh instance for each login.
    """

    def __init__(self) -> None:
        self._token: str | None = None
        # The Future is created lazily inside ``wait_for_token`` so
        # constructing the server does not require a running event
        # loop — tests build one synchronously.
        self._token_future: asyncio.Future[str | None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

        self.port: int = self._find_free_port()
        self.callback_url: str = f"http://localhost:{self.port}/callback"

        handler_cls = self._build_handler()
        self._server = _OwnedHTTPServer(("127.0.0.1", self.port), handler_cls)
        self._server.timeout = 1.0
        self._server.callback_server = self

        self._thread = Thread(target=self._serve, daemon=True)
        self._thread.start()

    @staticmethod
    def _find_free_port() -> int:
        """Ask the kernel for a free TCP port on ``127.0.0.1``."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        """Return a fresh ``BaseHTTPRequestHandler`` subclass.

        The class itself is defined per-instance so it never grows
        classvar state; the handler reads its captured
        :class:`CallbackServer` via ``self.server.callback_server``
        (attached in ``__init__``).
        """

        class _CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(inner_self) -> None:  # noqa: N805
                parsed = urlparse(inner_self.path)
                params = parse_qs(parsed.query)
                token = params.get("token", [None])[0]

                owner: CallbackServer = inner_self.server.callback_server

                if token:
                    owner._deliver_token(token)
                    inner_self.send_response(200)
                    inner_self.send_header("Content-Type", "text/html")
                    inner_self.end_headers()
                    inner_self.wfile.write(SUCCESS_PAGE.encode())
                else:
                    inner_self.send_response(400)
                    inner_self.send_header("Content-Type", "text/html")
                    inner_self.end_headers()
                    inner_self.wfile.write(b"<html><body><h2>Missing token</h2></body></html>")

            def log_message(inner_self, *args: object) -> None:  # noqa: N805
                """Silence HTTP request logs during OAuth callback.

                Overrides ``BaseHTTPRequestHandler.log_message`` to
                prevent incoming HTTP request logs from cluttering the
                console output while waiting for the OAuth redirect.
                """

        return _CallbackHandler

    def _serve(self) -> None:
        """Handle requests until a token arrives or the server closes."""
        while self._token is None:
            try:
                self._server.handle_request()
            except (ValueError, OSError):
                # Server socket was closed while blocked in select().
                break

    def _deliver_token(self, token: str) -> None:
        """Record ``token`` on this instance and wake the awaiter.

        Called from the HTTP request thread. When a
        :meth:`wait_for_token` coroutine is awaiting, we bounce the
        result back onto its event loop via ``call_soon_threadsafe``.
        """
        self._token = token
        loop = self._loop
        future = self._token_future
        if loop is not None and future is not None and not future.done():
            loop.call_soon_threadsafe(future.set_result, token)

    async def wait_for_token(self, timeout: float = 300.0) -> str | None:
        """Await the callback token, up to ``timeout`` seconds.

        Returns the token string on success, or ``None`` on timeout —
        the coordinating :class:`~ember_code.core.auth.portal_client.PortalClient`
        shapes that into a :class:`~ember_code.core.auth.schemas.LoginResult`.
        The event loop and Future are captured on first entry so the
        constructor stays sync-safe.
        """
        # Fast path: the token may already have arrived between
        # ``__init__`` and this coroutine being awaited.
        if self._token is not None:
            return self._token

        loop = asyncio.get_running_loop()
        if self._token_future is None:
            self._loop = loop
            self._token_future = loop.create_future()

        try:
            return await asyncio.wait_for(self._token_future, timeout)
        except asyncio.TimeoutError:
            return None

    def stop(self) -> None:
        """Close the underlying HTTP socket.

        Safe to call multiple times; the ``_serve`` thread exits
        cleanly the next time it wakes.
        """
        with contextlib.suppress(OSError):
            self._server.server_close()

    async def __aenter__(self) -> CallbackServer:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()
