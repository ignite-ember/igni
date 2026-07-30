"""Response text extraction.

Normalizes an Agno response object (``RunOutput``, ``RunResponse``,
plain ``str``, or any object with a ``.content`` / ``.messages``
attribute) into the assistant-visible text. Falls back to
``str(response)`` when no text field is present.

The public surface is intentionally two-tier:

* :class:`ResponseTextExtractor` — the OOP entry point. Wraps the
  response and exposes :meth:`text`. Prefer this in new code.
* :func:`extract_response_text` — a one-line delegator kept for
  back-compat. It is ALSO the live monkeypatch surface used by
  ``patch("ember_code.core.session.core.extract_response_text", ...)``
  in the test suite; do not inline or rename it (see
  ``session/core.py`` for the ``# noqa: F401 — test-patch target``
  companion import).
"""

from __future__ import annotations

from typing import Any, Protocol

# Sentinel distinguishing "attribute missing" from "attribute is None".
# Load-bearing for the ``.content = None`` case, which must still
# dispatch through the content branch (returning ``"None"``) rather
# than falling through to ``.messages`` / ``str(response)``.
_MISSING: Any = object()


class AgnoResponseLike(Protocol):
    """Structural type for Agno-style response objects.

    Names the two attributes :class:`ResponseTextExtractor` probes.
    Both attributes are declared ``Any`` because Agno's real types
    (``RunOutput.content: str | BaseModel | ...``, ``messages: list[Message]``)
    are polymorphic — the Protocol advertises attribute *presence*,
    not shape.
    """

    content: Any
    messages: Any


class ResponseTextExtractor:
    """Extracts the assistant-visible text from an Agno response.

    Dispatch order (pinned by ``tests/test_response.py``):

    1. ``str`` input → return verbatim.
    2. ``.content`` present → stringify (even if ``None``).
    3. ``.messages`` present → walk in reverse and return
       ``str(msg.content)`` for the last message whose ``content``
       is truthy.
    4. Fallback → ``str(response)``.
    """

    def __init__(self, response: str | AgnoResponseLike) -> None:
        self._response = response

    def text(self) -> str:
        response = self._response
        if isinstance(response, str):
            return response

        content = getattr(response, "content", _MISSING)
        if content is not _MISSING:
            return str(content)

        messages = getattr(response, "messages", _MISSING)
        if messages is not _MISSING:
            extracted = self._from_messages(messages)
            if extracted is not None:
                return extracted

        return str(response)

    @staticmethod
    def _from_messages(messages: Any) -> str | None:
        for msg in reversed(messages):
            candidate = getattr(msg, "content", None)
            if candidate:
                return str(candidate)
        return None


def extract_response_text(response: str | AgnoResponseLike) -> str:
    """Return the assistant-visible text for an Agno response.

    Falls back to ``str(response)`` when no text field is present.
    Thin delegator over :class:`ResponseTextExtractor` — kept as a
    module-level symbol because the test suite patches this name
    directly via ``patch("ember_code.core.session.core.extract_response_text")``.
    """
    return ResponseTextExtractor(response).text()
