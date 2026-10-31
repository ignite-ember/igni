"""Who may open the backend's local WebSocket.

The backend runs an agent with shell and file access, and its WebSocket
listens on ``127.0.0.1``. Browsers do not apply CORS to WebSockets, so
without a check here any web page the person opens could connect to
``ws://127.0.0.1:<port>`` and drive that agent, and so could any local
process. Two checks run at the handshake, before the upgrade:

* **Origin.** A browser always sends ``Origin``; a page that is not one
  of igni's own is refused (403). Allowed: the Tauri webview
  (``tauri://localhost``, ``http(s)://tauri.localhost``), VS Code
  webviews (``vscode-webview://…``) and loopback pages
  (``http(s)://127.0.0.1|localhost|[::1]:<any port>``: the JetBrains
  panel's own server, the Vite dev server). A remote page, a DNS-rebound
  name, and ``null`` (sandboxed iframes, ``file://``) are refused. A
  request without ``Origin`` is a non-browser client.
* **Token.** When whoever launched the backend gave it a per-launch
  secret (``IGNI_WS_TOKEN``, set by the desktop shell in the child's
  environment, never on its command line), every connection must offer
  it as a WebSocket subprotocol: ``Sec-WebSocket-Protocol: igni.v1,
  igni-token.<token>``. The server answers with ``igni.v1`` only, so the
  token is never echoed. Compared with :func:`hmac.compare_digest`.

Without a token (a backend started by hand, by an IDE plugin, or in the
cloud, where the gateway authenticates every client and nothing outside
the VM reaches the port) only the Origin check applies.
``X-Igni-Gateway: control`` never exempts a connection from the token: on
a desktop anybody can send that header.

``IGNI_DEV_NO_WS_TOKEN=1`` drops the token requirement for development,
with a loud warning; the desktop shell only honours it in debug builds.
"""

from __future__ import annotations

import hmac
import logging
import os
from collections.abc import MutableMapping, Sequence
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from websockets.asyncio.server import ServerConnection
    from websockets.http11 import Request, Response

__all__ = [
    "DEV_NO_TOKEN_ENV",
    "SUBPROTOCOL",
    "TAURI_ORIGINS",
    "TOKEN_ENV",
    "TOKEN_PROTOCOL_PREFIX",
    "HandshakeGuard",
    "origin_allowed",
    "take_token_from_environment",
]

logger = logging.getLogger(__name__)

#: The environment variable the launcher hands the per-launch secret in.
TOKEN_ENV = "IGNI_WS_TOKEN"
#: Development escape hatch: no token, with a loud warning.
DEV_NO_TOKEN_ENV = "IGNI_DEV_NO_WS_TOKEN"
#: The subprotocol igni's clients speak and the server selects.
SUBPROTOCOL = "igni.v1"
#: The offered subprotocol that carries the token: ``igni-token.<token>``.
TOKEN_PROTOCOL_PREFIX = "igni-token."

#: The Tauri webview's origins (macOS/Linux, Windows http and https).
TAURI_ORIGINS = frozenset(
    {"tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"}
)
_VSCODE_WEBVIEW_SCHEME = "vscode-webview"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def take_token_from_environment(environ: MutableMapping[str, str] | None = None) -> str | None:
    """The launcher's token, removed from the environment.

    Removed so the agent's shell commands, hooks and MCP servers (which
    inherit this process's environment) never see it.
    """
    env = os.environ if environ is None else environ
    token = (env.pop(TOKEN_ENV, None) or "").strip() or None
    if env.get(DEV_NO_TOKEN_ENV, "").strip() == "1":
        logger.warning(
            "%s=1: the WebSocket accepts connections WITHOUT a token. Any local process "
            "can drive this backend. Development only; never set it on a machine you use.",
            DEV_NO_TOKEN_ENV,
        )
        return None
    return token


def origin_allowed(origin: str) -> bool:
    """Whether a browser page from ``origin`` may connect."""
    origin = origin.strip()
    if origin in TAURI_ORIGINS:
        return True
    try:
        parts = urlsplit(origin)
        host = parts.hostname
        # Touch the port so a malformed one ("http://localhost:x") is refused.
        _ = parts.port
    except ValueError:
        return False
    if parts.scheme == _VSCODE_WEBVIEW_SCHEME and host:
        return True
    return parts.scheme in ("http", "https") and host in _LOOPBACK_HOSTS


def _offered_protocols(headers: Any) -> list[str]:
    values = headers.get_all("Sec-WebSocket-Protocol") if hasattr(headers, "get_all") else []
    return [p.strip() for value in values for p in value.split(",") if p.strip()]


class HandshakeGuard:
    """The handshake checks for one listening socket.

    ``token`` is the per-launch secret, or ``None`` when none was given
    (then only the Origin check applies).
    """

    def __init__(self, token: str | None = None) -> None:
        self._token = token or None

    @property
    def requires_token(self) -> bool:
        return self._token is not None

    def refusal(self, request: Request) -> str | None:
        """Why ``request`` is refused, or ``None`` when it may connect.

        The reason never contains the token, offered or expected.
        """
        headers = request.headers
        origin = headers.get("Origin")
        if origin is not None and not origin_allowed(origin):
            return f"foreign origin {origin!r}"
        if self._token is None:
            return None
        offered = [
            p[len(TOKEN_PROTOCOL_PREFIX) :]
            for p in _offered_protocols(headers)
            if p.startswith(TOKEN_PROTOCOL_PREFIX)
        ]
        if not offered:
            return "no token"
        expected = self._token.encode()
        if not any(hmac.compare_digest(o.encode(), expected) for o in offered):
            return "wrong token"
        return None

    def process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        """``websockets`` hook: refuse with 403 before the upgrade."""
        reason = self.refusal(request)
        if reason is None:
            return None
        remote = getattr(connection, "remote_address", None)
        logger.warning("refused a WebSocket connection from %s: %s", remote, reason)
        return connection.respond(HTTPStatus.FORBIDDEN, "Forbidden\n")

    @staticmethod
    def select_subprotocol(connection: ServerConnection, subprotocols: Sequence[str]) -> str | None:
        """Answer ``igni.v1`` when offered, never the token, else nothing."""
        return SUBPROTOCOL if SUBPROTOCOL in subprotocols else None
