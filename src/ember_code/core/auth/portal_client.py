"""Portal endpoint client — login coordination + token validation.

Home for :class:`PortalClient`, the coordinator that owns portal
endpoint state (``portal_url`` / ``api_url`` / ``http_timeout``)
and orchestrates the browser-callback login flow. Replaces the
free-function-with-defaults pattern in the old
``core/auth/client.py`` (``get_login_url`` / ``start_callback_server``
/ ``wait_for_token`` / ``wait_for_callback`` / ``validate_token``),
each of which threaded module-level defaults through five different
signatures.

Public surface:

* :meth:`PortalClient.login_url` — build the CLI-auth URL for a
  given local callback port.
* :meth:`PortalClient.start_callback` — construct and return a
  fresh :class:`~ember_code.core.auth.callback_server.CallbackServer`
  (exposed for callers that need to interleave status callbacks
  between server start and token wait, e.g. the ``AuthController``).
* :meth:`PortalClient.validate_token` — hit ``/v1/portal/me`` and
  return a :class:`~ember_code.core.auth.schemas.ValidateResult`
  carrying either the :class:`~ember_code.core.auth.schemas.UserInfo`
  or a machine-readable failure ``reason`` (no ambiguous ``None``).
* :meth:`PortalClient.run_login` — full one-shot coordinator:
  builds a :class:`CallbackServer`, opens the browser, awaits the
  token, and returns a :class:`~ember_code.core.auth.schemas.LoginResult`.
  Does *not* raise on timeout — callers get
  ``LoginResult(ok=False, reason='timeout')``.
"""

from __future__ import annotations

import contextlib
import logging
import webbrowser

import httpx

from ember_code.core.auth.callback_server import CallbackServer
from ember_code.core.auth.schemas import (
    LoginResult,
    UserInfo,
    ValidateResult,
)
from ember_code.core.config.group_policy import (
    PACK_UNCHANGED,
    GroupPolicyEntry,
    GroupPolicyPack,
    UnchangedPack,
)

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.ignite-ember.sh"
DEFAULT_PORTAL_URL = "https://ignite-ember.sh"


class PortalClient:
    """Coordinator for portal endpoints — login + token validation.

    Constructed once per caller (typically once per
    :class:`~ember_code.backend.server_auth.AuthController`); no
    mutable state beyond the injected endpoint strings.
    """

    def __init__(
        self,
        portal_url: str = DEFAULT_PORTAL_URL,
        api_url: str = DEFAULT_API_URL,
        http_timeout: float = 15.0,
    ) -> None:
        self._portal_url = portal_url
        self._api_url = api_url
        self._http_timeout = http_timeout

    @property
    def portal_url(self) -> str:
        return self._portal_url

    @property
    def api_url(self) -> str:
        return self._api_url

    def login_url(self, port: int) -> str:
        """Build the portal CLI-auth URL for ``port``.

        e.g. ``https://ignite-ember.sh/cli-auth?port=53842``.
        """
        return f"{self._portal_url.rstrip('/')}/cli-auth?port={port}"

    def start_callback(self) -> CallbackServer:
        """Return a fresh :class:`CallbackServer` bound to a free port.

        Exposed so callers can interleave status callbacks between
        server start (fast) and token wait (blocks on the user).
        """
        return CallbackServer()

    async def validate_token(self, token: str) -> ValidateResult:
        """Validate ``token`` against ``/v1/portal/me``.

        Returns a :class:`ValidateResult` whose ``ok`` flag is the
        single source of truth. On failure the ``reason`` field
        distinguishes network errors, non-200 responses, and JSON /
        schema problems so callers can log or branch precisely.
        """
        url = f"{self._api_url.rstrip('/')}/v1/portal/me"
        try:
            async with httpx.AsyncClient(timeout=self._http_timeout) as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        except Exception as exc:
            logger.debug("Token validation network error: %s", exc)
            return ValidateResult(
                ok=False,
                reason="network_error",
                error=str(exc),
            )

        if resp.status_code != 200:
            return ValidateResult(
                ok=False,
                reason="http_error",
                status_code=resp.status_code,
            )

        try:
            payload = resp.json()
        except Exception as exc:
            logger.debug("Token validation decode error: %s", exc)
            return ValidateResult(
                ok=False,
                reason="decode_error",
                status_code=resp.status_code,
                error=str(exc),
            )

        try:
            user = UserInfo.model_validate(payload)
        except Exception as exc:
            logger.debug("Token validation schema mismatch: %s", exc)
            return ValidateResult(
                ok=False,
                reason="schema_mismatch",
                status_code=resp.status_code,
                error=str(exc),
            )

        return ValidateResult(ok=True, user=user, status_code=resp.status_code)

    async def run_login(self, timeout: float = 300.0) -> LoginResult:
        """Run the full browser-callback login flow.

        Opens a :class:`CallbackServer`, launches the user's browser
        at :meth:`login_url`, and awaits the token — up to
        ``timeout`` seconds. Never raises on timeout; returns
        ``LoginResult(ok=False, reason='timeout')`` instead so the
        caller doesn't have to string-match exception messages.
        """
        try:
            async with CallbackServer() as server:
                login_url = self.login_url(server.port)
                with contextlib.suppress(Exception):
                    webbrowser.open(login_url)

                token = await server.wait_for_token(timeout=timeout)
                if token is None:
                    return LoginResult(
                        ok=False,
                        callback_url=server.callback_url,
                        reason="timeout",
                        error="Login timed out — no callback received",
                    )
                return LoginResult(
                    ok=True,
                    token=token,
                    callback_url=server.callback_url,
                )
        except OSError as exc:
            return LoginResult(
                ok=False,
                reason="port_bind_failed",
                error=str(exc),
            )
        except Exception as exc:
            return LoginResult(ok=False, reason="handler_error", error=str(exc))

    async def get_my_group_summary(self, token: str) -> dict | None:
        """Fetch /v1/portal/me/group and return the group summary dict or None."""
        url = f"{self._api_url.rstrip('/')}/v1/portal/me/group"
        try:
            async with httpx.AsyncClient(timeout=self._http_timeout) as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        except Exception as exc:
            logger.debug("get_my_group_summary network error: %s", exc)
            return None

        if resp.status_code == 404 or resp.status_code == 200 and not resp.json():
            return None
        if resp.status_code != 200:
            return None

        try:
            payload = resp.json()
            if payload is None:
                return None
            return payload
        except Exception:
            return None

    async def fetch_group_pack(
        self,
        token: str,
        etag: str | None = None,
    ) -> GroupPolicyPack | None | UnchangedPack:
        """Fetch the group pack, or learn that it has not changed.

        Three answers, and the caller has to tell them apart:

        * a :class:`GroupPolicyPack` — here is the group's configuration
        * ``None`` — no group, or the request failed; keep what you have
        * :data:`PACK_UNCHANGED` — the server says your copy is current

        Passing ``etag`` is what makes the third possible. Sessions poll
        this every few minutes, and the answer is nearly always the
        third; without it every poll would ship a full set of agent
        prompts to say nothing had happened.
        """
        url = f"{self._api_url.rstrip('/')}/v1/portal/me/group/pack"
        headers = {"Authorization": f"Bearer {token}"}
        if etag:
            headers["If-None-Match"] = etag
        try:
            async with httpx.AsyncClient(timeout=self._http_timeout) as client:
                resp = await client.get(url, headers=headers)
        except Exception as exc:
            logger.debug("fetch_group_pack network error: %s", exc)
            return None

        if resp.status_code == 304:
            return PACK_UNCHANGED
        if resp.status_code == 404:
            return None
        if resp.status_code == 204:
            return None
        if resp.status_code != 200:
            logger.debug("fetch_group_pack got status %s", resp.status_code)
            return None

        try:
            payload = resp.json()
        except Exception as exc:
            logger.debug("fetch_group_pack decode error: %s", exc)
            return None

        try:
            entries = [GroupPolicyEntry(**o) for o in payload.get("entries", [])]
            return GroupPolicyPack(
                group_id=payload.get("group_id", ""),
                group_name=payload.get("group_name", ""),
                fetched_at=payload.get("fetched_at"),
                # Dropped silently until now: the group's model reached
                # the CLI as None however it was set on the server.
                default_model=payload.get("default_model"),
                entries=entries,
                etag=resp.headers.get("etag"),
            )
        except Exception as exc:
            logger.debug("fetch_group_pack schema error: %s", exc)
            return None
