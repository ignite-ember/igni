"""Cross-origin calls from an extension's page, with its token and nothing else.

An extension's page runs in igni's window, on igni's origin, and calls its
service with `bridge.service.fetch`: a bearer token in `Authorization`, no
cookies. That is a cross-origin request, so the service has to say yes to
it — and only to it.

The yes is given per route, from what the route already depends on:

- A route that takes a person from their token (`igni.person`,
  `igni.optional_person`, `igni.org_admin`, or the token itself with
  `igni.token` / `igni.optional_token` — directly, through a router's
  dependencies, or through the service's own dependency) is a **bearer
  route**. Any origin may call it, because the token is the whole of the
  authority: a page without one gets nothing it could not get with `curl`.
- A route that reads the service's own sign-in cookie (`igni.web.*`), igni's
  own calls (`igni.igni_call`), and every route that takes no token at all
  are left alone. Cross-origin, the browser refuses them.

Credentials are never allowed: no `Access-Control-Allow-Credentials`, so a
browser does not send the service's cookies on a cross-origin call, and does
not show a page the answer to one that sent them anyway. A person's session
on the service's website stays the website's.

Which route a call reaches is known once it is routed, so the yes is given
in two halves:

- **The preflight** is answered before routing: `*`, the methods, and the
  two headers a token call needs — only when the call will carry
  `Authorization`, and never for the sign-in routes or igni's own. It lets
  a cookie-less call be *sent*, which any page can do with a form anyway;
  it does not let the page read the answer.
- **The answer** carries `*` only when the call had a bearer token and the
  route it reached is a bearer route. Without that the browser keeps the
  answer from the page. A refused token is still answered with `*`, so the
  page can read the 401 and fetch a new one.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from igni_service.urls import IgniPaths
from igni_service.web import WebSignIn

if TYPE_CHECKING:
    from igni_service.service import IgniService


class BearerRoutes:
    """Which routes take a person's token, and nothing else."""

    def __init__(self, igni: IgniService) -> None:
        self._igni = igni
        self._bearer = {
            type(igni).person,
            type(igni).optional_person,
            type(igni).org_admin,
            type(igni).token,
            type(igni).optional_token,
        }
        self._known: dict[int, bool] = {}

    def is_bearer(self, route: Any) -> bool:
        """Whether `route` — the one a call was routed to — is a bearer route."""
        if not isinstance(route, APIRoute):
            return False
        known = self._known.get(id(route))
        if known is None:
            known = self._judge(route.dependant)
            self._known[id(route)] = known
        return known

    def _judge(self, dependant: Dependant) -> bool:
        bearer = False
        for call in self._calls(dependant):
            owner = getattr(call, "__self__", None)
            if owner is self._igni.web:
                return False
            if owner is self._igni:
                if getattr(call, "__func__", None) not in self._bearer:
                    return False
                bearer = True
        return bearer

    def _calls(self, dependant: Dependant) -> Iterable[Any]:
        for sub in dependant.dependencies:
            if sub.call is not None:
                yield sub.call
            yield from self._calls(sub)


class BearerCors:
    """ASGI middleware: cross-origin calls to bearer routes, from any origin, without credentials."""

    #: The headers a page's token call sends that are not CORS-safelisted.
    ALLOW_HEADERS = frozenset({"authorization", "content-type"})
    ALLOW_METHODS = ("DELETE", "GET", "HEAD", "PATCH", "POST", "PUT")
    #: How long a browser may keep a preflight's answer.
    MAX_AGE = 600
    #: Never a bearer route: sign-in to the service's pages, and igni's own calls.
    CLOSED = (WebSignIn.PREFIX + "/", IgniPaths.SUBJECTS + "/", IgniPaths.ORGANISATIONS + "/")

    def __init__(self, app: ASGIApp, *, igni: IgniService) -> None:
        self.app = app
        self.routes = BearerRoutes(igni)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = Headers(scope=scope) if scope["type"] == "http" else None
        if headers is None or "origin" not in headers:
            await self.app(scope, receive, send)
        elif scope["method"] == "OPTIONS" and "access-control-request-method" in headers:
            await self._preflight(scope, headers, receive, send)
        elif self._carries_bearer(headers):
            await self.app(scope, receive, self._allowing(scope, send))
        else:
            await self.app(scope, receive, send)

    async def _preflight(
        self, scope: Scope, headers: Headers, receive: Receive, send: Send
    ) -> None:
        if self._closed(scope["path"]):
            # The app answers as it would, without a yes.
            await self.app(scope, receive, send)
            return
        method = headers["access-control-request-method"].upper()
        asked = {
            name.strip().lower()
            for name in headers.get("access-control-request-headers", "").split(",")
            if name.strip()
        }
        response: Response
        if (
            method in self.ALLOW_METHODS
            and "authorization" in asked
            and asked <= self.ALLOW_HEADERS
        ):
            response = Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": ", ".join(self.ALLOW_METHODS),
                    "Access-Control-Allow-Headers": "Authorization, Content-Type",
                    "Access-Control-Max-Age": str(self.MAX_AGE),
                },
            )
        else:
            response = PlainTextResponse(
                "Cross-origin calls carry a bearer token and nothing else", status_code=403
            )
        await response(scope, receive, send)

    def _closed(self, path: str) -> bool:
        return any(f"{path}/".startswith(prefix) for prefix in self.CLOSED)

    @staticmethod
    def _carries_bearer(headers: Headers) -> bool:
        scheme, _, token = headers.get("authorization", "").partition(" ")
        return scheme.lower() == "bearer" and bool(token.strip())

    def _allowing(self, scope: Scope, send: Send) -> Send:
        """`send`, saying yes once routing has put a bearer route in `scope`."""
        routes = self.routes

        async def allowing(message: Message) -> None:
            if message["type"] == "http.response.start" and routes.is_bearer(scope.get("route")):
                response_headers = MutableHeaders(scope=message)
                response_headers["Access-Control-Allow-Origin"] = "*"
                response_headers["Access-Control-Expose-Headers"] = "*"
                del response_headers["Access-Control-Allow-Credentials"]
            await send(message)

        return allowing
