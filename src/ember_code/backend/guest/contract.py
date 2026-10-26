"""igni's half of the guest contract, as the backend runs it.

``docs/CLOUD.md`` §9 and ``igni__cloudvm``'s ``docs/guest-protocol.md``.
Cloud profile only; the desktop never builds one. In boot order:

1. :meth:`CloudGuest.starting` — a ``ready`` file left by an earlier igni
   in this VM is removed before anything else;
2. :meth:`CloudGuest.listening` — once the transport listens, ``ready``
   is written: the node agent may now take a warm base snapshot;
3. :meth:`CloudGuest.bind` — when no producer is known yet, igni waits
   for ``producer.env``, refusing requests meanwhile
   (:class:`~ember_code.backend.guest.binding.ProducerBinding`); the
   backend loads settings and opens the database only after it returns;
4. :meth:`CloudGuest.serving` — the activity file is kept true, a
   restore is noticed and reconnected after, and a ``producer.env``
   that comes to name another producer shuts igni down;
5. :meth:`CloudGuest.stop` — the files igni keeps are removed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ember_code.backend.activity import Activity
from ember_code.backend.guest.activity_file import ActivityFile
from ember_code.backend.guest.binding import ProducerBinding, ProducerEnv
from ember_code.backend.guest.files import ReadyFile
from ember_code.backend.guest.paths import GuestPaths
from ember_code.backend.guest.resume import ResumeWatch
from ember_code.core.db.engine import cache
from ember_code.core.profile import RuntimeProfile

logger = logging.getLogger(__name__)


class CloudGuest:
    """The ready file, the late binding, the activity file and the resume watch."""

    def __init__(
        self,
        paths: GuestPaths,
        *,
        binding: ProducerBinding | None = None,
        resume: ResumeWatch | None = None,
    ) -> None:
        self.paths = paths
        self.ready = ReadyFile(paths.ready)
        self.binding = binding if binding is not None else ProducerBinding(paths.producer_env)
        self.resume = resume if resume is not None else ResumeWatch()
        self.activity: ActivityFile | None = None
        self._model: Any = None
        self._transport: Any = None

    @classmethod
    def current(cls) -> CloudGuest | None:
        """The contract for this process: only in the cloud profile."""
        if not RuntimeProfile.current().is_cloud:
            return None
        return cls(GuestPaths.from_env())

    def starting(self) -> None:
        try:
            self.ready.clear()
        except OSError as exc:
            logger.warning("cannot clear %s: %s", self.ready.path, exc)

    def listening(self) -> None:
        try:
            self.ready.mark()
        except OSError as exc:
            logger.warning("cannot write %s: %s", self.ready.path, exc)

    async def bind(self, transport: Any) -> ProducerEnv | None:
        """Wait for a producer, unless the environment already names one."""
        if not self.binding.needed():
            return None
        logger.info("no producer yet; waiting for %s", self.binding.path)
        return await self.binding.bind(transport)

    def serving(
        self,
        *,
        model: Any,
        transport: Any,
        notifications: Any = None,
        on_rebind: Callable[[], None] | None = None,
    ) -> None:
        """Start what runs while igni serves the producer."""
        self._model = model
        self._transport = transport
        self.activity = ActivityFile(
            self.paths.activity, activity=self._current_activity, clients=self._clients
        )
        model.broadcasts.on_activity(self.activity.changed)
        self.activity.start()
        self.resume.on_resume(self._reset_pools)
        if notifications is not None:
            self.resume.on_resume(notifications.resumed)
        self.resume.on_resume(self._rewrite_activity)
        self.resume.start()
        if self.binding.bound is not None and on_rebind is not None:
            self.binding.watch(lambda _env: on_rebind())

    async def stop(self) -> None:
        await self.resume.stop()
        await self.binding.stop()
        if self.activity is not None:
            await self.activity.stop()
        try:
            self.ready.clear()
        except OSError as exc:
            logger.debug("cannot clear %s: %s", self.ready.path, exc)

    def _current_activity(self) -> Activity:
        return Activity.model_validate(self._model.backend.activity())

    def _clients(self) -> int:
        inner = getattr(self._transport, "inner", self._transport)
        return int(getattr(inner, "client_count", 0) or 0)

    async def _reset_pools(self, _away: float) -> None:
        await cache.reset_pools()

    async def _rewrite_activity(self, _away: float) -> None:
        if self.activity is not None:
            self.activity.refresh(force=True)


__all__ = ["CloudGuest"]
