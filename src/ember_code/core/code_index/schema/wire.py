"""Wire-format coercion for Weaviate responses.

Pattern 7 keeps wire shapes out of the domain-schema package, but
chroma-weaviate responses are a different wire protocol with a
sentinel date/UUID encoding that downstream code can't accept
directly. This module owns that coercion surface.

:class:`WeaviateWireCodec` is a Pydantic model with a single
:class:`coerce` method that recursively substitutes ``datetime`` /
``UUID`` leaves for ISO strings / hex strings. The codec owns the
substitution rules; raw ``dict`` / ``list`` literals never escape
the codec's :meth:`coerce` boundary — callers receive a
``JsonSafe`` tree they can hand to ``model_validate`` or
``json.dumps``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

# A tree with no non-JSON-safe values — what ``json.dumps`` accepts.
JsonSafe = "str | int | float | bool | None | dict[str, JsonSafe] | list[JsonSafe]"


class WeaviateWireCodec(BaseModel):
    """Recursive Weaviate-payload normalizer.

    Stateless — the class exists to bind the substitution rules
    (datetime → ISO string, UUID → hex string) and to expose them
    through a typed :meth:`coerce` callable instead of a free
    function. Construct once, call :meth:`coerce` per response.
    """

    model_config = {"frozen": True, "extra": "forbid"}

    def coerce(self, data: Any) -> Any:
        """Walk ``data`` and return a JSON-safe tree.

        ``dict`` and ``list`` are descended into; ``datetime`` and
        ``UUID`` are replaced with their string representations;
        every other value passes through unchanged.
        """
        if isinstance(data, datetime):
            return data.isoformat()
        if isinstance(data, UUID):
            return str(data)
        if isinstance(data, dict):
            return {k: self.coerce(v) for k, v in data.items()}
        if isinstance(data, list):
            return [self.coerce(item) for item in data]
        return data


__all__ = ["JsonSafe", "WeaviateWireCodec"]
