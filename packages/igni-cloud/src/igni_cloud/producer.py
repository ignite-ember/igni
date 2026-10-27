"""Who a VM is for: ``person:<id>``, ``app:<id>`` or ``space:<id>``.

``docs/CLOUD.md`` §9. The producer string is the one the token names, the
node agent writes as ``IGNI_PRODUCER`` and ``igni-cloud db grant-producer``
is given — compared exactly, never folded. A **space** is one end user of
an app, a producer of its own; the app it belongs to is its owner
(``IGNI_PRODUCER_OWNER=app:<id>``, or ``--owner`` when granting). igni
server is still settling the space's contract; this module is the one
place the kinds are known.
"""

from __future__ import annotations

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


__all__ = ["ProducerKind", "ProducerRef", "UnknownProducer"]
