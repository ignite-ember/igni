"""Who may open the backend's local WebSocket.

The backend runs an agent with shell and file access, and its WebSocket
listens on ``127.0.0.1``. Browsers do not apply CORS to WebSockets, so
without a check here any web page the person opens could connect to
``ws://127.0.0.1:<port>`` and drive that agent, and so could any local
process. Two checks run at the handshake, before the upgrade:

* **Origin.** A browser always sends ``Origin``; a page that is not one
  of igni's own is refused (403). Allowed: the Tauri webview
  (``tauri://localhost``, ``http(s)://tauri.localhost``) and loopback
  pages (``http(s)://127.0.0.1|localhost|[::1]:<any port>``: the web
  client, the Vite dev server). A remote page, a DNS-rebound name, any
  other scheme (``vscode-webview://`` included: igni no longer ships
  the IDE plugins) and ``null`` (sandboxed iframes, ``file://``) are
  refused. A
  request without ``Origin`` is a non-browser client.
* **Token.** When whoever launched the backend gave it a per-launch
  secret (``IGNI_WS_TOKEN``, set by the desktop shell in the child's
  environment, never on its command line), every connection must offer
  it as a WebSocket subprotocol: ``Sec-WebSocket-Protocol: igni.v1,
  igni-token.<token>``. The server answers with ``igni.v1`` only, so the
  token is never echoed. Compared with :func:`hmac.compare_digest`.

The token a connection offered also decides what it may do
(:class:`~ember_code.protocol.schemas.envelope.Standing`):

* the launch secret makes it igni's **window**. Only the launcher holds
  it: it is never written to disk, so the agent — which runs as the same
  OS user and reads whatever its tools can — never has it;
* the **program** token, minted here and published in the owner-only
  lockfile for programs that attach (the SDK's ``Local``), makes it a
  program. A program approves nothing, speaks for no extension and
  loosens no rule (``backend/standing_policy.py``);
* an **approver** token, minted on the window's request
  (``mint_approver_token``) and held only in memory, makes it a program
  that may also answer approval requests. The person hands it to the
  program they chose; a restart forgets every one.

Without a token (a backend started by hand, or in the
cloud, where the gateway authenticates every client and nothing outside
the VM reaches the port) only the Origin check applies, and every
connection is a window.
``X-Igni-Gateway: control`` never exempts a connection from the token: on
a desktop anybody can send that header.

``IGNI_DEV_NO_WS_TOKEN=1`` drops the token requirement for development,
with a loud warning; the desktop shell only honours it in debug builds.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
from collections.abc import MutableMapping, Sequence
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from ember_code.protocol.schemas.envelope import Standing

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
    "Standing",
    "origin_allowed",
    "take_token_from_environment",
]

logger = logging.getLogger(__name__)

#: The environment variable the launcher hands the per-launch secret in.
TOKEN_ENV = "IGNI_WS_TOKEN"  # noqa: S105 — an environment variable's name, not a secret
#: Development escape hatch: no token, with a loud warning.
DEV_NO_TOKEN_ENV = "IGNI_DEV_NO_WS_TOKEN"  # noqa: S105 — an environment variable's name, not a secret
#: The subprotocol igni's clients speak and the server selects.
SUBPROTOCOL = "igni.v1"
#: The offered subprotocol that carries the token: ``igni-token.<token>``.
TOKEN_PROTOCOL_PREFIX = "igni-token."  # noqa: S105 — a subprotocol prefix, not a secret

#: The Tauri webview's origins (macOS/Linux, Windows http and https).
TAURI_ORIGINS = frozenset(
    {"tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"}
)
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
    return parts.scheme in ("http", "https") and host in _LOOPBACK_HOSTS


def _offered_protocols(headers: Any) -> list[str]:
    values = headers.get_all("Sec-WebSocket-Protocol") if hasattr(headers, "get_all") else []
    return [p.strip() for value in values for p in value.split(",") if p.strip()]


def _new_token() -> str:
    """32 random bytes, base64url: valid in a WebSocket subprotocol."""
    return secrets.token_urlsafe(32)


class HandshakeGuard:
    """The handshake checks for one listening socket.

    ``token`` is the per-launch secret, or ``None`` when none was given
    (then only the Origin check applies and every connection is a
    window). With one, the guard mints the program token for the lockfile.
    """

    def __init__(self, token: str | None = None) -> None:
        self._token = token or None
        self._program_token = _new_token() if self._token is not None else None
        self._approver_tokens: list[str] = []

    @property
    def requires_token(self) -> bool:
        return self._token is not None

    @property
    def program_token(self) -> str | None:
        """The token a program attaching through the lockfile offers."""
        return self._program_token

    def mint_approver_token(self) -> str | None:
        """A new token for a program that may answer approval requests.

        ``None`` when this socket takes no token: every connection is a
        window already.
        """
        if self._token is None:
            return None
        token = _new_token()
        self._approver_tokens.append(token)
        return token

    def revoke_approver_tokens(self) -> int:
        """Forget every approver token; how many there were.

        A program already connected with one keeps its connection until
        it drops — its standing was decided at the handshake.
        """
        count = len(self._approver_tokens)
        self._approver_tokens.clear()
        return count

    def standing(self, request: Request) -> Standing | None:
        """What a connection made with ``request`` may do; ``None`` when refused."""
        return self._standing_or_reason(request)[0]

    def refusal(self, request: Request) -> str | None:
        """Why ``request`` is refused, or ``None`` when it may connect.

        The reason never contains the token, offered or expected.
        """
        return self._standing_or_reason(request)[1]

    def _standing_or_reason(self, request: Request) -> tuple[Standing | None, str | None]:
        headers = request.headers
        origin = headers.get("Origin")
        if origin is not None and not origin_allowed(origin):
            return None, f"foreign origin {origin!r}"
        if self._token is None:
            return Standing.WINDOW, None
        offered = [
            p[len(TOKEN_PROTOCOL_PREFIX) :].encode()
            for p in _offered_protocols(headers)
            if p.startswith(TOKEN_PROTOCOL_PREFIX)
        ]
        if not offered:
            return None, "no token"
        known = [(self._token, Standing.WINDOW)]
        known += [(t, Standing.APPROVER) for t in self._approver_tokens]
        if self._program_token is not None:
            known.append((self._program_token, Standing.PROGRAM))
        # Every comparison runs, whichever matches: the time taken says
        # nothing about which token, if any, was offered.
        found: Standing | None = None
        for token, standing in known:
            expected = token.encode()
            if any(hmac.compare_digest(o, expected) for o in offered) and found is None:
                found = standing
        if found is None:
            return None, "wrong token"
        return found, None

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
