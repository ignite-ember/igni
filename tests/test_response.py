"""Tests for utils/response.py — response text extraction.

The function handles four shapes (in priority order):
  1. ``str`` → pass-through
  2. ``.content`` attribute → use that (stringify if not str)
  3. ``.messages`` list → walk reverse, return first non-empty content
  4. fallback → ``str(response)``

The original tests only checked ``isinstance(result, str)`` —
that catches a crash but not value drift. This file pins the
actual extracted strings.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from ember_code.core.utils.response import extract_response_text


class TestExtractResponseText:
    def test_string_response_returned_as_is(self):
        # Plain string input shouldn't be stringified through
        # any wrapper — just return it verbatim. The
        # ``isinstance(str)`` early-return is what makes this
        # work; pin it.
        assert extract_response_text("plain string") == "plain string"

    def test_empty_string_passes_through(self):
        assert extract_response_text("") == ""

    def test_extracts_content_string(self):
        # Most-common path — Agno RunResponse with a string
        # content attribute.
        response = MagicMock()
        response.content = "Hello, world!"
        assert extract_response_text(response) == "Hello, world!"

    def test_non_string_content_stringified(self):
        # If ``.content`` is a structured value (dict, list,
        # custom), the function falls through to ``str(...)``.
        # Pin so a future ``if isinstance(content, list): ...``
        # branch is a deliberate addition rather than silent.
        response = MagicMock()
        response.content = {"some": "dict"}
        result = extract_response_text(response)
        # Stringified form — order may vary, just pin a key
        # appears.
        assert "some" in result and "dict" in result

    def test_none_content_returns_string_None(self):
        # ``.content = None`` → falls to ``str(None)`` = "None".
        # Documented oddity — better than crashing, but pin so
        # a future "return empty string instead" change is
        # deliberate.
        response = MagicMock()
        response.content = None
        # The ``hasattr(response, "content")`` branch fires;
        # ``content`` is None → ``isinstance(None, str)`` is
        # False → falls to ``str(None)`` = "None".
        assert extract_response_text(response) == "None"

    def test_messages_fallback_returns_last_non_empty(self):
        # When no ``.content`` but ``.messages`` exists, walk
        # reversed and return the first message that has
        # truthy content. Mirrors how Agno aggregates a multi-
        # turn run.
        response = SimpleNamespace(
            messages=[
                SimpleNamespace(content="early"),
                SimpleNamespace(content="middle"),
                SimpleNamespace(content="last"),
            ],
        )
        assert extract_response_text(response) == "last"

    def test_messages_walks_reverse_skipping_empty(self):
        # Empty / falsy content on the LAST message → keep
        # walking backward until a non-empty one is found.
        # Load-bearing for runs where the final message is a
        # tool-call stub with no text.
        response = SimpleNamespace(
            messages=[
                SimpleNamespace(content="first"),
                SimpleNamespace(content="second"),
                SimpleNamespace(content=""),  # empty
                SimpleNamespace(content=None),  # falsy
            ],
        )
        assert extract_response_text(response) == "second"

    def test_messages_all_empty_falls_through_to_str(self):
        # No truthy content in any message → the for loop
        # exits without returning → ``str(response)`` fallback.
        # SimpleNamespace's __repr__ contains "namespace(..." —
        # not user-facing but pinned for the fallback path.
        response = SimpleNamespace(
            messages=[
                SimpleNamespace(content=""),
                SimpleNamespace(content=None),
            ],
        )
        result = extract_response_text(response)
        # The fallback is ``str(response)`` — pin that it
        # returns SOMETHING (not crash, not empty).
        assert isinstance(result, str)
        assert len(result) > 0

    def test_messages_without_content_attr_skipped(self):
        # A message object without a ``content`` attribute
        # is skipped by ``hasattr(msg, "content")``. The walk
        # continues to the next message.
        response = SimpleNamespace(
            messages=[
                SimpleNamespace(content="kept"),
                SimpleNamespace(),  # no content attr
            ],
        )
        # The no-attr message is at the END (most-recent) —
        # the walk steps over it and returns "kept".
        assert extract_response_text(response) == "kept"

    def test_no_content_no_messages_falls_through(self):
        # Neither attribute → ``str(response)``. Defensive
        # last resort; the agent at least sees the repr.
        response = SimpleNamespace()
        result = extract_response_text(response)
        assert isinstance(result, str)
        assert "namespace" in result.lower()

    def test_string_has_priority_over_content(self):
        # If the caller passes ``str("Hello")``, even if it
        # had a ``content`` attribute somehow (subclass?), the
        # isinstance check fires first. Pin the ordering.
        # ``str`` doesn't normally have ``content`` so this
        # is more about the dispatch order than the value.
        assert extract_response_text("string wins") == "string wins"

    def test_content_has_priority_over_messages(self):
        # Object with BOTH ``.content`` and ``.messages`` —
        # content wins (it's the dispatch-order pinning).
        # Drift here would make multi-message runs ignore
        # their final aggregated content.
        response = SimpleNamespace(
            content="content-wins",
            messages=[SimpleNamespace(content="messages-loses")],
        )
        assert extract_response_text(response) == "content-wins"
