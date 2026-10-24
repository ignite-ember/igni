"""igni's token endpoint, as this service calls it with its own key.

Every grant this service asks for — `client_credentials` for itself,
`authorization_code` and `refresh_token` for a person signed in to its
pages — authenticates the same way: a `private_key_jwt` client assertion,
single use, addressed to igni's issuer.
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel, ValidationError

from igni_service.errors import IgniError, IgniUnavailable
from igni_service.registration import ServiceRegistration
from igni_service.server import IgniServer
from igni_service.signing import SigningKey
from igni_service.urls import IgniPaths

ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


class IssuedTokens(BaseModel):
    """What the token endpoint answers."""

    access_token: str
    expires_in: int = 600
    refresh_token: str = ""
    token_type: str = "Bearer"


class GrantRefused(IgniError):
    """igni answered and refused the grant: `invalid_client`, `invalid_grant`."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status} {detail}")
        self.status = status
        self.detail = detail


class TokenEndpoint:
    """`POST /v1/oauth/token`, authenticated as this service."""

    def __init__(
        self, igni: IgniServer, registration: ServiceRegistration, key: SigningKey
    ) -> None:
        self._igni = igni
        self._registration = registration
        self._key = key

    async def grant(self, form: dict[str, str]) -> IssuedTokens:
        """Ask for tokens. Raises `GrantRefused` or `IgniUnavailable`."""
        client_id = await self._registration.client_id()
        authenticated = {
            **form,
            "client_assertion_type": ASSERTION_TYPE,
            "client_assertion": await self._key.assertion(
                client_id=client_id, audience=await self._igni.issuer()
            ),
        }
        try:
            response = await self._igni.http.post(
                self._igni.url(IgniPaths.TOKEN), data=authenticated
            )
        except httpx.HTTPError as exc:
            raise IgniUnavailable(
                f"could not reach igni server for a token: {type(exc).__name__}"
            ) from exc
        if response.status_code in (400, 401, 403):
            raise GrantRefused(response.status_code, response.text)
        if response.status_code != 200:
            raise IgniUnavailable(f"igni server answered a token request {response.status_code}")
        try:
            return IssuedTokens.model_validate(response.json())
        except (ValidationError, ValueError) as exc:
            raise IgniUnavailable(
                "igni server answered a token request with something else"
            ) from exc
