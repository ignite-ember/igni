"""Cloud auth RPCs — login, credential reload/clear, plan fetch.

Exposes a single class, :class:`AuthController`, constructed with
``(session, settings, status_provider)``:

* :meth:`AuthController.login` — browser-callback OAuth flow.
* :meth:`AuthController.reload_cloud_credentials` — refresh
  :class:`CloudCredentials` on the session (post-login) and rebuild
  the main team.
* :meth:`AuthController.clear_cloud_credentials` — logout inverse.
* :meth:`AuthController.get_cloud_plan` — hit ``/portal/me`` for
  the user's tier + org name.

Wire schemas :class:`~ember_code.backend.schemas_rpc.CloudPlan` +
:class:`~ember_code.backend.schemas_rpc.LoginResult` live in
``schemas_rpc.py`` beside the sibling :class:`LoginStarted` — every
backend wire shape belongs in ``schemas_*.py``. They are re-exported
from this module to preserve the ``from ember_code.backend.server_auth
import CloudPlan`` import path for the ``server.py`` TYPE_CHECKING
consumer.
"""

from __future__ import annotations

import asyncio
import contextlib
import webbrowser
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ember_code.backend.schemas_rpc import CloudPlan, LoginResult
from ember_code.core.auth.credentials import (
    CloudCredentials,
    Credentials,
    CredentialsStore,
)
from ember_code.core.auth.portal_client import PortalClient
from ember_code.core.auth.schemas import JwtClaims
from ember_code.protocol import messages as msg

# Re-exported so ``from ember_code.backend.server_auth import CloudPlan``
# keeps working (server.py TYPE_CHECKING import) — the canonical
# definition now lives in ``schemas_rpc``.
__all__ = ["AuthController", "CloudPlan", "LoginResult"]


if TYPE_CHECKING:
    from ember_code.core.config.settings import Settings
    from ember_code.core.session import Session


StatusCallback = Callable[[str], Awaitable[None] | None] | None


class _StatusForwarder:
    """Adapter around the optional login-status callback.

    Wrapping the sync-or-async callback in a named collaborator (one
    per :meth:`AuthController.login` call) flattens the login body —
    the previous nested ``def _status`` closure hid the async-schedule
    quirk (``ensure_future`` for coroutine returns) inside a
    per-line-of-progress lambda. Same semantics, one place to explain
    them.
    """

    def __init__(self, callback: StatusCallback) -> None:
        self._callback = callback

    def emit(self, text: str) -> None:
        """Forward ``text`` to the wrapped callback (sync or async);
        no-op when no callback was supplied."""
        if self._callback is None:
            return
        result = self._callback(text)
        # Coroutine callbacks are scheduled fire-and-forget so the
        # login flow doesn't have to await status echoes.
        if asyncio.iscoroutine(result):
            asyncio.ensure_future(result)


class AuthController:
    """Cloud auth controller for a single session + settings pair.

    Constructed once per :class:`BackendServer` (or per test); holds
    no mutable state — every operation reads/writes the injected
    session/settings collaborators.
    """

    def __init__(
        self,
        session: Session,
        settings: Settings,
        status_provider: Callable[[], msg.StatusUpdate],
    ) -> None:
        self._session = session
        self._settings = settings
        # ``status_provider`` is injected as a callable so
        # ``reload_cloud_credentials`` / ``clear_cloud_credentials``
        # can return the fresh :class:`msg.StatusUpdate` shape
        # without this class needing to know the ``ContextController``
        # exists.
        self._status_provider = status_provider
        # One :class:`PortalClient` per controller — the endpoint
        # URLs are read once from ``settings`` and reused across
        # login / plan-fetch calls instead of threading them through
        # every free-function default.
        self._portal = PortalClient(api_url=self._settings.api_url)
        # One :class:`CredentialsStore` per controller so the login
        # write + the :class:`CloudCredentials` read below share a
        # single path source (previously two independent free-function
        # calls both defaulted to ``~/.ember/credentials.json``).
        self._store = CredentialsStore(self._settings.auth.credentials_file)

    async def login(self, on_status: StatusCallback = None) -> LoginResult:
        """Run the browser-callback login flow.

        Returns a :class:`LoginResult` with named ``ok`` / ``email`` /
        ``error`` fields. Status callbacks are forwarded to
        ``on_status`` so the caller can echo progress into the FE.
        """
        status = _StatusForwarder(on_status)
        try:
            status.emit("Starting local server...")
            async with self._portal.start_callback() as callback:
                login_url = self._portal.login_url(callback.port)

                with contextlib.suppress(Exception):
                    webbrowser.open(login_url)

                status.emit(
                    f"Waiting for login in browser...\nIf the browser didn't open, go to:\n{login_url}"
                )

                token = await callback.wait_for_token(timeout=300)
                if token is None:
                    return LoginResult(ok=False, error="Login timed out")

            status.emit("Fetching user info...")
            validation = await self._portal.validate_token(token)
            email = validation.user.email if validation.ok and validation.user else ""

            # Read expiry from JWT for accurate TTL — falls back to
            # the :meth:`Credentials.new` default when the JWT has no
            # ``exp`` or can't be decoded.
            claims = JwtClaims.decode(token)
            if claims is not None and claims.exp:
                now = datetime.now(timezone.utc)
                exp = datetime.fromtimestamp(claims.exp, tz=timezone.utc)
                ttl = max(int((exp - now).total_seconds()), 0)
                self._store.save(Credentials.new(token, email, ttl=ttl))
            else:
                self._store.save(Credentials.new(token, email))

            self.reload_cloud_credentials()
            return LoginResult(ok=True, email=email)

        except Exception as exc:
            return LoginResult(ok=False, error=str(exc))

    def reload_cloud_credentials(self) -> msg.StatusUpdate:
        """Reload cloud credentials after login."""
        # Share the controller's :class:`CredentialsStore` so the
        # session sees the exact file the login flow just wrote.
        self._session.replace_cloud_credentials(CloudCredentials(store=self._store))
        return self._status_provider()

    def clear_cloud_credentials(self) -> msg.StatusUpdate:
        """Clear cloud credentials on logout."""
        self._session.clear_cloud_credentials()
        return self._status_provider()

    async def get_cloud_plan(self) -> CloudPlan | None:
        """Fetch the current user's plan tier from the cloud.

        Hits ``/portal/me`` with the stored JWT. Returns ``None``
        when logged out or the call fails.
        """
        token = self._session.cloud_access_token
        if not token:
            return None
        result = await self._portal.validate_token(token)
        if not result.ok or result.user is None:
            return None
        return CloudPlan.from_user_info(result.user)
