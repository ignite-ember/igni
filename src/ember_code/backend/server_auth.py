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
import logging
import webbrowser
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
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

logger = logging.getLogger(__name__)

#: How long a new dialogue waits for the server before giving up and
#: using the cached pack. Short on purpose: the point of the on-disk
#: cache is that a session starts without the portal, and blocking
#: startup on a slow network would undo it.
_NEW_DIALOGUE_REVALIDATE_TIMEOUT = 3.0

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
        # calls both defaulted to ``~/.igni/credentials.json``).
        self._store = CredentialsStore(self._settings.auth.credentials_file)
        # Serializes concurrent :meth:`_hydrate_group_policy` calls so a
        # cold-start hook and a fresh login can't race on the same
        # ``pack_meta.json`` write. Lazy because ``asyncio.Lock`` binds
        # to the current event loop; constructed on first async use.
        self._hydration_lock: asyncio.Lock | None = None

        # Hydrate the on-disk group-policy cache from the portal if a
        # stored token already exists. Fire-and-forget — start-up does
        # not block on the fetch; cold-start failures just mean the
        # next CLI invocation will retry (after the 5-min TTL or the
        # next login, whichever comes first). ``asyncio.create_task``
        # needs a running loop; the loop exists in every backend
        # process we ship, but catch the corner case so a bare CLI
        # utility import doesn't blow up.
        try:
            existing_token = self._settings.auth.access_token
        except Exception:
            existing_token = None
        #: The periodic re-check, if one is running.
        self._poll_task: asyncio.Task | None = None

        if existing_token:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                loop.create_task(self._hydrate_group_policy(existing_token, force=True))
                self.start_group_policy_polling()

    def start_group_policy_polling(self) -> None:
        """Re-check the group on a timer, if the deployment wants one.

        A session used to learn its group once, at start: an admin who
        moved somebody at nine reached a session opened at eight only
        when it was restarted. Now it asks again every
        ``settings.group_policy.poll_seconds``, and the ask is cheap —
        the server answers 304 with no body when nothing has changed,
        so the usual cost of a poll is a round trip and a hash
        comparison.

        Zero turns it off, for a deployment that would rather its
        machines only check at startup.
        """
        interval = getattr(self._settings.group_policy, "poll_seconds", 0)
        if interval <= 0 or self._poll_task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._poll_task = loop.create_task(self._poll_group_policy(interval))

    def stop_group_policy_polling(self) -> None:
        """Cancel the timer. Idempotent; safe on a loop that has gone."""
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None

    async def _poll_group_policy(self, interval: int) -> None:
        """Ask again, forever, until cancelled.

        Every failure mode here is a reason to keep going rather than
        stop: a server restart, a laptop asleep, a token that expired
        and will be replaced by the next login. A polling loop that
        dies on the first error is worse than none, because nothing
        says it stopped.
        """
        while True:
            try:
                await asyncio.sleep(interval)
                token = self._settings.auth.access_token
                if token:
                    await self._hydrate_group_policy(token)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the loop outlives its errors
                logger.debug("group-policy poll failed: %s", exc)

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

            # Refresh the on-disk group policy pack with the freshly
            # issued token so admin-side overrides take effect
            # immediately on the next request rather than waiting for
            # the 5-min TTL. ``force`` is what makes that true: without
            # it the stale check returned early and a login inside the
            # window did nothing, which is what this comment used to
            # claim it avoided. Failures are non-fatal — the cold-start
            # hook already fires on every backend start-up.
            await self._hydrate_group_policy(token, force=True)

            self.reload_cloud_credentials()
            return LoginResult(ok=True, email=email)

        except Exception as exc:
            return LoginResult(ok=False, error=str(exc))

    async def _hydrate_group_policy(self, token: str, *, force: bool = False) -> bool:
        """Refresh the cached group policy pack if stale.

        Wraps ``GroupPolicyCache.refresh`` so this controller doesn't
        need to know which on-disk path the cache writes to — that
        decision lives in :mod:`core.config.group_policy`.

        Constructs a fresh :class:`PluginInstaller` so plugin overrides
        with ``source_url`` get git-installed instead of being logged
        and skipped. The installer is built per-call (cheap, no IO at
        construction) rather than cached on ``self`` because hydration
        is rare and the installer's state is per-data-dir.

        Outcomes are surfaced via ``_status_provider`` and the
        ``logger`` so FE / ops see when the portal is unreachable
        instead of silently seeing "no group" for hours.
        """
        from ember_code.core.config.group_policy import refresh as _refresh
        from ember_code.core.plugins.installer import PluginInstaller

        data_dir = Path(self._settings.storage.data_dir).expanduser()
        installer = PluginInstaller(data_dir=data_dir)

        # Serialize concurrent hydrations (cold-start + login race) so
        # two parallel ``refresh`` calls don't trample each other's
        # ``pack_meta.json`` writes. Lazy bind to the running loop.
        if self._hydration_lock is None:
            self._hydration_lock = asyncio.Lock()

        async def _emit(message: str) -> None:
            # Status provider returns a fresh StatusUpdate — harmless
            # to call when the FE isn't listening. Echo through the
            # logger so the audit log picks it up.
            with contextlib.suppress(Exception):
                self._status_provider()
            logger.info("group-policy: %s", message)

        async with self._hydration_lock:
            if not token:
                await _emit("skipped hydration (no bearer token)")
                return False
            try:
                # Cheap stale check first so we don't surface noise on
                # every CLI invocation when the cache is fresh. Skipped
                # when ``force`` is set — a login or a new dialogue
                # should pick up an admin's change now, and the ETag
                # makes an unchanged pack a 304 rather than a download.
                from ember_code.core.config.group_policy import GroupPolicyCache

                cache_dir = data_dir / "group-policy"
                cache = GroupPolicyCache(cache_dir=cache_dir, data_dir=data_dir)
                if not force and not cache._is_stale():
                    return False
                refreshed = await _refresh(
                    token,
                    fetch=self._portal.fetch_group_pack,
                    data_dir=data_dir,
                    installer=installer,
                    force=force,
                )
                if refreshed:
                    await _emit("group policy pack refreshed")
                    # An admin moving somebody between groups should
                    # change what they have, not tell them to restart.
                    with contextlib.suppress(Exception):
                        if self._session.reload_group_agents():
                            await _emit("group agents reloaded")
                return bool(refreshed)
            except Exception as exc:
                # Network blip, schema drift, or PortalClient bug — at
                # minimum log loud; the cached pack (if any) is left
                # untouched so the next invocation can retry.
                logger.warning("group-policy hydration failed: %s", exc)
                with contextlib.suppress(Exception):
                    self._status_provider()
                return False

    async def revalidate_for_new_dialogue(self) -> bool:
        """Ask the server whether the group changed, bounded by a timeout.

        A new dialogue is the moment somebody should pick up an admin's
        change, and it is rare enough to afford a round trip — unlike a
        CLI invocation, which is what the 5-minute age check exists to
        keep quiet. The ETag makes an unchanged pack a 304, so the usual
        cost is one small request.

        **Bounded on purpose.** A session that will not start because
        the portal is slow is a worse failure than one running a
        five-minute-old pack: the on-disk cache exists so sessions work
        offline, and this must not undo that. On timeout the cached pack
        stands and the background poller catches up.
        """
        token = self._settings.auth.access_token
        if not token:
            return False
        try:
            return await asyncio.wait_for(
                self._hydrate_group_policy(token, force=True),
                timeout=_NEW_DIALOGUE_REVALIDATE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.info(
                "group-policy revalidation timed out after %ss; using the cached pack",
                _NEW_DIALOGUE_REVALIDATE_TIMEOUT,
            )
            return False
        except Exception as exc:
            logger.warning("group-policy revalidation failed: %s", exc)
            return False

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
