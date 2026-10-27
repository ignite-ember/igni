"""igni server's current token on each HTTP request: :class:`CloudTokenAuth`.

A model client is built once, with its agent. The token it would carry
is not fixed: in a producer's VM the agent is built before the VM has
exchanged its credential for one, and the token it gets lasts fifteen
minutes; on the desktop a sign-in or a refresh replaces it. So a client
for igni server's gateway reads the token when it sends, through the
same credentials every other igni server call reads (the layer's source
in the cloud, ``credentials.json`` on the desktop).
"""

from __future__ import annotations

from collections.abc import Generator

import httpx

from ember_code.core.auth.credentials import CloudCredentials


class CloudTokenAuth(httpx.Auth):
    """``Authorization: Bearer <the current token>`` on each request.

    Refused with a 401, the request goes again once if the token has
    changed meanwhile — it was rotated between the read and the answer.
    """

    def __init__(self, credentials_file: str | None = None) -> None:
        self._credentials_file = credentials_file

    def token(self) -> str | None:
        return CloudCredentials(self._credentials_file).access_token

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        sent = self._apply(request)
        response = yield request
        if response.status_code == 401:
            fresh = self.token()
            if fresh and fresh != sent:
                self._apply(request, fresh)
                yield request

    def _apply(self, request: httpx.Request, token: str | None = None) -> str | None:
        token = token if token is not None else self.token()
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        return token


__all__ = ["CloudTokenAuth"]
