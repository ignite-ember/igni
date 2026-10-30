"""Who a VM is for: ``person:<id>``, ``app:<id>`` or ``space:<id>``.

``docs/CLOUD.md`` §9. The producer string is the one the token names, the
node agent writes as ``IGNI_PRODUCER`` and ``igni-cloud db grant-producer``
is given — compared exactly, never folded. A **space** is one end user of
an app, a producer of its own; the app it belongs to is its owner
(``IGNI_PRODUCER_OWNER=app:<id>``, or ``--owner`` when granting). igni
server is still settling the space's contract; this module is the one
place the kinds are known.

A producer also has **incarnations** (igni server's
``service-identity.md`` §11.3): a person's is their membership of the
org, new each time they are added; an app's or a space's is its own id.
A person removed and added again is a new incarnation, and must not see
the old one's data, which is kept for the org's retention period and
then erased. So the cloud's rows belong to an incarnation
(:class:`ProducerIncarnation`): the node agent names it as
``IGNI_PRODUCER_INCARNATION``, ``grant-producer`` is given it, and the
producer column of every row holds :attr:`ProducerIncarnation.stored`.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict


class ProducerKind(StrEnum):
    PERSON = "person"
    APP = "app"
    SPACE = "space"


class UnknownProducer(ValueError):
    """Not ``<kind>:<id>`` with a kind igni knows."""


class ProducerRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    #: The variable naming a space's app.
    OWNER_ENV: ClassVar[str] = "IGNI_PRODUCER_OWNER"

    kind: ProducerKind
    id: str
    #: A space's app (``app:<id>``); ``None`` for a person or an app.
    owner: str | None = None

    @classmethod
    def parse(cls, text: str, *, owner: str | None = None) -> ProducerRef:
        kind, sep, ident = text.partition(":")
        if not sep or not ident:
            raise UnknownProducer(f"{text!r} is not <kind>:<id>")
        try:
            known = ProducerKind(kind)
        except ValueError:
            raise UnknownProducer(
                f"{text!r}: the kind is one of {', '.join(k.value for k in ProducerKind)}"
            ) from None
        if owner:
            owned = cls.parse(owner)
            if owned.kind is not ProducerKind.APP:
                raise UnknownProducer(f"a space's owner is an app, not {owner!r}")
            if known is not ProducerKind.SPACE:
                raise UnknownProducer(f"only a space has an owner, not {text!r}")
        return cls(kind=known, id=ident, owner=owner or None)

    @property
    def ref(self) -> str:
        return f"{self.kind.value}:{self.id}"

    @property
    def unattended(self) -> bool:
        """Nobody signs it in: an app, or an app's space. Its credential is re-issued."""
        return self.kind is not ProducerKind.PERSON


class ProducerIncarnation(BaseModel):
    """One life of a producer: whose rows, in the cloud's database.

    Every igni table is keyed by org, producer and project, and its
    upserts name those columns; the core knows nothing of incarnations.
    So the incarnation goes into the producer value the rows hold
    (:attr:`stored`, ``person:<id>@<incarnation>``): every key, every
    upsert and Agno's ``user_id`` prefix is then an incarnation's, and the
    database role's policy compares the whole value. An incarnation is
    an id — no ``@`` or ``/`` — so the value splits one way only, and
    Agno's ``<org>/<producer>/<project>`` too.
    """

    model_config = ConfigDict(frozen=True)

    #: The variable naming it in ``producer.env``.
    ENV: ClassVar[str] = "IGNI_PRODUCER_INCARNATION"
    #: Between the producer and its incarnation in :attr:`stored`.
    SEPARATOR: ClassVar[str] = "@"
    _ID: ClassVar[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._:-]{1,128}")

    producer: ProducerRef
    incarnation: str

    @classmethod
    def of(cls, producer: ProducerRef, incarnation: str) -> ProducerIncarnation:
        if not cls._ID.fullmatch(incarnation):
            raise UnknownProducer(
                f"{incarnation!r} is not an incarnation: letters, digits and . _ : - only"
            )
        return cls(producer=producer, incarnation=incarnation)

    @property
    def stored(self) -> str:
        """The producer column of this incarnation's rows."""
        return f"{self.producer.ref}{self.SEPARATOR}{self.incarnation}"


__all__ = ["ProducerIncarnation", "ProducerKind", "ProducerRef", "UnknownProducer"]
