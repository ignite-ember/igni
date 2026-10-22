"""Each extension's UI is served from its own loopback origin.

Why a listener per extension, rather than one server with a path per
extension at ``/ext/<id>/``:

The browser's unit of isolation is the **origin**, and a port is part
of one. Two extensions sharing ``http://127.0.0.1:9000`` share
``localStorage``, ``IndexedDB``, cookies and the right to reach into
each other's frame through ``window.parent.frames``. A path prefix
separates nothing — it is a naming convention the browser has never
heard of. Giving each extension its own ephemeral port makes
``http://127.0.0.1:54321`` and ``http://127.0.0.1:54322`` as separate
to the browser as two unrelated websites, with no work on our part and
no trust placed in the extension's own code.

The cost is one listener per installed extension. We expect single
digits, and an idle asyncio listener is a socket and a callback.

There is no HTTP server anywhere else in this backend —
``transport/websocket.py`` is a raw ``websockets`` server — so the
first attempt here reused that library's ``process_request`` hook,
which can answer a request as plain HTTP instead of upgrading it. It
does not work for this. ``websockets`` parses only what a handshake
can be: a ``POST`` never reaches the hook at all, it is refused by
closing the connection with no response and a traceback in the log, and
``HEAD`` is refused the same way. A client gets a dropped socket where
it asked a reasonable question, and anything that can reach the port
can fill the log by asking.

So this is a small HTTP/1.1 server of its own. It is small because of
what it refuses to do: ``GET`` and ``HEAD`` only, one request per
connection and then close, no keep-alive, no pipelining, no chunked
anything, no request body, a cap on how much head it will read and a
deadline for reading it. What is left is a request line, some headers,
and a file.

One consequence worth stating. A refused request that is still sending
a large body gets a connection reset rather than the refusal, because
closing a socket with unread data in its receive buffer is what TCP
does. A draining read was written to avoid that and then measured: a
4 KB and a 256 KB body get their 405 with or without it, and a 4 MB one
fails either way. It bought nothing, so it is gone. No browser posts
megabytes to a port that serves two methods, and a client that does is
not owed a tidy answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
from collections.abc import Iterable, Mapping
from pathlib import Path
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)

#: Loopback only, always. These ports serve files from a directory an
#: extension installed; nothing about them is safe to route.
LOOPBACK = "127.0.0.1"

#: The most request head we will read before giving up. A browser's is
#: a few hundred bytes; anything approaching this is not a browser.
MAX_HEAD_BYTES = 16 * 1024

#: How long a connection may take to send that head. Without this, a
#: client that opens a socket and says nothing holds it open for as
#: long as the backend runs.
HEAD_TIMEOUT = 10.0

#: How much of a file to hold in memory at once while sending it.
FILE_CHUNK_BYTES = 64 * 1024


#: What a file's extension is allowed to be served as.
#:
#: An explicit table rather than :mod:`mimetypes` alone, because that
#: module reads the host's ``mime.types`` and so answers differently on
#: different machines — and a file served as the wrong type is either
#: broken or, for ``text/html``, a way to get script into this origin
#: under a name that did not look like script.
CONTENT_TYPES: Mapping[str, str] = {
    ".css": "text/css; charset=utf-8",
    ".gif": "image/gif",
    ".html": "text/html; charset=utf-8",
    ".ico": "image/x-icon",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".wasm": "application/wasm",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}

#: Anything else. Not a guess — a type the browser will not execute,
#: render or sniff, which is the right answer for a file we have no
#: rule for.
UNKNOWN_TYPE = "application/octet-stream"


class HttpReply:
    """One response, ready to put on the wire.

    A class rather than a tuple because every reply carries the same
    header block and the same framing rules, and those belong with the
    thing they describe.
    """

    def __init__(
        self,
        status: int,
        reason: str,
        *,
        body: bytes = b"",
        file: Path | None = None,
        length: int | None = None,
        content_type: str = "text/plain; charset=utf-8",
        extra: Mapping[str, str] | None = None,
    ) -> None:
        """
        Args:
            body: The whole response, for the short ones this module
                writes itself.
            file: A file to stream instead, for the ones it does not.
                The first version of this called ``read_bytes`` and held
                the answer in memory: a bundle with one large asset in
                it then cost that much resident memory per concurrent
                request, chosen by whoever is making the requests.
            length: ``file``'s size, taken once with ``stat`` so the
                header can be written before the file is opened.
        """
        self.status = status
        self.reason = reason
        self.body = body
        self.file = file
        self.length = length if length is not None else len(body)
        self.content_type = content_type
        self.extra = dict(extra or {})

    def encode_head(self) -> bytes:
        """The status line and headers. The body follows separately."""
        lines = [f"HTTP/1.1 {self.status} {self.reason}"]
        for name, value in self.headers().items():
            lines.append(f"{name}: {value}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")

    def headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": self.content_type,
            "Content-Length": str(self.length),
            # One request per connection. Keep-alive would mean framing
            # a second request out of whatever follows, and the parsing
            # this module deliberately does not do.
            "Connection": "close",
            # The CSP is the rule; this stops a browser deciding a file
            # is something else because of what is inside it, which
            # would route around the rule.
            "X-Content-Type-Options": "nosniff",
            # This origin's files are for this origin's page. Other
            # pages do not get to pull them in as images or scripts.
            "Cross-Origin-Resource-Policy": "same-origin",
            # The port is ephemeral: it changes every time the backend
            # restarts, so a cached response is one that belongs to an
            # origin that no longer exists.
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        }
        headers.update(self.extra)
        return headers


class ExtensionOrigin:
    """One extension's UI, on a loopback port of its own.

    Life-cycle: construct, :meth:`start`, read :attr:`url`, and
    :meth:`stop` when the extension is disabled or the backend exits.
    A stopped origin can be started again; it takes a new port.
    """

    def __init__(
        self,
        extension_id: str,
        root: Path,
        *,
        frame_ancestors: Iterable[str] = (),
        host: str = LOOPBACK,
    ) -> None:
        """
        Args:
            extension_id: The manifest id, used only in logs and errors.
            root: The directory to serve. Resolved once here, so a
                symlinked root is allowed — it is the *escape* from the
                root that is refused, and resolving both sides is what
                tells those apart.
            frame_ancestors: Host application origins permitted to embed
                this extension, for the CSP. Empty means nothing may
                embed it, which is where this starts: the host side of
                the bridge is a separate piece of work, and until it
                exists an extension that could be framed by anything
                would be a surface with no one minding it.
            host: Override only in tests that need a second loopback
                address. It is never correct to bind this off-host.
        """
        self._extension_id = extension_id
        self._root = root.resolve()
        self._frame_ancestors = tuple(frame_ancestors)
        self._host = host
        self._server: asyncio.AbstractServer | None = None
        self._port = 0

    @property
    def extension_id(self) -> str:
        return self._extension_id

    @property
    def root(self) -> Path:
        """The directory being served, fully resolved."""
        return self._root

    @property
    def port(self) -> int:
        """The bound port. Zero until :meth:`start` has returned."""
        return self._port

    @property
    def url(self) -> str:
        """The origin, as the frontend should load it."""
        if not self._port:
            raise RuntimeError(
                f"extension origin for '{self._extension_id}' has no port yet — "
                f"await start() before reading url"
            )
        return f"http://{self._host}:{self._port}"

    async def start(self) -> None:
        """Bind an ephemeral port and begin serving. Idempotent."""
        if self._server is not None:
            return
        self._server = await asyncio.start_server(self._handle, self._host, 0, limit=MAX_HEAD_BYTES)
        sockets = self._server.sockets or ()
        if not sockets:  # pragma: no cover - the loop would have raised
            await self.stop()
            raise RuntimeError(f"extension origin for '{self._extension_id}' bound no socket")
        self._port = int(sockets[0].getsockname()[1])
        logger.debug(
            "extension origin %s serving %s at %s",
            self._extension_id,
            self._root,
            self.url,
        )

    async def stop(self) -> None:
        """Close the listener. Safe to call when never started."""
        server, self._server = self._server, None
        self._port = 0
        if server is None:
            return
        server.close()
        try:
            await server.wait_closed()
        except asyncio.CancelledError:  # pragma: no cover - shutdown race
            raise
        except Exception as exc:  # noqa: BLE001 - closing must not raise
            logger.debug("extension origin %s close: %s", self._extension_id, exc)

    # ── One connection ────────────────────────────────────────────────

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Read one request, answer it, close.

        Nothing here raises outward. A listener whose callback raises
        logs a traceback per connection, and anything that can reach
        this port can open connections.
        """
        try:
            reply, headers_only = await self._reply_to(reader)
            writer.write(reply.encode_head())
            if not headers_only:
                await self._write_body(reply, writer)
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass  # the client left; nothing to say and no one to say it to
        except asyncio.CancelledError:  # pragma: no cover - shutdown race
            raise
        except Exception:  # noqa: BLE001 - a connection must not take the server
            logger.exception("extension origin %s failed while answering", self._extension_id)
        finally:
            writer.close()
            with contextlib.suppress(OSError, asyncio.CancelledError):
                await writer.wait_closed()

    async def _write_body(self, reply: HttpReply, writer: asyncio.StreamWriter) -> None:
        """Put the body on the wire, a chunk at a time if it is a file.

        ``drain`` between chunks is what bounds memory: without it the
        whole file lands in the transport's write buffer, which is the
        problem this avoids rather than a different one.
        """
        if reply.file is None:
            writer.write(reply.body)
            return
        sent = 0
        with reply.file.open("rb") as handle:
            while chunk := handle.read(FILE_CHUNK_BYTES):
                writer.write(chunk)
                await writer.drain()
                sent += len(chunk)
        if sent != reply.length:
            # The file changed between the stat and the read. The client
            # is getting a response that does not match its declared
            # length and will notice; say so on this side too.
            logger.warning(
                "extension origin %s sent %d bytes of %s, declared %d",
                self._extension_id,
                sent,
                reply.file,
                reply.length,
            )

    async def _reply_to(self, reader: asyncio.StreamReader) -> tuple[HttpReply, bool]:
        """Parse one request head and decide the answer.

        Returns the reply and whether to send headers only.
        """
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=HEAD_TIMEOUT)
        except (TimeoutError, asyncio.IncompleteReadError):
            return self._error(408, "Request Timeout"), False
        except (asyncio.LimitOverrunError, ValueError):
            # ``readuntil`` raises LimitOverrunError once the separator
            # is further away than the stream limit — a head larger than
            # any browser sends.
            return self._error(431, "Request Header Fields Too Large"), False

        lines = head.decode("latin-1").split("\r\n")
        method, target, bad = self._request_line(lines[0] if lines else "")
        if bad is not None:
            return bad, False
        headers = self._parse_headers(lines[1:])

        if method not in ("GET", "HEAD"):
            return self._error(405, "Method Not Allowed", allow="GET, HEAD"), False

        # From here the method is known, and HEAD means headers only
        # whatever the answer turns out to be. Getting this wrong on the
        # refusals is a protocol violation nobody notices until a client
        # waits for a body that a HEAD may not have.
        headers_only = method == "HEAD"

        # DNS rebinding: a page on an attacker's domain whose name has
        # been pointed at 127.0.0.1 reaches this port with the browser's
        # own credentials and its own Host header. Requiring the Host we
        # actually bound costs one comparison and closes it.
        if not self._host_is_ours(headers.get("host", "")):
            logger.debug(
                "extension origin %s refused Host %r",
                self._extension_id,
                headers.get("host", ""),
            )
            return self._error(421, "Misdirected Request"), headers_only

        target_path = self._resolve(target)
        if target_path is None:
            return self._error(404, "Not Found"), headers_only

        try:
            length = target_path.stat().st_size
        except OSError as exc:
            logger.debug(
                "extension origin %s could not stat %s: %s",
                self._extension_id,
                target_path,
                exc,
            )
            return self._error(404, "Not Found"), headers_only

        reply = HttpReply(
            200,
            "OK",
            file=target_path,
            length=length,
            content_type=self._content_type(target_path),
            extra={"Content-Security-Policy": self._csp()},
        )
        return reply, headers_only

    def _request_line(self, line: str) -> tuple[str, str, HttpReply | None]:
        """``method``, ``target`` — or the reply explaining why not."""
        parts = line.split(" ")
        if len(parts) != 3:
            return "", "", self._error(400, "Bad Request")
        method, target, version = parts
        if not version.startswith("HTTP/1."):
            return "", "", self._error(505, "HTTP Version Not Supported")
        # Only origin-form. Absolute-form is for proxies, and this is
        # not one; authority-form belongs to CONNECT, which is refused
        # by the method check anyway.
        if not target.startswith("/"):
            return "", "", self._error(400, "Bad Request")
        return method, target, None

    @staticmethod
    def _parse_headers(lines: Iterable[str]) -> dict[str, str]:
        """Header names lowercased, last value wins. No folding.

        Enough for the one header this server reads. Obsolete line
        folding is not supported, which matches every HTTP/1.1 client
        written this century.
        """
        headers: dict[str, str] = {}
        for line in lines:
            if not line:
                continue
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        return headers

    def _host_is_ours(self, host: str) -> bool:
        """Is this request addressed to the address we bound?"""
        return host in (f"{self._host}:{self._port}", f"[{self._host}]:{self._port}")

    # ── Which file ────────────────────────────────────────────────────

    def _resolve(self, target: str) -> Path | None:
        """The file this request names, or ``None`` if there isn't one.

        ``None`` covers every refusal a client is told nothing more
        about: outside the root, a directory with no ``index.html``, a
        path that does not exist. They answer alike on purpose — the
        difference between "not here" and "here but not yours" is
        exactly what a prober is asking for.
        """
        # A target starting with ``//`` is parsed by ``urlsplit`` as
        # scheme-relative, so the first segment becomes a hostname and
        # disappears: ``//assets/icon.svg`` would quietly serve
        # ``icon.svg`` from the root. Nothing escapes the root that way,
        # but a request answered with a file it did not name is its own
        # kind of wrong.
        if target.startswith("//"):
            return None
        # The query and fragment belong to the page, not to us.
        path = unquote(urlsplit(target).path)
        if "\x00" in path:
            return None

        candidate = (self._root / path.lstrip("/")).resolve()
        if not self._contains(candidate):
            logger.debug(
                "extension origin %s refused %r: outside %s",
                self._extension_id,
                target,
                self._root,
            )
            return None

        if candidate.is_dir():
            # A directory is served as its index or not at all. There is
            # no listing: the contents of an extension's bundle are not
            # something to publish to anything that can reach the port.
            candidate = (candidate / "index.html").resolve()
            if not self._contains(candidate):
                return None

        return candidate if candidate.is_file() else None

    def _contains(self, candidate: Path) -> bool:
        """Is ``candidate`` inside the served root?

        Both sides are resolved, so this catches ``..`` segments *and*
        a symlink pointing out of the tree — the one a string-prefix
        check misses, and the one an extension would use to read
        ``~/.ssh`` through a file it was allowed to install.
        """
        return candidate == self._root or self._root in candidate.parents

    def _content_type(self, target: Path) -> str:
        suffix = target.suffix.lower()
        if suffix in CONTENT_TYPES:
            return CONTENT_TYPES[suffix]
        # Asked, but not trusted: a type the host's table knows and this
        # module does not is still only served when it cannot execute.
        guessed, _ = mimetypes.guess_type(target.name)
        if guessed and guessed.startswith(("image/", "font/", "audio/", "video/")):
            return guessed
        return UNKNOWN_TYPE

    def _csp(self) -> str:
        """What the extension's page may do.

        Itself, and nothing else. No egress: an extension talks to the
        backend through the bridge, so a page that can open its own
        connections is a page that can leave with whatever it was shown.
        Declared egress, when there is such a thing, is granted by the
        manifest and widens ``connect-src`` here rather than anywhere
        else.
        """
        ancestors = " ".join(self._frame_ancestors) if self._frame_ancestors else "'none'"
        return "; ".join(
            (
                "default-src 'self'",
                "script-src 'self'",
                # Inline styles only. A UI framework writes them, and
                # style cannot exfiltrate without a fetch, which the
                # connect-src below refuses.
                "style-src 'self' 'unsafe-inline'",
                "img-src 'self' data: blob:",
                "font-src 'self'",
                "connect-src 'self'",
                "object-src 'none'",
                "base-uri 'none'",
                "form-action 'none'",
                f"frame-ancestors {ancestors}",
            )
        )

    def _error(self, status: int, reason: str, *, allow: str = "") -> HttpReply:
        extra = {"Content-Security-Policy": self._csp()}
        if allow:
            extra["Allow"] = allow
        return HttpReply(status, reason, body=f"{status} {reason}\n".encode(), extra=extra)


class ExtensionOrigins:
    """Every running origin, one per extension.

    The backend holds one of these. It exists so that starting and
    stopping origins is not something each caller does by hand, and so
    that "which port is extension X on?" has one answer.
    """

    def __init__(self, *, frame_ancestors: Iterable[str] = ()) -> None:
        self._frame_ancestors = tuple(frame_ancestors)
        self._origins: dict[str, ExtensionOrigin] = {}
        # ``serve`` awaits between deciding to start a listener and
        # recording it, so two callers racing on one id both started one
        # and the first was dropped on the floor still listening.
        self._lock = asyncio.Lock()

    def __contains__(self, extension_id: object) -> bool:
        return extension_id in self._origins

    def __len__(self) -> int:
        return len(self._origins)

    async def serve(self, extension_id: str, root: Path) -> ExtensionOrigin:
        """Start an origin for one extension, or return the running one.

        Serving a second root under an id already in use replaces the
        first: an extension is installed once, so two roots for one id
        is a caller bug, and the loud version of it is the one that does
        not leave a stale port answering with the old files.
        """
        async with self._lock:
            existing = self._origins.get(extension_id)
            if existing is not None:
                if existing.root == root.resolve():
                    return existing
                await self._drop_locked(extension_id)

            origin = ExtensionOrigin(extension_id, root, frame_ancestors=self._frame_ancestors)
            await origin.start()
            self._origins[extension_id] = origin
            return origin

    def url_for(self, extension_id: str) -> str | None:
        """Where this extension's UI is, or ``None`` if it has none."""
        origin = self._origins.get(extension_id)
        return origin.url if origin is not None else None

    def urls(self) -> dict[str, str]:
        """Every running origin, for the frontend to load frames from."""
        return {ext_id: origin.url for ext_id, origin in self._origins.items()}

    async def drop(self, extension_id: str) -> None:
        """Stop one extension's origin. Unknown ids are not an error."""
        async with self._lock:
            await self._drop_locked(extension_id)

    async def _drop_locked(self, extension_id: str) -> None:
        """:meth:`drop` for callers already holding the lock."""
        origin = self._origins.pop(extension_id, None)
        if origin is not None:
            await origin.stop()

    async def stop_all(self) -> None:
        """Stop every origin. Called on backend shutdown."""
        async with self._lock:
            for extension_id in list(self._origins):
                await self._drop_locked(extension_id)
