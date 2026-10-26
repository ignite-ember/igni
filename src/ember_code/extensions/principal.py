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

    @property
    def ref(self) -> str:
        """``person:<id>`` — how the token endpoint is told whom a token is for."""
        return f"{self.kind.value}:{self.id}"

    def as_meta(self) -> dict[str, Any]:
        return {self.META_KEY: self.model_dump(mode="json")}


class SignedInPerson:
    """The principal on a single-person backend: whoever is signed in here.

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
        listed = self._store.group()
        return Principal(
            kind=PrincipalKind.PERSON,
            id=user_id,
            org=credentials.org_id,
            group=listed.group if listed is not None else credentials.group,
        )

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
