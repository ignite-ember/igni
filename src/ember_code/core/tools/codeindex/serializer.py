"""JSON serialization boundary for the codeindex toolkit.

Every service (``QueryService``, ``TreeService``) returns typed Pydantic
unions (``ItemsResponse | ErrorResponse``); the toolkit facade uses a
single :class:`JsonSerializer` to render them as JSON at the agent
boundary. Keeping the serializer in one class means the formatting
(indent, ``exclude_none``, ``default=str`` fallback for stray objects)
has ONE definition, and the services don't have to accept a
``json_dumps`` callable threaded through every method signature.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel


class JsonSerializer:
    """Renders responses to JSON strings.

    Stateless — the class exists to give the formatting rules a name
    and a home. One instance per toolkit is enough; the toolkit
    constructs it once and hands it to any code that needs to serialize
    at the agent boundary.

    ``exclude_none=True`` for Pydantic models: the response schemas
    have several fields (``references``, ``refs``) that are ``None``
    on most nodes; emitting them as ``null`` would bloat every
    response and force the agent to skip past them. The two response
    shapes (:class:`ItemsResponse`, :class:`ErrorResponse`) are both
    happy with this — ``ErrorResponse`` has no optional fields, and
    ``ItemsResponse`` uses ``None`` as "omit" everywhere.
    """

    def __init__(self, *, indent: int = 2) -> None:
        self._indent = indent

    def dumps(self, data: Any) -> str:
        """Serialize ``data`` to a JSON string.

        Pydantic models render via :meth:`model_dump_json` with
        ``exclude_none=True`` so optional fields don't clutter agent
        responses; plain dicts fall back to :func:`json.dumps` with
        ``default=str`` so stray non-JSON-native values (enums, paths,
        datetimes) don't crash the toolkit at the last mile.
        """
        if isinstance(data, BaseModel):
            return data.model_dump_json(indent=self._indent, exclude_none=True)
        return json.dumps(data, indent=self._indent, default=str)
