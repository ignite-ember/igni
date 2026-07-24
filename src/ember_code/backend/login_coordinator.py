"""Login task coordinator.

Owns the "one login attempt at a time" invariant that used to live
as an ad-hoc ``login_state: dict[str, Any] = {"task": None}`` bag
threaded through the RPC dispatch closure and the ``CancelLogin``
message handler.

The class exposes a single-writer interface:

* :meth:`start` — cancel any in-flight login and kick off a new one.
* :meth:`cancel` — cancel the in-flight login (no-op if none).

Both are idempotent so races between an RPC and the ``CancelLogin``
message resolve deterministically.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from ember_code.backend.push_bridge import PushNotificationBridge
from ember_code.backend.schemas_rpc import LoginResult, LoginStarted

logger = logging.getLogger(__name__)


class LoginCoordinator:
    """Single-writer manager for the login task.

    Owns the ``asyncio.Task`` handle for the in-flight
    ``backend.login`` call (if any) so the RPC entry point and the
    ``CancelLogin`` message handler can't step on each other.
    """

    def __init__(self, *, backend: Any, push_bridge: PushNotificationBridge) -> None:
        self._backend = backend
        self._push = push_bridge
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> LoginStarted:
        """Cancel any previous login attempt, kick off a new one,
        and ack immediately. The actual login result arrives
        asynchronously via a ``login_result`` push notification."""
        await self.cancel()
        self._task = asyncio.create_task(self._run_login())
        return LoginStarted(started=True)

    async def cancel(self) -> None:
        """Cancel the in-flight login task (if any). Awaits the
        task's completion so downstream state is settled before we
        return."""
        old = self._task
        if old is None or old.done():
            return
        old.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await old

    # ── Internals ────────────────────────────────────────────────

    async def _run_login(self) -> None:
        try:
            res: LoginResult = await self._backend.login(on_status=self._push.on_login_status)
            if res.ok:
                self._backend.reload_cloud_credentials()
            await self._push.on_login_result(success=res.ok, result=res.wire_result_string())
        except asyncio.CancelledError:
            # Cancelled via ``CancelLogin`` — no result to push.
            pass
        except Exception as exc:  # pragma: no cover — defensive
            await self._push.on_login_result(success=False, result=str(exc))
