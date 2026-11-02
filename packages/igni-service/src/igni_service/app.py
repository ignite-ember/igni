"""`IgniApp`: an app of one organisation, acting as itself.

An **app** — a review bot, an integration — is not a service. A service
is the deployment's, approved by a super admin, and serves every
organisation; an app belongs to one organisation, whose org admin
registered it on the portal (*Apps*) by pasting its **public** key. The app
then obtains tokens as itself: `client_credentials` with `private_key_jwt`,
`client_id` = its app id, no organisation (igni server's
`docs/service-identity.md` §11.1). Its VM in igni's cloud is its own.

    app = IgniApp("3f0c…", igni_server_url="https://igni.bank.internal", key_store=store)
    print(await app.public_jwks())       # once: what the org admin pastes
    record = await app.request_vm_profile(VmProfileRequest(packages=VmPackages(apt=["chromium"])))
    record.state                         # "pending" until an org admin approves it

The key is made on first use and kept in `key_store`, exactly as a
service's is (`igni_service.keystore`); the private half never leaves the
app. Nothing here can approve a request: that is an org admin's, on the
portal.
"""

from __future__ import annotations

import asyncio
from types import TracebackType
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from igni_service.clock import Clock
from igni_service.errors import ConfigurationError, IgniUnavailable
from igni_service.keystore import KeyStore
from igni_service.server import IgniServer
from igni_service.service_tokens import HeldToken
from igni_service.signing import SigningKey
from igni_service.token_endpoint import GrantRefused, TokenEndpoint
from igni_service.urls import IgniPaths
from igni_service.vm_profiles import (
    AppVmProfile,
    VmProfileRequest,
    VmProfileRequestRecord,
    refusal,
)


class AppId:
    """The client id an app authenticates as: its app id, from the portal."""

    def __init__(self, app_id: str) -> None:
        self._app_id = app_id

    async def client_id(self) -> str:
        return self._app_id


class IgniApp:
    """An app's place in an igni deployment: its key, its token, its VM's profile."""

    #: How long before expiry the token is replaced, in seconds.
    RENEW_BEFORE = 60.0

    def __init__(
        self,
        app_id: str,
        *,
        igni_server_url: str,
        key_store: KeyStore,
        http: httpx.AsyncClient | None = None,
        clock: Clock | None = None,
        timeout: float = 10.0,
    ) -> None:
        if not app_id.strip():
            raise ConfigurationError("igni: an app needs its app id, as the portal shows it")
        if not igni_server_url.strip():
            raise ConfigurationError("igni: IGNI_SERVER_URL must be set")
        self.app_id = app_id
        self.clock = clock or Clock()
        self.server = IgniServer(base_url=igni_server_url, timeout=timeout, http=http)
        self.key = SigningKey(key_store, clock=self.clock)
        self._endpoint = TokenEndpoint(self.server, AppId(app_id), self.key)
        self._held: HeldToken | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> IgniApp:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self.server.aclose()

    # ── The key an org admin registers ──────────────────────────────

    async def public_jwks(self) -> dict[str, Any]:
        """`{"keys": [public JWK]}` — what the org admin pastes on the portal's Apps page."""
        return {"keys": [(await self.key.pair()).public_jwk().as_dict()]}

    async def thumbprint(self) -> str:
        """The key's RFC 7638 thumbprint, which the portal shows beside the app."""
        return await self.key.thumbprint()

    # ── Its own token ───────────────────────────────────────────────

    async def token(self) -> str:
        """The app's own token for igni (`token_use` = `app`). Raises `IgniUnavailable`."""
        held = self._fresh()
        if held is not None:
            return held
        async with self._lock:
            held = self._fresh()
            if held is not None:
                return held
            try:
                issued = await self._endpoint.grant({"grant_type": "client_credentials"})
            except GrantRefused as exc:
                # `invalid_client` until an org admin registers this key, and
                # after they delete the app or rotate its key.
                raise IgniUnavailable(f"igni server refused this app a token: {exc}") from exc
            self._held = HeldToken(
                token=issued.access_token,
                expires_at=self.clock.monotonic() + issued.expires_in,
            )
            return issued.access_token

    def _fresh(self) -> str | None:
        held = self._held
        if held is not None and self.clock.monotonic() < held.expires_at - self.RENEW_BEFORE:
            return held.token
        return None

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """One call to igni as this app; a 401 is retried once with a new token."""
        for attempt in (1, 2):
            token = await self.token()
            try:
                response = await self.server.http.request(
                    method,
                    self.server.url(path),
                    headers={"Authorization": f"Bearer {token}"},
                    **kwargs,
                )
            except httpx.HTTPError as exc:
                raise IgniUnavailable(f"could not reach igni server: {type(exc).__name__}") from exc
            if response.status_code != 401:
                return response
            self._held = None
            if attempt == 2:
                break
        raise IgniUnavailable("igni server refused this app's token")

    # ── What its VM needs ───────────────────────────────────────────

    async def request_vm_profile(self, wanted: VmProfileRequest) -> VmProfileRequestRecord:
        """Ask for what this app's VM needs. It runs nothing until an org admin approves.

        The same request again changes nothing and keeps its state; a
        changed one is `pending` again, and the VM keeps the profile it has
        until the admin approves the new one. Raises `VmProfileRefused`
        (over the platform's caps, malformed) or `IgniUnavailable`.
        """
        response = await self.request(
            "PUT", IgniPaths.app_vm_profile_request(self.app_id), json=wanted.body()
        )
        if response.status_code in (200, 201):
            return self._parse(response, VmProfileRequestRecord)
        raise self._failed(response)

    async def vm_profile_request(self) -> VmProfileRequestRecord | None:
        """This app's request and what became of it; `None` if it never asked."""
        response = await self.request("GET", IgniPaths.app_vm_profile_request(self.app_id))
        if response.status_code == 404:
            return None
        if response.status_code == 200:
            return self._parse(response, VmProfileRequestRecord)
        raise self._failed(response)

    async def vm_profile(self) -> AppVmProfile:
        """The profile this app's VM gets now — approved, or the organisation's default."""
        response = await self.request("GET", IgniPaths.app_vm_profile(self.app_id))
        if response.status_code == 200:
            return self._parse(response, AppVmProfile)
        raise self._failed(response)

    @staticmethod
    def _parse[M: BaseModel](response: httpx.Response, model: type[M]) -> M:
        try:
            return model.model_validate(response.json())
        except (ValidationError, ValueError) as exc:
            raise IgniUnavailable("igni answered with something else") from exc

    @staticmethod
    def _failed(response: httpx.Response) -> Exception:
        if 400 <= response.status_code < 500 and response.status_code != 429:
            return refusal(response)
        return IgniUnavailable(f"igni answered {response.status_code}")
