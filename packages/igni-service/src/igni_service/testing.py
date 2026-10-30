"""igni server, played in-process, so a service can be tested without one.

    fake = FakeIgni()
    fake.add_member("org-1", "u-ada", email="ada@acme.test", name="Ada")
    igni = fake.service()                     # an IgniService wired to the fake
    app = FastAPI(lifespan=igni.lifespan)
    ...
    headers = {"Authorization": f"Bearer {fake.person_token('u-ada', org_id='org-1')}"}

It is a real issuer in every way a service can observe: it holds an RSA
key and publishes the public half with a `max-age`; it signs tokens RS256
with a `kid`; its issuer is not the address it is reached at, as a
deployment's is not. It checks what igni checks — client assertions
against the registered key with single-use `jti`s, bundle signatures and
digests, notification limits — and records what it was sent:
`fake.notify_calls` (each call as sent), `fake.deliveries` (each
person told, after igni's rules) and `fake.revoked` (each refresh token
revoked, RFC 7009). `fake.delete_organisation(org_id)` plays igni deleting
an organisation and hands back the call it owes the service.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import uuid
from collections.abc import Collection
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
from jwt.api_jws import PyJWS
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from igni_service.bundle import BundleArchive
from igni_service.clock import Clock
from igni_service.crypto import ALGORITHM, AssetClaim, Base64Url, BundleClaim, Jwk, RsaKeyPair
from igni_service.directory import Member, Organisation
from igni_service.keystore import KeyStore, MemoryKeyStore
from igni_service.notifications import Notification, NotificationBatch, Notifier
from igni_service.registration import PublishedExtension
from igni_service.revocations import Revocation, RevocationPage, RevokedKind
from igni_service.service import IgniService
from igni_service.settings import IgniSettings
from igni_service.testing_items import FakeAgents, SentEvent
from igni_service.tokens import PEOPLE_ONLY, CallerKind
from igni_service.urls import IgniPaths, ServiceUrl


class ManualClock(Clock):
    """A clock that moves only when told to."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self._now = start

    def wall(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class SeenRequest(BaseModel):
    """One request the fake was sent."""

    method: str
    path: str
    headers: dict[str, str]
    body: bytes
    query: dict[str, list[str]] = Field(default_factory=dict)

    def json_body(self) -> Any:
        return json.loads(self.body)

    def form(self) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(self.body.decode()).items()}


class FakeClient(BaseModel):
    """A registration, as igni keeps it."""

    client_id: str
    client_name: str
    client_uri: str
    kind: str
    redirect_uris: list[str]
    state: str = "pending"
    thumbprint: str
    jwk: Jwk


class IssuedCode(BaseModel):
    """An authorization code the portal handed a browser, until it is used."""

    client_id: str
    redirect_uri: str
    challenge: str
    user_id: str
    org_id: str
    expires_at: float


class HeldRefresh(BaseModel):
    client_id: str
    user_id: str
    org_id: str
    #: The sign-in it belongs to: what a revocation ends.
    grant: str


class HeldExtension(BaseModel):
    client_id: str
    extension_id: str
    version: str
    sha256: str
    archive: bytes
    pages: list[str] = Field(default_factory=list)


class NotifyCall(BaseModel):
    """One `POST /v1/service-notifications`, as the service sent it."""

    org_id: str
    client_id: str
    notifications: list[Notification]


class Delivery(BaseModel):
    """One person told about one notification, after igni's rules."""

    model_config = ConfigDict(frozen=True)

    org_id: str
    user_id: str
    source: str
    notification: Notification


class OrganisationDeletion(BaseModel):
    """What igni sends a service when it deletes an organisation."""

    model_config = ConfigDict(frozen=True)

    org_id: str
    path: str
    headers: dict[str, str]

    async def send(self, http: httpx.AsyncClient) -> httpx.Response:
        """Deliver it to the service `http` reaches, as igni does — again, if called again."""
        return await http.delete(self.path, headers=self.headers)


class FakeOrganisation(BaseModel):
    organisation: Organisation
    members: dict[str, Member] = Field(default_factory=dict)


class FakeIgni:
    """Every igni endpoint a service calls, in one object."""

    #: Where the service reaches the fake.
    BASE = "http://igni.test"
    #: Its issuer: its public URL, deliberately not `BASE`.
    ISSUER = "https://igni.example.test"
    #: The service under test's own URL.
    SERVICE_URL = "http://service.test"
    #: The longest a client assertion may live.
    MAX_ASSERTION_LIFETIME = 300

    def __init__(
        self,
        *,
        base_url: str = BASE,
        issuer: str = ISSUER,
        service_url: str = SERVICE_URL,
        clock: Clock | None = None,
        require_approval: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.issuer = issuer.rstrip("/")
        self.service_url = ServiceUrl.normalise(service_url)
        self.clock = clock or Clock()
        #: When set, a pending registration gets no service token.
        self.require_approval = require_approval
        self.pair = RsaKeyPair.generate()
        self.kid = "igni-key-1"
        #: Keys published besides the signing one, by kid.
        self.published: dict[str, RsaKeyPair] = {self.kid: self.pair}
        self.max_age = 300
        self.organisations: dict[str, FakeOrganisation] = {}
        self.clients: dict[str, FakeClient] = {}
        self.extensions: dict[str, HeldExtension] = {}
        #: Uploaded assets, by `(client_id, sha256)`.
        self.assets: dict[tuple[str, str], bytes] = {}
        self.seen: list[SeenRequest] = []
        self.notify_calls: list[NotifyCall] = []
        self.deliveries: list[Delivery] = []
        #: The agent channel: items, uploaded files, their states
        #: (`igni_service.testing_items`).
        self.agents = FakeAgents(
            is_member=lambda org_id, user_id: self._member(org_id, user_id) is not None,
            clock=self.clock.wall,
        )
        #: Path prefix → status to answer instead, to play igni failing.
        self.failing: dict[str, int] = {}
        self._service_tokens: dict[str, tuple[str, str]] = {}
        self._jtis: set[str] = set()
        self._told: set[tuple[str, str, str]] = set()
        self._codes: dict[str, IssuedCode] = {}
        self._refresh_tokens: dict[str, HeldRefresh] = {}
        #: Every grant the token endpoint answered, by `grant_type`.
        self.grants: list[str] = []
        #: Every refresh token revoked at `/v1/oauth/revoke`, as presented.
        self.revoked: list[str] = []
        #: igni's revocations, in order (`publish_revocation`).
        self.revocations: list[Revocation] = []

    def reset(self) -> None:
        """Forget what happened, keep who exists.

        For a fake shared across a session: requests, notifications,
        deliveries, the once-per-event memory, failures and issued codes go;
        organisations, members, registered clients, keys and uploaded
        extensions stay, so the next test starts from the same world.
        """
        self.seen.clear()
        self.notify_calls.clear()
        self.deliveries.clear()
        self.agents.reset()
        self.failing.clear()
        self.grants.clear()
        self.revoked.clear()
        self._told.clear()
        self._codes.clear()

    # ── Setting the scene ───────────────────────────────────────────

    def add_organisation(
        self,
        org_id: str,
        *,
        name: str = "",
        slug: str = "",
        tenant_number: int = 1,
        domains: list[str] | None = None,
    ) -> Organisation:
        organisation = Organisation(
            org_id=org_id,
            name=name or org_id,
            slug=slug or org_id,
            tenant_number=tenant_number,
            domains=list(domains or []),
        )
        held = self.organisations.get(org_id)
        if held is None:
            self.organisations[org_id] = FakeOrganisation(organisation=organisation)
        else:
            held.organisation = organisation
        return organisation

    def add_member(
        self,
        org_id: str,
        user_id: str,
        *,
        email: str = "",
        name: str = "",
        role: str = "member",
        status: str = "active",
        super_admin: bool = False,
    ) -> Member:
        if org_id not in self.organisations:
            self.add_organisation(org_id)
        member = Member(
            user_id=user_id,
            email=email,
            name=name,
            role=role,
            status=status,
            super_admin=super_admin,
        )
        self.organisations[org_id].members[user_id] = member
        return member

    def delete_organisation(self, org_id: str) -> OrganisationDeletion:
        """Delete an organisation, as igni does: it leaves the directory,
        and the service is owed the call that says so.

        `await fake.delete_organisation(ORG).send(http)` delivers it.
        """
        self.organisations.pop(org_id, None)
        return OrganisationDeletion(
            org_id=org_id,
            path=f"{IgniPaths.ORGANISATIONS}/{org_id}",
            headers={"Authorization": f"Bearer {self.igni_token(org_id)}"},
        )

    def service(
        self,
        settings: IgniSettings | None = None,
        *,
        key_store: KeyStore | None = None,
        clock: Clock | None = None,
        accepted_callers: Collection[CallerKind] = PEOPLE_ONLY,
    ) -> IgniService:
        """An `IgniService` wired to this fake: key in memory, retries without waiting."""
        service = IgniService(
            settings or self.settings(),
            key_store=key_store or MemoryKeyStore(),
            http=self.http_client(),
            clock=clock or self.clock,
            retry_min=0.01,
            accepted_callers=accepted_callers,
        )
        service.notifier = Notifier(service.tokens, sleep=self.no_sleep)
        return service

    @staticmethod
    async def no_sleep(_: float) -> None:
        """Retries in tests do not wait."""

    def settings(self, **overrides: Any) -> IgniSettings:
        values: dict[str, Any] = {
            "igni_server_url": self.base_url,
            "public_base_url": self.service_url,
            "service_name": "Service under test",
        }
        values.update(overrides)
        return IgniSettings.model_validate(values)

    def approve(self, client_id: str | None = None) -> None:
        for client in self._clients(client_id):
            client.state = "approved"

    def revoke(self, client_id: str | None = None) -> None:
        for client in self._clients(client_id):
            client.state = "revoked"

    def rotate(self) -> str:
        """Publish and sign with a new key, keeping the old one published."""
        self.pair = RsaKeyPair.generate()
        self.kid = f"igni-key-{len(self.published) + 1}"
        self.published[self.kid] = self.pair
        return self.kid

    def expire_service_tokens(self) -> None:
        """Stop honouring every service token issued so far, as a key move would."""
        self._service_tokens.clear()

    @property
    def registration(self) -> FakeClient | None:
        """The one registration, when there is one."""
        return next(iter(self.clients.values()), None)

    def _clients(self, client_id: str | None) -> list[FakeClient]:
        return [c for c in self.clients.values() if client_id is None or c.client_id == client_id]

    # ── The portal's sign-in page ───────────────────────────────────

    @property
    def authorization_endpoint(self) -> str:
        """The portal's `/authorize`, as igni's metadata publishes it."""
        return f"{self.issuer}/authorize"

    def authorize(self, location: str, *, user_id: str, org_id: str) -> str:
        """Play the portal: `location` is where the service sent the browser.

        Signs `user_id` in to `org_id` and answers where the portal sends
        the browser back: the client's redirect URI with a code and the
        `state`. Raises `ValueError` for anything the portal would refuse.
        """
        parts = urlsplit(location)
        if f"{parts.scheme}://{parts.netloc}{parts.path}" != self.authorization_endpoint:
            raise ValueError(f"{location} is not igni's sign-in page")
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        client = self._client(query.get("client_id", ""))
        if client is None or client.state == "revoked":
            raise ValueError("unknown client")
        if query.get("response_type") != "code" or query.get("code_challenge_method") != "S256":
            raise ValueError("only the code flow with S256 is supported")
        if query.get("redirect_uri") not in client.redirect_uris:
            raise ValueError(f"{query.get('redirect_uri')} is not a registered redirect URI")
        if not query.get("code_challenge") or not query.get("state"):
            raise ValueError("a challenge and a state are required")
        member = self._member(org_id, user_id)
        if member is None or not member.is_active:
            raise ValueError(f"{user_id} is not an active member of {org_id}")
        code = uuid.uuid4().hex
        self._codes[code] = IssuedCode(
            client_id=client.client_id,
            redirect_uri=query["redirect_uri"],
            challenge=query["code_challenge"],
            user_id=member.user_id,
            org_id=org_id,
            expires_at=self.clock.wall() + 60,
        )
        separator = "&" if "?" in query["redirect_uri"] else "?"
        return f"{query['redirect_uri']}{separator}{urlencode({'code': code, 'state': query['state']})}"

    def deactivate(self, org_id: str, user_id: str) -> None:
        """Somebody leaves: their refresh tokens stop working at once."""
        member = self.organisations[org_id].members[user_id]
        self.organisations[org_id].members[user_id] = member.model_copy(
            update={"status": "deactivated"}
        )

    # ── Tokens igni would sign ──────────────────────────────────────

    def token(
        self,
        sub: str,
        *,
        use: str = "user",
        aud: str | None = None,
        iss: str | None = None,
        lifetime: float | timedelta = 600,
        kid: str | None = None,
        key: RsaKeyPair | None = None,
        **claims: Any,
    ) -> str:
        now = int(self.clock.wall())
        if isinstance(lifetime, timedelta):
            lifetime = lifetime.total_seconds()
        payload: dict[str, Any] = {
            "iss": iss if iss is not None else self.issuer,
            "sub": sub,
            "aud": aud if aud is not None else self.service_url,
            "token_use": use,
            "iat": now,
            "iat_ms": int(self.clock.wall() * 1000),
            "exp": int(now + lifetime),
            **claims,
        }
        return (key or self.pair).sign_jwt(payload, kid=kid or self.kid)

    def app_token(self, app_id: str, *, org_id: str, name: str = "", **overrides: Any) -> str:
        """An app's token exchanged for this service (igni's service-identity §9.2)."""
        claims: dict[str, Any] = {"org": org_id, "kind": "app", "name": name or app_id}
        claims.update(overrides)
        return self.token(app_id, use="app", **claims)

    def space_token(
        self, space_id: str, *, org_id: str, app_id: str, external_id: str, **overrides: Any
    ) -> str:
        """A space's token exchanged for this service (§9.2, §17)."""
        claims: dict[str, Any] = {
            "org": org_id,
            "kind": "space",
            "app": app_id,
            "external_id": external_id,
        }
        claims.update(overrides)
        return self.token(space_id, use="space", **claims)

    def person_token(self, user_id: str, *, org_id: str, **overrides: Any) -> str:
        """A user token as igni issues one: the person's claims, for one organisation."""
        held = self.organisations.get(org_id)
        member = held.members.get(user_id) if held else None
        claims: dict[str, Any] = {
            "org": org_id,
            "org_name": held.organisation.name if held else org_id,
            "org_role": member.role if member else "member",
            "email": member.email if member else "",
            "name": member.name if member else "",
            "super_admin": member.super_admin if member else False,
        }
        claims.update(overrides)
        return self.token(user_id, **claims)

    def igni_token(self, org_id: str, **overrides: Any) -> str:
        """What igni signs to call the service about one organisation."""
        claims: dict[str, Any] = {"use": "igni", "org": org_id}
        claims.update(overrides)
        sub = str(claims.pop("sub", self.issuer))
        return self.token(sub, **claims)

    # ── The wire ────────────────────────────────────────────────────

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport())

    def requests_to(self, path: str) -> list[SeenRequest]:
        return [seen for seen in self.seen if seen.path == path]

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        if f"{url.scheme}://{url.netloc}" != self.base_url:
            raise httpx.ConnectError(f"{url.netloc} is not igni", request=request)
        seen = SeenRequest(
            method=request.method,
            path=url.path,
            headers={k.lower(): v for k, v in request.headers.items()},
            body=request.read(),
            query=parse_qs(url.query),
        )
        self.seen.append(seen)
        for prefix, status in self.failing.items():
            if url.path.startswith(prefix):
                return httpx.Response(status, json={"detail": "failing on purpose"})
        return self._route(seen)

    def _route(self, seen: SeenRequest) -> httpx.Response:
        path = seen.path
        if path == IgniPaths.DISCOVERY:
            return httpx.Response(
                200,
                json={
                    "issuer": self.issuer,
                    "jwks_uri": f"{self.issuer}{IgniPaths.JWKS}",
                    "token_endpoint": f"{self.issuer}{IgniPaths.TOKEN}",
                    "revocation_endpoint": f"{self.issuer}{IgniPaths.REVOKE}",
                    "authorization_endpoint": self.authorization_endpoint,
                },
            )
        if path == IgniPaths.JWKS:
            keys = [pair.public_jwk(kid).as_dict() for kid, pair in self.published.items()]
            return httpx.Response(
                200,
                json={"keys": keys},
                headers={"Cache-Control": f"public, max-age={self.max_age}"},
            )
        if path == IgniPaths.CLIENTS and seen.method == "POST":
            return self._register(seen)
        if path.startswith(f"{IgniPaths.CLIENTS}/") and path.endswith("/extension"):
            return self._extension(seen)
        if path.startswith(f"{IgniPaths.CLIENTS}/") and "/extension/assets/" in path:
            return self._asset(seen)
        if path == IgniPaths.TOKEN:
            return self._issue(seen)
        if path == IgniPaths.REVOKE:
            return self._revoke(seen)
        if (
            path.startswith(f"{IgniPaths.DIRECTORY}/")
            or path == IgniPaths.NOTIFICATIONS
            or path == IgniPaths.SERVICE_EVENTS
            or path.startswith(f"{IgniPaths.SERVICE_EVENTS}/")
            or path == IgniPaths.SERVICE_ITEMS
            or path == IgniPaths.SERVICE_ATTACHMENTS
            or path == IgniPaths.REVOCATIONS
            or path == IgniPaths.SERVED
        ):
            held = self._service_tokens.get(
                seen.headers.get("authorization", "").removeprefix("Bearer ")
            )
            client = self._client(held[0]) if held else None
            if held is None or client is None or client.state == "revoked":
                return httpx.Response(401, json={"detail": "Not authenticated"})
            client_id, org_id = held
            if not org_id and path not in (IgniPaths.REVOCATIONS, IgniPaths.SERVED):
                return httpx.Response(401, json={"detail": "Not authenticated"})
            if path == IgniPaths.REVOCATIONS:
                return self._revocations(seen)
            if path == IgniPaths.SERVED:
                return self._served()
            if path == IgniPaths.NOTIFICATIONS:
                return self._notify(seen, client_id, org_id)
            if path == IgniPaths.SERVICE_EVENTS and seen.method == "POST":
                return self.agents.event(seen.body, client_id, org_id)
            if path == IgniPaths.SERVICE_ITEMS and seen.method == "POST":
                return self.agents.item(seen.body, client_id, org_id)
            if path == IgniPaths.SERVICE_ATTACHMENTS and seen.method == "POST":
                return self.agents.upload(
                    client_id=client_id,
                    filename=(seen.query.get("filename") or [""])[0],
                    content_type=seen.headers.get("content-type", ""),
                    data=seen.body,
                )
            if path.startswith(f"{IgniPaths.SERVICE_EVENTS}/") and seen.method == "GET":
                return self.agents.status(path.rsplit("/", 1)[1], client_id)
            return self._directory(seen, org_id)
        return httpx.Response(404, json={"detail": "Not Found"})

    # ── Revocations (service-identity §21) ──────────────────────────

    def publish_revocation(
        self,
        subject_id: str,
        *,
        org_id: str | None,
        kind: str = "person",
        reason: str = "removed",
        incarnation: str | None = None,
    ) -> Revocation:
        """Publish a watermark: ``subject_id``'s tokens issued until now stop.

        ``org_id=None`` is every organisation (a person's sessions withdrawn).
        """
        revocation = Revocation(
            kind=RevokedKind(kind),
            org_id=org_id,
            subject_id=subject_id,
            incarnation=incarnation,
            not_before_ms=int(self.clock.wall() * 1000),
            reason=reason,
        )
        self.revocations.append(revocation)
        return revocation

    def _served(self) -> httpx.Response:
        served = [
            {"org_id": org_id, "name": held.organisation.name}
            for org_id, held in self.organisations.items()
        ]
        return httpx.Response(200, json={"organisations": served})

    def _revocations(self, seen: SeenRequest) -> httpx.Response:
        """Every served organisation's revocations on one cursor — at once, never held."""
        since = int((seen.query.get("since") or ["0"])[0] or 0)
        cursor = str(len(self.revocations))
        if seen.headers.get("if-none-match") == f'"{cursor}"':
            return httpx.Response(304)
        page = RevocationPage(revocations=self.revocations[since:], cursor=cursor)
        return httpx.Response(200, json=page.model_dump(mode="json"))

    # ── Registration and the extension ──────────────────────────────

    def _register(self, seen: SeenRequest) -> httpx.Response:
        body = seen.json_body()
        keys = body.get("jwks", {}).get("keys", [])
        if len(keys) != 1 or "d" in keys[0]:
            return self._invalid_metadata("exactly one public key is required")
        jwk = Jwk.model_validate(keys[0])
        try:
            client_uri = ServiceUrl.normalise(body.get("client_uri", ""))
        except ValueError as exc:
            return self._invalid_metadata(str(exc))
        thumbprint = jwk.kid
        existing = next((c for c in self.clients.values() if c.thumbprint == thumbprint), None)
        if existing is None:
            existing = FakeClient(
                client_id=str(uuid.uuid4()),
                client_name=body.get("client_name", ""),
                client_uri=client_uri,
                kind=body.get("kind", "service"),
                redirect_uris=body.get("redirect_uris", []),
                thumbprint=thumbprint,
                jwk=jwk,
            )
            self.clients[existing.client_id] = existing
        return httpx.Response(
            201,
            json={
                "client_id": existing.client_id,
                "client_id_issued_at": 0,
                "client_name": existing.client_name,
                "client_uri": existing.client_uri,
                "kind": existing.kind,
                "redirect_uris": existing.redirect_uris,
                "token_endpoint_auth_method": "private_key_jwt",
                "state": existing.state,
                "thumbprint": existing.thumbprint,
            },
        )

    @staticmethod
    def _invalid_metadata(description: str) -> httpx.Response:
        return httpx.Response(
            400, json={"error": "invalid_client_metadata", "error_description": description}
        )

    def _client(self, client_id: str) -> FakeClient | None:
        return self.clients.get(client_id)

    def _extension(self, seen: SeenRequest) -> httpx.Response:
        client_id = seen.path.split("/")[3]
        client = self._client(client_id)
        held = self.extensions.get(client_id)
        if seen.method == "GET":
            if held is None:
                return httpx.Response(404, json={"detail": "Nothing uploaded for this client"})
            return httpx.Response(200, json=self._published(held).model_dump())
        if client is None:
            return httpx.Response(404, json={"detail": "no such client"})
        if client.state == "revoked":
            return httpx.Response(403, json={"detail": "revoked"})
        try:
            payload = PyJWS().decode(
                seen.headers.get("x-igni-bundle-signature", ""),
                client.jwk.public_key(),
                algorithms=[ALGORITHM],
            )
            claim = BundleClaim.model_validate_json(payload)
        except (jwt.PyJWTError, ValidationError, ValueError):
            return httpx.Response(
                400, json={"detail": "not signed by this client's registered key"}
            )
        if claim.sha256 != hashlib.sha256(seen.body).hexdigest():
            return httpx.Response(400, json={"detail": "the signature is for other bytes"})
        manifest = self._manifest(seen.body)
        if manifest is None:
            return httpx.Response(400, json={"detail": "no manifest.json at the bundle's root"})
        if manifest.get("id") != claim.extension or manifest.get("version") != claim.version:
            return httpx.Response(400, json={"detail": "the manifest is not what was signed"})
        missing = [
            str(asset.get("name"))
            for asset in manifest.get("assets") or []
            if isinstance(asset, dict) and (client_id, asset.get("sha256")) not in self.assets
        ]
        if missing:
            return httpx.Response(
                400, json={"detail": f"assets not uploaded first: {', '.join(missing)}"}
            )
        shipped_elsewhere = any(
            held.extension_id == claim.extension and other != client_id
            for other, held in self.extensions.items()
            if self.clients[other].state == "approved"
        )
        if shipped_elsewhere:
            return httpx.Response(409, json={"detail": "another service ships this extension"})
        pages = [
            str(page.get("kind"))
            for page in manifest.get("contributes", {}).get("pages", [])
            if isinstance(page, dict)
        ]
        self.extensions[client_id] = HeldExtension(
            client_id=client_id,
            extension_id=claim.extension,
            version=claim.version,
            sha256=claim.sha256,
            archive=seen.body,
            pages=pages,
        )
        return httpx.Response(200, json=self._published(self.extensions[client_id]).model_dump())

    def _asset(self, seen: SeenRequest) -> httpx.Response:
        parts = seen.path.split("/")
        client_id, sha256 = parts[3], parts[-1]
        client = self._client(client_id)
        if seen.method == "HEAD":
            return httpx.Response(200 if (client_id, sha256) in self.assets else 404)
        if client is None:
            return httpx.Response(404, json={"detail": "no such client"})
        try:
            payload = PyJWS().decode(
                seen.headers.get("x-igni-asset-signature", ""),
                client.jwk.public_key(),
                algorithms=[ALGORITHM],
            )
            claim = AssetClaim.model_validate_json(payload)
        except (jwt.PyJWTError, ValidationError, ValueError):
            return httpx.Response(
                400, json={"detail": "not signed by this client's registered key"}
            )
        if claim.asset != sha256 or hashlib.sha256(seen.body).hexdigest() != sha256:
            return httpx.Response(400, json={"detail": "the signature is for other bytes"})
        if claim.size != len(seen.body):
            return httpx.Response(400, json={"detail": "the size is not what was signed"})
        self.assets[(client_id, sha256)] = seen.body
        return httpx.Response(200, json={"sha256": sha256, "size": len(seen.body)})

    @staticmethod
    def _published(held: HeldExtension) -> PublishedExtension:
        return PublishedExtension(
            extension_id=held.extension_id, version=held.version, sha256=held.sha256
        )

    @staticmethod
    def _manifest(archive: bytes) -> dict[str, Any] | None:
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
                for member in tar:
                    if member.isfile() and member.name.removeprefix("./") == "manifest.json":
                        extracted = tar.extractfile(member)
                        if extracted is not None:
                            loaded: dict[str, Any] = json.loads(extracted.read())
                            return loaded
        except (tarfile.TarError, OSError, ValueError):
            return None
        return None

    # ── Tokens ──────────────────────────────────────────────────────

    def _issue(self, seen: SeenRequest) -> httpx.Response:
        form = seen.form()
        if (
            form.get("client_assertion_type")
            != "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        ):
            return httpx.Response(400, json={"error": "invalid_request"})
        client = self._authenticated(form.get("client_assertion", ""))
        if client is None:
            return self._invalid_client()
        grant = form.get("grant_type", "")
        self.grants.append(grant)
        if grant == "client_credentials":
            return self._client_credentials(client, form)
        if grant == "authorization_code":
            return self._authorization_code(client, form)
        if grant == "refresh_token":
            return self._refresh(client, form)
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _authenticated(self, assertion: str) -> FakeClient | None:
        """The client a `private_key_jwt` assertion proves, or `None`."""
        try:
            client_id = str(
                jwt.decode(assertion, options={"verify_signature": False}).get("iss", "")
            )
        except jwt.PyJWTError:
            return None
        client = self._client(client_id)
        if client is None or client.state == "revoked":
            return None
        if self.require_approval and client.state != "approved":
            return None
        try:
            claims = jwt.decode(
                assertion,
                client.jwk.public_key(),
                algorithms=[ALGORITHM],
                audience=[
                    self.issuer,
                    f"{self.issuer}{IgniPaths.TOKEN}",
                    f"{self.issuer}{IgniPaths.REVOKE}",
                ],
                issuer=client_id,
                options={
                    "require": ["exp", "jti", "sub"],
                    "verify_exp": False,
                    "verify_iat": False,
                },
            )
        except jwt.PyJWTError:
            return None
        lifetime = float(claims["exp"]) - self.clock.wall()
        if claims.get("sub") != client_id or not 0 < lifetime <= self.MAX_ASSERTION_LIFETIME:
            return None
        if claims["jti"] in self._jtis:
            return None
        self._jtis.add(claims["jti"])
        return client

    def _client_credentials(self, client: FakeClient, form: dict[str, str]) -> httpx.Response:
        if form.get("scope") == "revocations" and not form.get("organisation"):
            # A token for no organisation: revocations and served only (§3, §21).
            token = f"revocations-{uuid.uuid4().hex}"
            self._service_tokens[token] = (client.client_id, "")
            return httpx.Response(
                200, json={"access_token": token, "token_type": "Bearer", "expires_in": 600}
            )
        if not form.get("organisation"):
            return httpx.Response(400, json={"error": "invalid_request"})
        token = f"service-{uuid.uuid4().hex}"
        self._service_tokens[token] = (client.client_id, form["organisation"])
        return httpx.Response(
            200, json={"access_token": token, "token_type": "Bearer", "expires_in": 600}
        )

    def _authorization_code(self, client: FakeClient, form: dict[str, str]) -> httpx.Response:
        issued = self._codes.pop(form.get("code", ""), None)
        if (
            issued is None
            or issued.client_id != client.client_id
            or issued.redirect_uri != form.get("redirect_uri")
            or self.clock.wall() >= issued.expires_at
            or Base64Url.encode(hashlib.sha256(form.get("code_verifier", "").encode()).digest())
            != issued.challenge
        ):
            return httpx.Response(400, json={"error": "invalid_grant"})
        return self._user_tokens(client, issued.user_id, issued.org_id, grant=uuid.uuid4().hex)

    def _refresh(self, client: FakeClient, form: dict[str, str]) -> httpx.Response:
        held = self._refresh_tokens.pop(form.get("refresh_token", ""), None)
        if held is None or held.client_id != client.client_id:
            return httpx.Response(400, json={"error": "invalid_grant"})
        member = self._member(held.org_id, held.user_id)
        if member is None or not member.is_active:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return self._user_tokens(client, held.user_id, held.org_id, grant=held.grant)

    def _user_tokens(
        self, client: FakeClient, user_id: str, org_id: str, *, grant: str
    ) -> httpx.Response:
        refresh = f"refresh-{uuid.uuid4().hex}"
        self._refresh_tokens[refresh] = HeldRefresh(
            client_id=client.client_id, user_id=user_id, org_id=org_id, grant=grant
        )
        access = self.person_token(
            user_id, org_id=org_id, aud=client.client_uri, azp=client.client_id
        )
        return httpx.Response(
            200,
            json={
                "access_token": access,
                "refresh_token": refresh,
                "token_type": "Bearer",
                "expires_in": 600,
            },
        )

    def _revoke(self, seen: SeenRequest) -> httpx.Response:
        """RFC 7009, as igni answers it: the whole sign-in ends."""
        form = seen.form()
        if (
            form.get("client_assertion_type")
            != "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        ):
            return self._invalid_client()
        client = self._authenticated(form.get("client_assertion", ""))
        if client is None:
            return self._invalid_client()
        token = form.get("token", "")
        if not token:
            return httpx.Response(400, json={"error": "invalid_request"})
        held = self._refresh_tokens.get(token)
        if held is None:
            if token.startswith("eyJ"):  # a JWT: here, only ever an access token
                return httpx.Response(400, json={"error": "unsupported_token_type"})
            # Not a token igni would still honour: nothing to revoke.
            return httpx.Response(200)
        if held.client_id != client.client_id:
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.revoked.append(token)
        for other, also in list(self._refresh_tokens.items()):
            if also.grant == held.grant:
                del self._refresh_tokens[other]
        return httpx.Response(200)

    @staticmethod
    def _invalid_client() -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    # ── The directory ───────────────────────────────────────────────

    def _member(self, org_id: str, identity: str) -> Member | None:
        held = self.organisations.get(org_id)
        if held is None:
            return None
        wanted = identity.strip().casefold()
        return next(
            (
                m
                for m in held.members.values()
                if m.user_id.casefold() == wanted or (m.email and m.email.casefold() == wanted)
            ),
            None,
        )

    def _directory(self, seen: SeenRequest, org_id: str) -> httpx.Response:
        held = self.organisations.get(org_id)
        if held is None:
            return httpx.Response(404, json={"detail": "no such organisation"})
        path = seen.path.removeprefix(IgniPaths.DIRECTORY)
        if path == "/organisation":
            return httpx.Response(200, json=held.organisation.model_dump())
        if path == "/members":
            query = (seen.query.get("query") or [""])[0].casefold()
            limit = min(int((seen.query.get("limit") or ["100"])[0]), 500)
            former = (seen.query.get("include_former") or ["false"])[0] == "true"
            people = sorted(
                (
                    m
                    for m in held.members.values()
                    if (former or m.is_active)
                    and (
                        not query
                        or m.email.casefold().startswith(query)
                        or m.name.casefold().startswith(query)
                    )
                ),
                key=lambda m: m.email,
            )
            return httpx.Response(200, json={"people": [m.model_dump() for m in people[:limit]]})
        if path == "/people/resolve":
            found: dict[str, Member] = {}
            for identity in seen.json_body().get("identities", []):
                member = self._member(org_id, str(identity))
                if member is not None:
                    found[member.user_id] = member
            return httpx.Response(200, json={"people": [m.model_dump() for m in found.values()]})
        if path.startswith("/people/"):
            member = held.members.get(path.removeprefix("/people/"))
            if member is None:
                return httpx.Response(404, json={"detail": "Not a member of this organisation"})
            return httpx.Response(200, json=member.model_dump())
        return httpx.Response(404, json={"detail": "Not Found"})

    # ── Notifications ───────────────────────────────────────────────

    def _notify(self, seen: SeenRequest, client_id: str, org_id: str) -> httpx.Response:
        try:
            batch = NotificationBatch.model_validate(seen.json_body())
        except (ValidationError, ValueError) as exc:
            return httpx.Response(422, json={"detail": str(exc)})
        if len(batch.notifications) > Notifier.MAX_BATCH:
            return httpx.Response(413, json={"detail": "too many notifications in one call"})
        if any(len(n.recipients) > Notifier.MAX_RECIPIENTS for n in batch.notifications):
            return httpx.Response(413, json={"detail": "too many recipients in one notification"})
        held = self.extensions.get(client_id)
        source = held.extension_id if held else ""
        for notification in batch.notifications:
            if held and notification.target and notification.target.page not in held.pages:
                return httpx.Response(
                    400,
                    json={"detail": f"{notification.target.page} is not one of {source}'s pages"},
                )
        self.notify_calls.append(
            NotifyCall(org_id=org_id, client_id=client_id, notifications=batch.notifications)
        )
        delivered = 0
        for notification in batch.notifications:
            actor = self._member(org_id, notification.actor) if notification.actor else None
            for recipient in notification.recipients:
                member = self._member(org_id, recipient)
                if member is None or not member.is_active:
                    continue
                if actor is not None and actor.user_id == member.user_id:
                    continue
                if notification.event_key:
                    once = (member.user_id, source, notification.event_key)
                    if once in self._told:
                        continue
                    self._told.add(once)
                self.deliveries.append(
                    Delivery(
                        org_id=org_id,
                        user_id=member.user_id,
                        source=source,
                        notification=notification,
                    )
                )
                delivered += 1
        return httpx.Response(200, json={"delivered": delivered})

    @property
    def events(self) -> list[SentEvent]:
        """Every item a service sent (`host.send_event`, `host.agent(...)`), as igni stored it."""
        return self.agents.items


#: What a service's tests import. `BundleArchive` builds a stand-in
#: bundle so the fake knows an extension's pages; `IgniPaths` names the
#: routes `FakeIgni.failing` can be told to fail.
__all__ = [
    "BundleArchive",
    "Delivery",
    "FakeAgents",
    "FakeIgni",
    "IgniPaths",
    "ManualClock",
    "NotifyCall",
    "SentEvent",
]
