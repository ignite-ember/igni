"""Who a call into an extension is for: its principal.

``docs/EXTENSION_ROUTING_AND_CONCURRENCY.md`` §3 and §6. Every request
the host sends an extension's process — a tool call, a page's call, a
hook — names the producer it is for, in the MCP request's ``_meta``
under ``igni/principal`` (a hook carries it on the frame). The
extension serves that caller and keeps callers apart; it never decides
who may call: the floor only forwards what the producer may make.

This backend serves one person, so the principal is always the one
signed in on this machine (:class:`SignedInPerson`). Apps — the other
kind of producer — and a backend serving several people are not built
yet; the shape already names ``kind`` so neither changes the wire.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel

from ember_code.core.auth.credentials import CloudCredentials
from ember_code.core.db.scope import Tenant
from ember_code.extensions.store import ExtensionStore


class PrincipalKind(StrEnum):
    PERSON = "person"
    APP = "app"
    #: One end user of an app, a producer of its own (igni server's service-identity §17).
    SPACE = "space"


class Principal(BaseModel):
    """The producer a call is for."""

    #: Where it travels: the MCP request's ``_meta`` key, and the hook frame's field.
    META_KEY: ClassVar[str] = "igni/principal"
    #: The header a process names the caller with when it asks for a service token.
    TOKEN_HEADER: ClassVar[str] = "X-Igni-Principal"

    kind: PrincipalKind
    #: The person's or the app's id on igni server.
    id: str
    #: Their organisation.
    org: str | None = None
    #: The one group a person belongs to in ``org``; an app has none.
    group: str | None = None
    #: The igni session the call comes from, when there is one.
    session: str | None = None
    #: A space's app (its id); ``None`` for a person or an app.
    app: str | None = None
    #: The app's own name for a space's end user.
    external_id: str | None = None

    @property
    def ref(self) -> str:
        """``person:<id>`` — how the token endpoint is told whom a token is for."""
        return f"{self.kind.value}:{self.id}"

    #: Carried only when set: a space's, and nobody else's.
    _SPACE_FIELDS: ClassVar[frozenset[str]] = frozenset({"app", "external_id"})

    def as_meta(self) -> dict[str, Any]:
        unset = {name for name in self._SPACE_FIELDS if getattr(self, name) is None}
        return {self.META_KEY: self.model_dump(mode="json", exclude=unset)}


class SignedInPerson:
    """The principal on a single-producer backend: whoever is signed in here.

    A person on the desktop; in an app's or a space's VM, the app or the
    space its token names (``kind``).

    Read fresh on every call, as the session is: signing in and out
    rewrites the credentials, and a principal remembered from before
    would answer for somebody who left. Their group comes from the last
    extension listing — igni server reads it at the time it lists — and
    the session token's snapshot only before any listing arrived.
    """

    def __init__(self, credentials_file: str | None, store: ExtensionStore) -> None:
        self._credentials_file = credentials_file
        self._store = store

    def __call__(self) -> Principal | None:
        credentials = CloudCredentials(self._credentials_file)
        user_id = credentials.user_id
        if not credentials.is_authenticated or not user_id:
            return None
        claims = credentials.claims
        kind = self._kind_of(claims.kind if claims is not None else None)
        if kind is not PrincipalKind.PERSON:
            # An app's or a space's own token (a VM it runs in): no group.
            return Principal(
                kind=kind,
                id=user_id,
                org=credentials.org_id,
                app=claims.app if claims is not None else None,
                external_id=claims.external_id if claims is not None else None,
            )
        listed = self._store.group()
        return Principal(
            kind=PrincipalKind.PERSON,
            id=user_id,
            org=credentials.org_id,
            group=listed.group if listed is not None else credentials.group,
        )

    @staticmethod
    def _kind_of(claimed: str | None) -> PrincipalKind:
        """What the token says it is; a session that says nothing is a person's."""
        try:
            return PrincipalKind(claimed) if claimed else PrincipalKind.PERSON
        except ValueError:
            return PrincipalKind.PERSON

    def tenant(self) -> Tenant | None:
        """Whose rows a shared database holds for this backend (``docs/CLOUD.md`` §9).

        The org is the tenant and the producer is the principal's
        ``ref``. ``None`` when nobody is signed in, or the sign-in names
        no org: rows with no tenant would be nobody's to keep apart.
        """
        principal = self()
        if principal is None or not principal.org:
            return None
        return Tenant(org=principal.org, producer=principal.ref)

    def meta(self) -> dict[str, Any] | None:
        """The ``_meta`` every request to an extension carries, or ``None`` when nobody is signed in."""
        principal = self()
        return principal.as_meta() if principal is not None else None
