"""How the ``anthropic`` provider's key goes out: as ``X-Api-Key``.

The SDK from 2.0 on sends an API key as ``Authorization: Bearer <key>``
rather than in ``X-Api-Key``. Anthropic's API reads either, but the
provider is also for Anthropic-compatible gateways inside a customer's
network, and those read ``X-Api-Key`` — which is what igni has always
sent and what docs/CONFIGURATION.md promises. So the key is moved back,
on every request, retries included, the way the SDK's migration guide
describes for a gateway that reads ``X-Api-Key``.

Moving it also keeps ``Authorization`` free for what it carried before:
igni server's current token where the key is ``cloud_token``
(:class:`~ember_code.core.auth.http_auth.CloudTokenAuth`, read at each
request and retried once on a 401 when it has rotated).

Imported only when the provider builds a model: ``httpx2`` arrives with
the SDK.
"""

from __future__ import annotations

from collections.abc import Generator

import httpx2

from ember_code.core.auth.http_auth import CloudTokenAuth


class AnthropicKeyAuth(httpx2.Auth):
    """``X-Api-Key: <key>``, and igni server's token where there is one."""

    def __init__(self, key: str, cloud: CloudTokenAuth | None = None) -> None:
        self._key = key
        self._cloud = cloud

    def auth_flow(
        self, request: httpx2.Request
    ) -> Generator[httpx2.Request, httpx2.Response, None]:
        if request.headers.get("Authorization") == f"Bearer {self._key}":
            del request.headers["Authorization"]
            request.headers["X-Api-Key"] = self._key
        if self._cloud is None:
            yield request
            return
        # CloudTokenAuth's own flow, on httpx2's request and response.
        sent = self._bear(request, self._cloud.token())
        response = yield request
        if response.status_code == 401:
            fresh = self._cloud.token()
            if fresh and fresh != sent:
                self._bear(request, fresh)
                yield request

    @staticmethod
    def _bear(request: httpx2.Request, token: str | None) -> str | None:
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        return token


__all__ = ["AnthropicKeyAuth"]
