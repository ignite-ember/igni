"""The VM's own credential, and the access token igni keeps from it.

igni server's ``docs/service-identity.md`` §16, ``docs/CLOUD.md`` §9. A
producer's VM runs with nobody present, so it holds a **VM credential**
of its own — the platform never mints a producer a token. Every token
igni needs is an exchange of it, and every exchange rotates it:

* **A person's** first credential comes when they sign in through the
  gateway: the gateway hands igni the person's session
  (``vm_sign_in``), igni exchanges it (``vm-sign-in`` grant) and keeps
  the credential it gets. **An app's** the node agent writes to the
  producer's volume once, when the app's VM is first made.
* The credential lives in ``IGNI_VM_CREDENTIAL_FILE`` (on the volume,
  ``0600``, replaced atomically and fsynced). The access token (15
  minutes) is written to ``IGNI_ACCESS_TOKEN_FILE`` (the tmpfs), which
  every part of igni already reads on every load — so nothing else
  changes when a token is renewed.
* **Renewing** (``vm-credential`` grant): a minute before the token
  expires, after a restore, and on demand. One exchange at a time
  (single flight). The answer's new credential is written — durably —
  **before** the token is: a crash after the exchange and before the
  write leaves the rotated-away credential on the volume, and
  presenting it again is reuse, which revokes the whole family on igni
  server; a person then signs in again, an app is re-issued. The window
  is one round trip and an fsync; the write is the first thing done
  with the answer.
* ``invalid_grant`` — revoked, reused, expired after 90 idle days, the
  person signed out or was removed, the app deleted: a person's VM
  **needs sign-in**, an app's **needs re-issue**. Said to the windows
  and in ``activity.json`` (``credential``); the token is removed, so
  nothing goes on using one the server no longer stands behind.

Until there is a credential igni is bound and works locally — its
database, its sessions — and whatever needs igni server or a service
says "needs sign-in" (``NotSignedIn``) until one arrives, which it does
without a restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

import httpx
from pydantic import BaseModel, ValidationError

from ember_code.core.activity import ActivityRegistry, background
from ember_code.core.activity.http import busy_call
from igni_cloud.guest.files import AtomicFile
from igni_cloud.guest.watch import DirectoryWatch

logger = logging.getLogger(__name__)


class CredentialState(StrEnum):
    #: A credential, and a token from it.
    READY = "ready"
    #: A person's VM before their first sign-in through the gateway, or
    #: after their credential was revoked: they must sign in.
    NEEDS_SIGN_IN = "needs_sign_in"
    #: An app's VM whose credential is gone: an org admin re-issues it.
    NEEDS_REISSUE = "needs_reissue"
    #: igni server could not be reached; the last token holds until it expires.
    UNREACHABLE = "unreachable"


class TokenGrant(BaseModel):
    """igni server's answer to a ``vm-sign-in`` or ``vm-credential`` exchange."""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int = 900
    vm_credential: str


class InvalidGrant(Exception):
    """igni server refused the grant: the credential is no good any more."""


class ExchangeFailed(Exception):
    """igni server did not answer the exchange (network, 5xx): try again later."""


class VmCredentialExchange:
    """``POST {igni}/v1/oauth/token`` with the VM grants."""

    PATH: ClassVar[str] = "/v1/oauth/token"
    SIGN_IN_GRANT: ClassVar[str] = "urn:igni:params:oauth:grant-type:vm-sign-in"
    CREDENTIAL_GRANT: ClassVar[str] = "urn:igni:params:oauth:grant-type:vm-credential"
    TIMEOUT_SECONDS: ClassVar[float] = 15.0

    def __init__(
        self,
        api_url: Callable[[], str | None],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_url = api_url
        self._transport = transport

    async def sign_in(self, session_token: str, organisation: str | None = None) -> TokenGrant:
        form = {"grant_type": self.SIGN_IN_GRANT, "subject_token": session_token}
        if organisation:
            form["organisation"] = organisation
        return await self._exchange(form)

    async def rotate(self, vm_credential: str) -> TokenGrant:
        return await self._exchange(
            {"grant_type": self.CREDENTIAL_GRANT, "vm_credential": vm_credential}
        )

    async def _exchange(self, form: dict[str, str]) -> TokenGrant:
        api_url = self._api_url()
        if not api_url:
            raise ExchangeFailed("no igni server is configured")
        try:
            async with (
                busy_call("igni-server", "VM credential"),
                httpx.AsyncClient(
                    timeout=self.TIMEOUT_SECONDS, transport=self._transport
                ) as client,
            ):
                response = await client.post(f"{api_url.rstrip('/')}{self.PATH}", data=form)
        except httpx.HTTPError as exc:
            raise ExchangeFailed(f"igni server could not be reached: {exc}") from exc
        if response.status_code == 400 and self._error_of(response) == "invalid_grant":
            raise InvalidGrant(self._description_of(response))
        if response.status_code != 200:
            raise ExchangeFailed(f"igni server answered {response.status_code}")
        try:
            return TokenGrant.model_validate_json(response.content)
        except ValidationError as exc:
            raise ExchangeFailed(f"igni server's answer is malformed: {exc}") from exc

    @staticmethod
    def _error_of(response: httpx.Response) -> str:
        try:
            return str(response.json().get("error", ""))
        except ValueError:
            return ""

    @staticmethod
    def _description_of(response: httpx.Response) -> str:
        try:
            return str(response.json().get("error_description", "") or "invalid_grant")
        except ValueError:
            return "invalid_grant"


class CredentialFile:
    """``IGNI_VM_CREDENTIAL_FILE``: the credential, on the producer's volume."""

    MODE: ClassVar[int] = 0o600

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> str | None:
        try:
            value = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None

    def write(self, credential: str) -> None:
        """Replace it so that a crash leaves the old one or the new, and the new survives."""
        AtomicFile(self.path, mode=self.MODE).write(credential.encode())
        # The rename itself must reach the disk before the token is used.
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


class StateChange(BaseModel):
    state: CredentialState
    message: str = ""


class VmCredentials:
    """Keeps an access token from the VM credential, renewing it before it lapses."""

    #: Renewed this long before the token expires.
    RENEW_BEFORE_SECONDS: ClassVar[float] = 60.0
    #: After igni server could not be reached.
    RETRY_SECONDS: ClassVar[float] = 30.0

    def __init__(
        self,
        *,
        credential_file: CredentialFile,
        token_file: Path,
        exchange: VmCredentialExchange,
        producer: str,
        on_state: Callable[[StateChange], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._credential = credential_file
        self._token = AtomicFile(token_file)
        self._exchange = exchange
        #: ``person:<id>`` or ``app:<id>``.
        self._producer = producer
        self._on_state = on_state
        self._clock = clock
        self._lock = asyncio.Lock()
        self._expires_at = 0.0
        self._state = CredentialState.NEEDS_SIGN_IN
        self._message = ""
        self._timer: asyncio.Future[None] | None = None
        self._watcher: asyncio.Future[None] | None = None

    @property
    def state(self) -> CredentialState:
        return self._state

    @property
    def is_app(self) -> bool:
        return self._producer.startswith("app:")

    @property
    def expires_at(self) -> float:
        return self._expires_at

    async def start(self) -> None:
        """Renew from the credential on the volume, if there is one; else wait for a sign-in."""
        ActivityRegistry.shared().on_rearm(self._schedule_soon)
        if self._credential.read() is None:
            self._set(self._lacking(), "no VM credential yet")
            # An app's is written to the volume by the node agent; take it
            # when it appears, without a restart.
            self._watcher = background.daemon(self._wait_for_file(), name="vm-credential-file")
            return
        await self.refresh()

    #: Between looks for the credential file where its directory cannot be watched.
    FILE_POLL_SECONDS: ClassVar[float] = 5.0

    async def _wait_for_file(self) -> None:
        directory = DirectoryWatch(self._credential.path.parent)
        try:
            while self._credential.read() is None:
                if directory.watching or directory.start():
                    await directory.wait(None)
                else:
                    await asyncio.sleep(self.FILE_POLL_SECONDS)
        finally:
            directory.close()
        if self._state is not CredentialState.READY:
            await self.refresh()

    async def stop(self) -> None:
        ActivityRegistry.shared().off_rearm(self._schedule_soon)
        for task in (self._timer, self._watcher):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._timer = self._watcher = None

    async def sign_in(self, session_token: str, organisation: str | None = None) -> StateChange:
        """The gateway handed the person's session: exchange it for the VM's own credential."""
        async with self._lock:
            try:
                grant = await self._exchange.sign_in(session_token, organisation)
            except InvalidGrant as exc:
                self._set(CredentialState.NEEDS_SIGN_IN, f"the sign-in was refused: {exc}")
                return self._change()
            except ExchangeFailed as exc:
                self._set(CredentialState.UNREACHABLE, str(exc))
                return self._change()
            self._take(grant)
        return self._change()

    async def refresh(self, *, force: bool = True) -> StateChange:
        """Exchange the credential for a token. Single flight: a second caller waits for the first."""
        async with self._lock:
            if not force and self._fresh():
                return self._change()
            credential = self._credential.read()
            if credential is None:
                self._set(self._lacking(), "no VM credential")
                return self._change()
            try:
                grant = await self._exchange.rotate(credential)
            except InvalidGrant as exc:
                self._revoked(str(exc))
                return self._change()
            except ExchangeFailed as exc:
                self._set(CredentialState.UNREACHABLE, str(exc))
                self._arm(self.RETRY_SECONDS)
                return self._change()
            self._take(grant)
        return self._change()

    def _take(self, grant: TokenGrant) -> None:
        # The new credential first: the one presented no longer works.
        self._credential.write(grant.vm_credential)
        self._token.write(grant.access_token.encode())
        self._expires_at = self._clock() + grant.expires_in
        self._set(CredentialState.READY, "")
        self._arm(max(1.0, grant.expires_in - self.RENEW_BEFORE_SECONDS))

    def _revoked(self, reason: str) -> None:
        with contextlib.suppress(OSError):
            self._token.remove()
        self._expires_at = 0.0
        if self.is_app:
            logger.error("the app's VM credential was refused (%s): it needs re-issuing", reason)
            self._set(CredentialState.NEEDS_REISSUE, reason)
        else:
            self._set(CredentialState.NEEDS_SIGN_IN, reason)

    def _lacking(self) -> CredentialState:
        return CredentialState.NEEDS_REISSUE if self.is_app else CredentialState.NEEDS_SIGN_IN

    def _fresh(self) -> bool:
        return (
            self._state is CredentialState.READY
            and self._expires_at - self._clock() > self.RENEW_BEFORE_SECONDS
        )

    def _set(self, state: CredentialState, message: str) -> None:
        changed = state is not self._state or message != self._message
        self._state, self._message = state, message
        if changed and self._on_state is not None:
            try:
                self._on_state(self._change())
            except Exception:
                logger.exception("telling of the credential's state failed")

    def _change(self) -> StateChange:
        return StateChange(state=self._state, message=self._message)

    def _arm(self, seconds: float) -> None:
        """Renew in ``seconds`` — re-armed from the wall clock after a restore."""
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
        with contextlib.suppress(RuntimeError):
            self._timer = background.daemon(self._renew_in(seconds), name="vm-credential")

    def _schedule_soon(self) -> None:
        """``ActivityRegistry.rearm``: the monotonic clock may be far behind the wall clock."""
        if self._expires_at:
            seconds = max(0.0, self._expires_at - self._clock() - self.RENEW_BEFORE_SECONDS)
            with contextlib.suppress(RuntimeError):
                asyncio.get_running_loop().call_soon_threadsafe(self._arm, seconds)

    async def _renew_in(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        await self.refresh()


__all__ = [
    "CredentialFile",
    "CredentialState",
    "ExchangeFailed",
    "InvalidGrant",
    "StateChange",
    "TokenGrant",
    "VmCredentialExchange",
    "VmCredentials",
]
