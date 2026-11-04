"""The one way out of the sandbox: a proxy that knows the extension's hosts.

The sandbox (:mod:`.seatbelt`, :mod:`.bubblewrap`) denies the process
every network destination but this machine's loopback. The process
reaches anything further through this proxy, which igni runs, one per
extension, and which connects only to a host on that extension's list:
its own service and its ``permissions.egress`` — narrowed, when
air-gapped, to those inside the network
(:class:`~ember_code.core.network_policy.NetworkPolicy`).

It speaks the three protocols clients find through the environment:

* **HTTP CONNECT** — ``HTTPS_PROXY``; the tunnel carries TLS end to end,
  so the proxy decides on the host and port and never sees the traffic.
* **Plain HTTP** in absolute form (``GET http://host:port/…``) —
  ``HTTP_PROXY``; forwarded one request per connection.
* **SOCKS5**, CONNECT only — told apart by its first byte, on the same
  port. Not put in ``ALL_PROXY`` (httpx would need ``socksio`` to build
  a client), but there for a client that asks.

Each proxy has a token, given to its process inside the proxy URL
(``http://igni:<token>@127.0.0.1:<port>``) and required on every
request — HTTP Basic proxy authentication, or SOCKS5 username and
password — so another extension that finds the port cannot borrow this
one's allow-list.

A host is matched exactly, with the port: an entry ``host`` is
``host:443`` (egress is https/wss), ``host:port`` is that port. A refusal
is a ``403`` (or the SOCKS equivalent) and a warning in igni's log that
names the extension and the host.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import logging
import os
import socket
import struct
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: How a proxied connection is opened: ``(host, port) -> (reader, writer)``.
Connector = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]

#: Default port of an egress entry with none: egress is https/wss.
DEFAULT_PORT = 443
#: The most a request head may be before the proxy gives up on it.
MAX_HEAD = 64 * 1024
#: How long a client has to send its request head.
HEAD_TIMEOUT_S = 30.0
#: How long an upstream connect may take.
CONNECT_TIMEOUT_S = 30.0
#: The user name in a proxy URL; the password is the run's token.
PROXY_USER = "igni"


def canonical_host(host: str) -> str:
    return host.strip().strip("[]").rstrip(".").lower()


class HostAllowList:
    """Exact ``host:port`` pairs a proxy may connect to."""

    def __init__(self, entries: Iterable[str] = ()) -> None:
        self._pairs: set[tuple[str, int]] = set()
        for entry in entries:
            pair = self.parse(entry)
            if pair is not None:
                self._pairs.add(pair)

    @staticmethod
    def parse(entry: str) -> tuple[str, int] | None:
        """``host`` or ``host:port`` (or ``[v6]:port``) as a pair; ``None`` if malformed."""
        text = entry.strip()
        if not text:
            return None
        parsed = urlsplit(f"//{text}")
        try:
            port = parsed.port
        except ValueError:
            return None
        host = parsed.hostname
        if not host:
            return None
        return canonical_host(host), port if port is not None else DEFAULT_PORT

    def add(self, host: str, port: int) -> None:
        self._pairs.add((canonical_host(host), port))

    def permits(self, host: str, port: int) -> bool:
        return (canonical_host(host), port) in self._pairs

    @property
    def entries(self) -> list[str]:
        return sorted(f"{h}:{p}" if ":" not in h else f"[{h}]:{p}" for h, p in self._pairs)

    def __bool__(self) -> bool:
        return bool(self._pairs)


async def _default_connect(
    host: str, port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(host, port)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    finally:
        with contextlib.suppress(Exception):
            if writer.can_write_eof():
                writer.write_eof()


async def splice(
    a: tuple[asyncio.StreamReader, asyncio.StreamWriter],
    b: tuple[asyncio.StreamReader, asyncio.StreamWriter],
) -> None:
    """Copy both ways until both sides are done, then close both."""
    try:
        await asyncio.gather(_pipe(a[0], b[1]), _pipe(b[0], a[1]))
    finally:
        for _, writer in (a, b):
            with contextlib.suppress(Exception):
                writer.close()


class EgressProxy:
    """An HTTP/SOCKS5 proxy that connects only to an allow-list."""

    def __init__(
        self,
        extension_id: str,
        allow: HostAllowList,
        *,
        connect: Connector | None = None,
        token: str | None = None,
    ) -> None:
        """
        Args:
            token: When set, a client must present it — HTTP as
                ``Proxy-Authorization: Basic`` for user ``igni``, SOCKS5
                as that username and password — so one extension cannot
                use another's proxy, and with it another's allow-list.
                The process gets it inside its proxy URL.
        """
        self._id = extension_id
        self._allow = allow
        self._connect = connect or _default_connect
        self._token = token
        self._servers: list[asyncio.base_events.Server] = []
        self._port = 0
        self._tasks: set[asyncio.Task[None]] = set()
        #: Every refused destination, newest last, for tests and the log.
        self.refused: list[str] = []

    @property
    def allow(self) -> HostAllowList:
        return self._allow

    @property
    def port(self) -> int:
        return self._port

    def url(self, port: int | None = None) -> str:
        """The proxy URL a client is given, credentials included."""
        credentials = f"{PROXY_USER}:{self._token}@" if self._token else ""
        return f"http://{credentials}127.0.0.1:{port if port is not None else self._port}"

    def _authorised(self, lines: list[str]) -> bool:
        if self._token is None:
            return True
        expected = base64.b64encode(f"{PROXY_USER}:{self._token}".encode()).decode()
        for line in lines[1:]:
            name, _, value = line.partition(":")
            if name.strip().lower() == "proxy-authorization":
                scheme, _, credentials = value.strip().partition(" ")
                if scheme.lower() == "basic" and hmac.compare_digest(credentials.strip(), expected):
                    return True
        return False

    async def start_tcp(self, host: str = "127.0.0.1") -> int:
        """Listen on a loopback port; returns it."""
        server = await asyncio.start_server(self._accept, host, 0)
        self._servers.append(server)
        self._port = int(server.sockets[0].getsockname()[1])
        return self._port

    async def start_unix(self, path: Path) -> Path:
        """Listen on a unix socket (bridged into a network namespace)."""
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        server = await asyncio.start_unix_server(self._accept, path=str(path))
        os.chmod(path, 0o600)
        self._servers.append(server)
        return path

    async def stop(self) -> None:
        for server in self._servers:
            server.close()
        for task in list(self._tasks):
            task.cancel()
        for server in self._servers:
            with contextlib.suppress(Exception):
                await server.wait_closed()
        self._servers.clear()

    # ── one connection ─────────────────────────────────────────────

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)  # type: ignore[arg-type]
        try:
            first = await asyncio.wait_for(reader.readexactly(1), HEAD_TIMEOUT_S)
            if first == b"\x05":
                await self._socks5(reader, writer)
            else:
                await self._http(first, reader, writer)
        except (asyncio.IncompleteReadError, TimeoutError, ConnectionError, OSError):
            pass
        except asyncio.CancelledError:
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()
            if task is not None:
                self._tasks.discard(task)  # type: ignore[arg-type]

    def _refuse_reason(self, host: str, port: int) -> str | None:
        if self._allow.permits(host, port):
            return None
        target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        self.refused.append(target)
        logger.warning(
            "extension %s tried to reach %s, which is not its service or in its "
            "permissions.egress (allowed: %s); refused by the sandbox proxy",
            self._id,
            target,
            ", ".join(self._allow.entries) or "nothing",
        )
        return (
            f"igni sandbox: {self._id} may not reach {target}. It may reach its own "
            "service and the hosts in its permissions.egress (inside the network, "
            "when igni is air-gapped), and nothing else."
        )

    async def _upstream(
        self, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.wait_for(self._connect(host, port), CONNECT_TIMEOUT_S)

    async def _http(
        self, first: bytes, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        head = first + await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_S)
        if len(head) > MAX_HEAD:
            await self._reply(writer, 431, "Request Header Fields Too Large", "")
            return
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            await self._reply(writer, 400, "Bad Request", "not an HTTP request")
            return
        if not self._authorised(lines):
            await self._reply(
                writer,
                407,
                "Proxy Authentication Required",
                f"igni sandbox: this is {self._id}'s proxy",
                extra='Proxy-Authenticate: Basic realm="igni"\r\n',
            )
            return

        if method.upper() == "CONNECT":
            pair = HostAllowList.parse(target)
            if pair is None or ":" not in target.rsplit("]", 1)[-1]:
                await self._reply(writer, 400, "Bad Request", "CONNECT needs host:port")
                return
            host, port = pair
            reason = self._refuse_reason(host, port)
            if reason is not None:
                await self._reply(writer, 403, "Forbidden", reason)
                return
            try:
                upstream = await self._upstream(host, port)
            except (OSError, TimeoutError) as exc:
                await self._reply(writer, 502, "Bad Gateway", f"could not reach {target}: {exc}")
                return
            writer.write(f"{version} 200 Connection established\r\n\r\n".encode("latin-1"))
            await writer.drain()
            await splice((reader, writer), upstream)
            return

        parts = urlsplit(target)
        if parts.scheme.lower() != "http" or not parts.hostname:
            await self._reply(
                writer,
                400,
                "Bad Request",
                "only absolute http:// requests and CONNECT are proxied",
            )
            return
        host = parts.hostname
        port = parts.port or 80
        reason = self._refuse_reason(host, port)
        if reason is not None:
            await self._reply(writer, 403, "Forbidden", reason)
            return
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        headers = [
            line
            for line in lines[1:]
            if line and not line.lower().startswith(("proxy-", "connection:", "keep-alive:"))
        ]
        rewritten = "\r\n".join(
            [f"{method} {path} {version}", *headers, "Connection: close", "", ""]
        ).encode("latin-1")
        try:
            upstream = await self._upstream(host, port)
        except (OSError, TimeoutError) as exc:
            await self._reply(writer, 502, "Bad Gateway", f"could not reach {host}:{port}: {exc}")
            return
        upstream[1].write(rewritten)
        await upstream[1].drain()
        await splice((reader, writer), upstream)

    @staticmethod
    async def _reply(
        writer: asyncio.StreamWriter, code: int, text: str, body: str, *, extra: str = ""
    ) -> None:
        data = body.encode()
        writer.write(
            (
                f"HTTP/1.1 {code} {text}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                f"{extra}Content-Length: {len(data)}\r\nConnection: close\r\n\r\n"
            ).encode("latin-1")
            + data
        )
        with contextlib.suppress(Exception):
            await writer.drain()

    async def _socks5(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        count = (await reader.readexactly(1))[0]
        methods = await reader.readexactly(count)
        wanted = 0 if self._token is None else 2
        if wanted not in methods:
            writer.write(b"\x05\xff")
            await writer.drain()
            return
        writer.write(bytes([5, wanted]))
        await writer.drain()
        if wanted == 2:
            # RFC 1929: version, username, password.
            await reader.readexactly(1)
            user = await reader.readexactly((await reader.readexactly(1))[0])
            password = await reader.readexactly((await reader.readexactly(1))[0])
            ok = user == PROXY_USER.encode() and hmac.compare_digest(
                password, (self._token or "").encode()
            )
            writer.write(b"\x01\x00" if ok else b"\x01\x01")
            await writer.drain()
            if not ok:
                return
        version, command, _, kind = await reader.readexactly(4)
        if kind == 1:
            host = socket.inet_ntop(socket.AF_INET, await reader.readexactly(4))
        elif kind == 4:
            host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
        elif kind == 3:
            length = (await reader.readexactly(1))[0]
            host = (await reader.readexactly(length)).decode("idna")
        else:
            await self._socks_reply(writer, 8)
            return
        (port,) = struct.unpack("!H", await reader.readexactly(2))
        if version != 5 or command != 1:
            await self._socks_reply(writer, 7)
            return
        if self._refuse_reason(host, port) is not None:
            await self._socks_reply(writer, 2)  # connection not allowed by ruleset
            return
        try:
            upstream = await self._upstream(host, port)
        except (OSError, TimeoutError):
            await self._socks_reply(writer, 5)
            return
        await self._socks_reply(writer, 0)
        await splice((reader, writer), upstream)

    @staticmethod
    async def _socks_reply(writer: asyncio.StreamWriter, code: int) -> None:
        writer.write(bytes([5, code, 0, 1, 0, 0, 0, 0, 0, 0]))
        with contextlib.suppress(Exception):
            await writer.drain()


class LoopbackRelay:
    """A unix socket that relays to one of this machine's loopback ports.

    For a process in its own network namespace (Linux): its loopback is
    not this machine's, so igni's host channel and token endpoint, which
    listen on this machine's ``127.0.0.1``, are relayed in through a unix
    socket on a path bound into the sandbox, and the bridge inside
    (:mod:`.bridge`) listens on the same port number there.
    """

    def __init__(self, port: int, path: Path) -> None:
        self.port = port
        self.path = path
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        self._server = await asyncio.start_unix_server(self._accept, path=str(self.path))
        os.chmod(self.path, 0o600)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            upstream = await asyncio.open_connection("127.0.0.1", self.port)
        except OSError:
            writer.close()
            return
        await splice((reader, writer), upstream)


__all__ = [
    "DEFAULT_PORT",
    "EgressProxy",
    "HostAllowList",
    "LoopbackRelay",
    "canonical_host",
    "splice",
]
