"""Late producer binding: an igni started before anyone it is for is known.

``docs/CLOUD.md`` §9. A warm base snapshot is a VM booted, igni started
and snapshotted before any producer is known; every producer's VM is a
restore of it. So igni starts with no producer — no database, no token,
nothing producer-specific — and waits for the node agent to write
``/run/igni/producer.env`` (``docs/guest-protocol.md``)::

    IGNI_ACCESS_TOKEN_FILE=/run/igni/token
    IGNI_PRODUCER=person:<id>
    IGNI_ORG=<org>
    IGNI_DATABASE_URL=postgresql://...

The file is written atomically (temporary file, rename), after the token
file and after the producer's volume is mounted, and the protocol has no
"look now" call. On Linux igni watches the run directory with inotify
(:class:`~ember_code.backend.guest.watch.DirectoryWatch`): the rename is
an ``IN_MOVED_TO``, and igni looks at once. It still looks every
:attr:`ProducerBinding.FALLBACK_SECONDS` in case an event is missed;
where inotify is missing it polls every :attr:`ProducerBinding.POLL_SECONDS`
— a ``stat`` of a tmpfs file. Once it names a database, and the token it
points at is a producer's whose org and id are the ones it names, the
variables are put in igni's environment and :meth:`ProducerBinding.bind`
returns: the backend then loads its settings and opens the database, as
it would have with those variables set from the start. Until then every
request a client sends is answered with a refusal (:class:`UnboundRefusal`).

A VM is one producer's for its life. If the file changes after binding to
name another producer or another database, igni does not move its open
sessions across: :meth:`ProducerBinding.watch` reports it, and the
backend shuts down so the image's supervisor starts a fresh igni, which
binds to what the file says now.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ember_code.backend.guest.watch import DirectoryWatch
from ember_code.core.activity import background
from ember_code.core.auth.credentials import CloudCredentials, EnvironmentCredentials
from ember_code.core.config.schemas.storage_and_paths import StorageConfig
from ember_code.core.db.scope import Tenant
from ember_code.extensions.principal import Principal, PrincipalKind
from ember_code.protocol import messages as msg

logger = logging.getLogger(__name__)


class ProducerEnv(BaseModel):
    """``producer.env``: one ``KEY=value`` per line."""

    model_config = ConfigDict(frozen=True)

    ORG_ENV: ClassVar[str] = "IGNI_ORG"
    PRODUCER_ENV: ClassVar[str] = "IGNI_PRODUCER"
    #: Only igni's own variables are taken; a line setting ``PATH`` is ignored.
    PREFIX: ClassVar[str] = "IGNI_"

    values: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def parse(cls, text: str) -> ProducerEnv:
        values: dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key.startswith(cls.PREFIX):
                values[key] = value
        return cls(values=values)

    @property
    def database_url(self) -> str:
        return self.values.get(StorageConfig.DATABASE_URL_ENV, "")

    @property
    def org(self) -> str:
        return self.values.get(self.ORG_ENV, "")

    @property
    def producer(self) -> str:
        return self.values.get(self.PRODUCER_ENV, "")

    def identity(self) -> tuple[str, str, str]:
        """What may not change under a bound igni: database, org and producer."""
        return (self.database_url, self.org, self.producer)

    def apply(self, environ: MutableMapping[str, str]) -> None:
        environ.update(self.values)


class TokenTenant:
    """Whose token the VM holds, read the way the backend will read it."""

    def __call__(self) -> Tenant | None:
        credentials = CloudCredentials()
        user_id = credentials.user_id
        org = credentials.org_id
        if not credentials.is_authenticated or not user_id or not org:
            return None
        return Tenant(org=org, producer=Principal(kind=PrincipalKind.PERSON, id=user_id).ref)


class BindingAttempt(BaseModel):
    """One look at ``producer.env``: bound, or why not yet."""

    env: ProducerEnv | None = None
    tenant: Tenant | None = None
    #: Empty when bound.
    waiting_for: str = ""

    @property
    def bound(self) -> bool:
        return not self.waiting_for


class UnboundRefusal:
    """Answers every client request while no producer is bound."""

    REASON = "igni is not bound to a producer yet; retry shortly"

    def __init__(self, transport: Any) -> None:
        self._transport = transport

    async def run(self) -> None:
        async for message in self._transport.receive():
            try:
                await self._transport.send(self.answer(message))
            except Exception as exc:
                logger.debug("could not refuse %s: %s", message.type, exc)

    @classmethod
    def answer(cls, message: msg.Message) -> msg.Message:
        if isinstance(message, msg.RPCRequest):
            return msg.RPCResponse(id=message.id, session_id=message.session_id, error=cls.REASON)
        return msg.Error(id=message.id, session_id=message.session_id, text=cls.REASON)


class ProducerBinding:
    """Waits for ``producer.env`` and binds igni to the producer it names."""

    #: Between looks when the directory cannot be watched.
    POLL_SECONDS = 0.1
    #: Between looks when it is watched: only a fallback for a missed event.
    FALLBACK_SECONDS = 1.0
    #: An unchanged file is read again this often (the token may have come).
    RELOOK_SECONDS = 1.0

    def __init__(
        self,
        path: Path,
        *,
        environ: MutableMapping[str, str] | None = None,
        tenant: Callable[[], Tenant | None] | None = None,
        poll_seconds: float | None = None,
        watch: bool = True,
    ) -> None:
        self.path = path
        self._environ = os.environ if environ is None else environ
        self._tenant = tenant if tenant is not None else TokenTenant()
        self._poll = self.POLL_SECONDS if poll_seconds is None else poll_seconds
        self._directory = DirectoryWatch(path.parent) if watch else None
        self._seen: tuple[int, int, int] | None = None
        self._said = ""
        self._bound: ProducerEnv | None = None
        self._watch: asyncio.Task[None] | None = None

    @property
    def bound(self) -> ProducerEnv | None:
        return self._bound

    def needed(self) -> bool:
        """Whether to wait: the file is the source when present, and when nothing else names a database."""
        return self.path.exists() or not self._environ.get(StorageConfig.DATABASE_URL_ENV, "")

    def attempt(self) -> BindingAttempt:
        """Read the file once and say whether it binds."""
        try:
            env = ProducerEnv.parse(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return BindingAttempt(waiting_for=f"{self.path} to be written")
        except OSError as exc:
            return BindingAttempt(waiting_for=f"{self.path} to be readable ({exc})")
        if not env.database_url:
            return BindingAttempt(env=env, waiting_for=f"{self.path} to name IGNI_DATABASE_URL")
        # The token is read through the environment the file sets, so
        # apply it to a copy first: nothing changes until it all holds.
        tenant = self._tenant_under(env)
        if tenant is None:
            return BindingAttempt(
                env=env, waiting_for="a producer's token in IGNI_ACCESS_TOKEN_FILE"
            )
        if env.org and env.org != tenant.org:
            return BindingAttempt(
                env=env, waiting_for=f"IGNI_ORG={env.org} to match the token's org {tenant.org}"
            )
        if env.producer and env.producer != tenant.producer:
            return BindingAttempt(
                env=env,
                waiting_for=f"IGNI_PRODUCER={env.producer} to match the token's {tenant.producer}",
            )
        return BindingAttempt(env=env, tenant=tenant)

    def _tenant_under(self, env: ProducerEnv) -> Tenant | None:
        """The token's tenant, read where the file says the token is.

        Credentials are read from the process's environment, so the
        token file's variable is set there for the read and put back.
        """
        key = EnvironmentCredentials.TOKEN_FILE_ENV
        if key not in env.values:
            return self._tenant()
        saved = os.environ.get(key)
        os.environ[key] = env.values[key]
        try:
            return self._tenant()
        finally:
            if saved is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = saved

    def _changed(self) -> bool:
        try:
            stat = self.path.stat()
        except OSError:
            seen = None
        else:
            seen = (stat.st_mtime_ns, stat.st_ino, stat.st_size)
        if seen == self._seen:
            return False
        self._seen = seen
        return True

    def _say(self, attempt: BindingAttempt) -> None:
        if attempt.waiting_for != self._said:
            self._said = attempt.waiting_for
            logger.info("waiting for a producer: %s", attempt.waiting_for)

    async def bind(self, transport: Any = None) -> ProducerEnv:
        """Wait until the file binds, refusing ``transport``'s requests meanwhile."""
        refusal = (
            background.daemon(UnboundRefusal(transport).run(), name="unbound-refusal")
            if transport is not None
            else None
        )
        try:
            # The token can arrive after the file, so a file that has not
            # changed is still looked at again, every second.
            last_look = float("-inf")
            while True:
                now = time.monotonic()
                if self._changed() or now - last_look >= self.RELOOK_SECONDS:
                    last_look = now
                    attempt = self.attempt()
                    if attempt.bound and attempt.env is not None:
                        attempt.env.apply(self._environ)
                        self._bound = attempt.env
                        assert attempt.tenant is not None
                        logger.info(
                            "bound to producer %s of %s",
                            attempt.tenant.producer,
                            attempt.tenant.org,
                        )
                        return attempt.env
                    self._say(attempt)
                await self._next()
        finally:
            if refusal is not None:
                refusal.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await refusal

    @property
    def watching(self) -> bool:
        return self._directory is not None and self._directory.watching

    async def _next(self) -> None:
        """Until the run directory changes — or a poll interval, where it cannot be watched."""
        directory = self._directory
        if directory is not None and (directory.watching or directory.start()):
            await directory.wait(self.FALLBACK_SECONDS)
        else:
            await asyncio.sleep(self._poll)

    def watch(self, on_changed: Callable[[ProducerEnv], None]) -> None:
        """After binding: be told if the file comes to name another producer or database."""
        if self._watch is None:
            self._watch = background.daemon(self._watch_loop(on_changed), name="producer-env-watch")

    async def stop(self) -> None:
        task, self._watch = self._watch, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._directory is not None:
            self._directory.close()

    async def _watch_loop(self, on_changed: Callable[[ProducerEnv], None]) -> None:
        assert self._bound is not None
        while True:
            await self._next()
            if not self._changed():
                continue
            try:
                env = ProducerEnv.parse(self.path.read_text(encoding="utf-8"))
            except OSError:
                # Removed or unreadable: the next write says what it is.
                continue
            if env.identity() == self._bound.identity():
                # Rewritten as it was, or a change igni reads anyway
                # (the token file is read on every load).
                env.apply(self._environ)
                continue
            logger.error(
                "%s now names another producer or database; this igni stays bound to the first",
                self.path,
            )
            on_changed(env)
            return


__all__ = ["BindingAttempt", "ProducerBinding", "ProducerEnv", "TokenTenant", "UnboundRefusal"]
