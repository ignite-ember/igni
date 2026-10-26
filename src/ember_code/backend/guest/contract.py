"""igni's half of the guest contract, as the backend runs it.

``docs/CLOUD.md`` §9 and ``igni__cloudvm``'s ``docs/guest-protocol.md``.
Cloud profile only; the desktop never builds one. In boot order:

1. :meth:`CloudGuest.starting` — a ``ready`` file left by an earlier igni
   in this VM is removed before anything else;
2. :meth:`CloudGuest.listening` — once the transport listens, what a
   bound backend's first run would import is imported
   (:class:`~ember_code.backend.guest.warm.WarmImports`) and ``ready``
   is written: the node agent may now take a warm base snapshot;
3. :meth:`CloudGuest.bind` — when no producer is known yet, igni waits
   for ``producer.env``, refusing requests meanwhile
   (:class:`~ember_code.backend.guest.binding.ProducerBinding`); the
   backend loads settings and opens the database only after it returns,
   and the process's pseudo-random state is reseeded
   (:class:`~ember_code.backend.guest.entropy.ProcessEntropy`);
4. :meth:`CloudGuest.serving` — the activity file is kept true, a
   restore (``/run/igni/restored``) is reseeded, reconnected and re-timed
   after, what igni built is frozen for the collector, and a ``producer.env``
   that comes to name another producer shuts igni down;
5. :meth:`CloudGuest.stop` — the files igni keeps are removed.
"""

from __future__ import annotations

import gc
import logging
from collections.abc import Callable
from typing import Any

from ember_code.backend.activity import Activity
from ember_code.backend.guest.activity_file import ActivityFile
from ember_code.backend.guest.binding import ProducerBinding, ProducerEnv
from ember_code.backend.guest.credential import (
    CredentialFile,
    CredentialState,
    StateChange,
    VmCredentialExchange,
    VmCredentials,
)
from ember_code.backend.guest.entropy import ProcessEntropy
from ember_code.backend.guest.files import ReadyFile
from ember_code.backend.guest.paths import GuestPaths
from ember_code.backend.guest.resume import Restored, RestoreWatch
from ember_code.backend.guest.warm import WarmImports
from ember_code.core.activity import ActivityRegistry, background
from ember_code.core.db.engine import cache
from ember_code.core.db.scope import Tenant
from ember_code.core.profile import RuntimeProfile
from ember_code.protocol import messages as msg

logger = logging.getLogger(__name__)


class CloudGuest:
    """The ready file, the late binding, the activity file and the restore watch."""

    def __init__(
        self,
        paths: GuestPaths,
        *,
        binding: ProducerBinding | None = None,
        restores: RestoreWatch | None = None,
        warm: WarmImports | None = None,
        gc_freeze: bool = True,
    ) -> None:
        self.paths = paths
        self.ready = ReadyFile(paths.ready)
        self.binding = binding if binding is not None else ProducerBinding(paths.producer_env)
        self.restores = restores if restores is not None else RestoreWatch(paths.restored)
        self.warm = warm if warm is not None else WarmImports()
        self.entropy = ProcessEntropy()
        self._gc_freeze = gc_freeze
        self.activity: ActivityFile | None = None
        self.credentials: VmCredentials | None = None
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
        """Warm the imports a bound backend needs, then say ``ready``."""
        self.warm.run()
        try:
            self.ready.mark()
        except OSError as exc:
            logger.warning("cannot write %s: %s", self.ready.path, exc)

    async def bind(self, transport: Any) -> ProducerEnv | None:
        """Wait for a producer, unless the environment already names one."""
        if not self.binding.needed():
            return None
        logger.info("no producer yet; waiting for %s", self.binding.path)
        env = await self.binding.bind(transport)
        # The first moment this VM is one producer's: it must not share
        # the warm base's pseudo-random state with every other.
        self.entropy.reseed()
        return env

    def serving(
        self,
        *,
        model: Any,
        transport: Any,
        notifications: Any = None,
        on_rebind: Callable[[], None] | None = None,
        api_url: Callable[[], str | None] | None = None,
    ) -> None:
        """Start what runs while igni serves the producer."""
        self._model = model
        self._transport = transport
        self.credentials = self._credentials_for(api_url)
        self.activity = ActivityFile(
            self.paths.activity,
            activity=self._current_activity,
            clients=self._clients,
            credential=self._credential_state,
        )
        model.broadcasts.on_activity(self.activity.changed)
        # A window coming or going changes ``clients``.
        watch_connections = getattr(
            getattr(transport, "inner", transport), "watch_connections", None
        )
        if watch_connections is not None:
            watch_connections(lambda _event, _client: self._rewrite_now())
        self.activity.start()
        self.restores.on_restore(self.entropy.after_restore)
        self.restores.on_restore(self._reset_pools)
        if notifications is not None:
            self.restores.on_restore(notifications.resumed)
        self.restores.on_restore(self._rearm_timers)
        self.restores.on_restore(self._rewrite_activity)
        self.restores.start()
        if self.binding.bound is not None and on_rebind is not None:
            self.binding.watch(lambda _env: on_rebind())
        if self.credentials is not None:
            self.restores.on_restore(self._renew_credential)
            background.start(
                self.credentials.start(), holder="vm-credential", reason="a token from it"
            )
        # Everything igni built to serve this producer stays: frozen, the
        # collector no longer walks (and so writes to) it while igni idles.
        if self._gc_freeze:
            gc.collect()
            gc.freeze()

    async def vm_sign_in(self, session_token: str, organisation: str | None = None) -> StateChange:
        """The gateway handed the person's session (``vm_sign_in``): get the VM its credential."""
        if self.credentials is None:
            return StateChange(state=CredentialState.NEEDS_SIGN_IN, message="not a producer's VM")
        return await self.credentials.sign_in(session_token, organisation)

    @property
    def tenant(self) -> Tenant | None:
        """Whose VM this is, once bound."""
        return self.binding.tenant

    async def stop(self) -> None:
        if self.credentials is not None:
            await self.credentials.stop()
        await self.restores.stop()
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

    def _credentials_for(self, api_url: Callable[[], str | None] | None) -> VmCredentials | None:
        """The credential keeper, for a VM bound to a producer through ``producer.env``."""
        bound = self.binding.bound
        if bound is None or api_url is None:
            return None
        # Read now: ``producer.env`` named the files after igni started.
        paths = GuestPaths.from_env()
        return VmCredentials(
            credential_file=CredentialFile(paths.vm_credential),
            token_file=paths.token,
            exchange=VmCredentialExchange(api_url),
            producer=bound.producer,
            on_state=self._credential_moved,
        )

    def _credential_state(self) -> str | None:
        return self.credentials.state.value if self.credentials is not None else None

    def _credential_moved(self, change: StateChange) -> None:
        """Tell the windows, and the node agent through ``activity.json``."""
        if change.state is not CredentialState.READY:
            logger.warning("VM credential: %s (%s)", change.state.value, change.message)
        if self._transport is not None:
            background.daemon(
                self._transport.send(
                    msg.PushNotification(channel="vm_credential", payload=change.model_dump())
                ),
                name="push-vm-credential",
            )
        self._rewrite_now()

    async def _renew_credential(self, _restored: Restored) -> None:
        if self.credentials is not None:
            await self.credentials.refresh(force=False)

    def _rewrite_now(self) -> None:
        if self.activity is not None:
            self.activity.refresh()

    async def _reset_pools(self, _restored: Restored) -> None:
        await cache.reset_pools()

    async def _rearm_timers(self, _restored: Restored) -> None:
        ActivityRegistry.shared().rearm()

    async def _rewrite_activity(self, _restored: Restored) -> None:
        if self.activity is not None:
            self.activity.refresh(force=True)


__all__ = ["CloudGuest"]
