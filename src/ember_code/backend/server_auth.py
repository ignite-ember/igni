"""Cloud auth RPCs — login, credential reload/clear, plan fetch.

Extracted from :mod:`ember_code.backend.server`. Four free
functions taking ``BackendServer`` as arg — the class holds
one-line delegates:

* :func:`login` — browser-callback OAuth flow (spawns a local
  server, waits for the JWT via redirect, persists it with the
  right TTL from the JWT's ``exp`` claim).
* :func:`reload_cloud_credentials` — refresh ``_cloud`` on the
  session and rebuild ``main_team`` so the next agent turn
  picks up the new token / plan / model set.
* :func:`clear_cloud_credentials` — the logout inverse of the
  reload: point ``_cloud`` at a nonexistent path so all
  properties resolve to None, then rebuild.
* :func:`get_cloud_plan` — hit ``/portal/me`` for the user's
  tier + org name (Pro / Max / Lite / CodeIndex badge).

Rule 2 clean — all imports at module top.
"""

from __future__ import annotations

import asyncio
import contextlib
import webbrowser
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Awaitable, Callable

from ember_code.core.auth.client import (
    DEFAULT_API_URL,
    get_login_url,
    start_callback_server,
    validate_token,
    wait_for_token,
)
from ember_code.core.auth.credentials import (
    CloudCredentials,
    decode_jwt_claims,
    save_credentials,
)
from ember_code.protocol import messages as msg
from pydantic import BaseModel


class CloudPlan(BaseModel):
    """Wire shape for :func:`get_cloud_plan` — tier + org name for
    the org popover badge. Nullable fields because the token
    validation response may omit either."""

    tier: str | None
    org_name: str | None


if TYPE_CHECKING:
    from ember_code.backend.server import BackendServer


StatusCallback = Callable[[str], Awaitable[None] | None] | None


async def login(
    backend: "BackendServer",
    on_status: StatusCallback = None,
) -> tuple[bool, str]:
    """Run the browser-callback login flow.

    Args:
        backend: the server instance (session + settings live on it).
        on_status: optional callback(str) for status updates to FE.

    Returns:
        ``(success, email)`` tuple.
    """

    def _status(text: str) -> None:
        if on_status:
            result = on_status(text)
            # Support both sync and async callbacks.
            if asyncio.iscoroutine(result):
                asyncio.ensure_future(result)

    server = None
    try:
        _status("Starting local server...")
        server, callback_url = start_callback_server()
        port = int(callback_url.split(":")[2].split("/")[0])
        login_url = get_login_url(port)

        with contextlib.suppress(Exception):
            webbrowser.open(login_url)

        _status(
            f"Waiting for login in browser...\nIf the browser didn't open, go to:\n{login_url}"
        )

        try:
            token = await wait_for_token(server, timeout=300)
        except TimeoutError:
            return False, "Login timed out"

        _status("Fetching user info...")
        user_info = await validate_token(token, backend._settings.api_url)
        email = user_info.get("email", "") if user_info else ""

        # Read expiry from JWT for accurate TTL — falls back to the
        # ``save_credentials`` default when the JWT has no ``exp``.
        claims = decode_jwt_claims(token)
        if claims.get("exp"):
            now = datetime.now(timezone.utc)
            exp = datetime.fromtimestamp(claims["exp"], tz=timezone.utc)
            ttl = max(int((exp - now).total_seconds()), 0)
            save_credentials(token, email, ttl=ttl)
        else:
            save_credentials(token, email)

        reload_cloud_credentials(backend)
        return True, email

    except Exception as exc:
        return False, str(exc)
    finally:
        # Always close the callback server to free the port.
        if server is not None:
            with contextlib.suppress(Exception):
                server.server_close()


def reload_cloud_credentials(backend: "BackendServer") -> msg.StatusUpdate:
    """Reload cloud credentials after login."""
    backend._session._cloud = CloudCredentials(backend._settings.auth.credentials_file)
    backend._session.main_team = backend._session._build_main_agent()
    return backend.get_status()


def clear_cloud_credentials(backend: "BackendServer") -> msg.StatusUpdate:
    """Clear cloud credentials on logout."""
    # Point at a path that doesn't exist so all properties resolve
    # to None. That way the very next ``cloud_connected`` /
    # ``access_token`` read reflects the logout without needing
    # to know which fields to zero out one by one.
    backend._session._cloud = CloudCredentials(path="/dev/null")
    backend._session.main_team = backend._session._build_main_agent()
    return backend.get_status()


async def get_cloud_plan(backend: "BackendServer") -> CloudPlan | None:
    """Fetch the current user's plan tier from the cloud.

    Hits ``/portal/me`` with the stored JWT (same endpoint the
    client uses to validate the token on login). The response
    includes the user's tier from their active org membership —
    ``lite`` / ``pro`` / ``max`` / ``codeindex``. FE renders
    this as "Plan: Pro" in the org popover and refreshes on
    every popover open so users see seat/tier changes without
    having to restart the app.

    Returns ``None`` when there are no credentials (logged out)
    or the call fails — FE hides the row in that case.
    """
    token = backend._session._cloud.access_token
    if not token:
        return None
    api_url = getattr(backend._settings.auth, "api_url", DEFAULT_API_URL) or DEFAULT_API_URL
    info = await validate_token(token, api_url=api_url)
    if not info:
        return None
    return CloudPlan(tier=info.get("tier"), org_name=info.get("org_display_name"))
