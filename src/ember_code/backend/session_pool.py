"""SessionPool — route protocol messages to per-session BE runtimes.

One BE process, N live sessions, each owned by its own
``BackendServer`` (its own Agno team, run lock, HITL state). Views
bind to a session by stamping ``session_id`` on their messages; the
pool routes to the matching runtime, lazily resuming sessions that
aren't loaded yet. Runs on different sessions execute in parallel —
nothing is shared between runtimes except the process.

Id aliasing: ``/clear`` renews a runtime's internal session id, but
attached views keep stamping the id they bound to until they learn
the new one. Every id a runtime has EVER carried stays in
``known_ids`` so those in-flight messages still route to the same
runtime instead of spawning a ghost resume of the old id.

The default runtime (the one created at boot) handles messages with
an empty ``session_id`` — which is every message from the TUI, so
pre-pool views work unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SessionRuntime:
    """One live session: its BackendServer + per-runtime wiring."""

    backend: Any
    rpc_table: dict[str, Any]
    queue: list[str]
    transport: Any  # session-stamping transport wrapper
    known_ids: set[str] = field(default_factory=set)

    def remember_id(self) -> None:
        """Record the runtime's CURRENT session id as an alias.

        Called around every dispatch so id renames (``/clear``) keep
        routing stale-stamped messages to this runtime.
        """
        try:
            sid = self.backend.session_id
            if sid:
                self.known_ids.add(sid)
        except Exception:  # pragma: no cover — defensive
            pass


class SessionPool:
    """Find-or-create SessionRuntimes keyed by (current or past) id."""

    def __init__(
        self,
        default: SessionRuntime,
        factory: Callable[[str], Awaitable[SessionRuntime]],
    ) -> None:
        default.remember_id()
        self._runtimes: list[SessionRuntime] = [default]
        self._factory = factory
        # Serialises creation so two messages for the same not-yet-
        # loaded session don't resume it twice.
        self._create_lock = asyncio.Lock()

    @property
    def default(self) -> SessionRuntime:
        return self._runtimes[0]

    @property
    def runtimes(self) -> list[SessionRuntime]:
        return list(self._runtimes)

    def find(self, session_id: str) -> SessionRuntime | None:
        if not session_id:
            return self.default
        for rt in self._runtimes:
            rt.remember_id()
            if session_id in rt.known_ids:
                return rt
        return None

    async def get_or_create(self, session_id: str) -> SessionRuntime:
        rt = self.find(session_id)
        if rt is not None:
            return rt
        async with self._create_lock:
            # Re-check: another message may have created it while we
            # waited on the lock.
            rt = self.find(session_id)
            if rt is not None:
                return rt
            logger.info("session pool: resuming session %s", session_id)
            rt = await self._factory(session_id)
            rt.known_ids.add(session_id)
            rt.remember_id()
            self._runtimes.append(rt)
            return rt

    async def shutdown(self) -> None:
        for rt in self._runtimes:
            try:
                await rt.backend.shutdown()
            except Exception as exc:  # pragma: no cover — defensive
                logger.debug("runtime shutdown failed: %s", exc)


class SessionStampingTransport:
    """Transport wrapper that stamps outbound events with the
    emitting runtime's CURRENT session id (unless already stamped),
    so views can filter the broadcast stream to their bound session."""

    def __init__(self, inner: Any, backend: Any) -> None:
        self._inner = inner
        self._backend = backend

    async def send(self, message: Any) -> None:
        if not message.session_id:
            try:
                sid = self._backend.session_id
            except Exception:  # pragma: no cover — defensive
                sid = ""
            if sid:
                message = message.model_copy(update={"session_id": sid})
        await self._inner.send(message)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
