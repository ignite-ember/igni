"""Message sanitizer — converts multimodal content arrays to plain
text for non-vision models.

Extracted from the free ``_sanitize_messages`` function in the old
``models.py`` so the behavior lives on a real collaborator that the
``LoggingModel`` holds by composition instead of a state-mutating
free function. Same fix pattern as the audit's Rule 1 remediation
(free functions taking a state object as first arg → methods on a
class).
"""

from __future__ import annotations

from typing import Any


class MessageSanitizer:
    """Rewrites multimodal message content into plain text.

    When a non-vision model receives messages from a session that
    previously used a vision model, ``content`` may be a list of
    dicts (text + image_url + file). Extracts only the ``text``
    parts, joined with newlines. Vision-capable models see the
    original content untouched.

    ``sanitize_if_needed`` takes the four-tuple ``args`` that Agno
    passes into ``invoke`` / ``ainvoke`` / ``invoke_stream`` /
    ``ainvoke_stream`` and returns a new ``args`` tuple — hiding
    the "sanitize only when non-vision and messages present"
    branching from every one of the four invoke sites.
    """

    def sanitize_if_needed(
        self,
        args: tuple[Any, ...],
        *,
        vision: bool,
    ) -> tuple[Any, ...]:
        """Return a possibly-rewritten ``args`` tuple.

        No-op fast paths:
        * ``vision=True`` — pass through so image content survives.
        * empty ``args`` — nothing to sanitize.

        The message list is rewritten in place (matches the old
        behavior; callers pass in a fresh list per call so no
        cross-call aliasing risk).
        """
        if vision or not args:
            return args
        sanitized = self._sanitize_messages(args[0])
        return (sanitized, *args[1:])

    def _sanitize_messages(self, messages: list[Any]) -> list[Any]:
        """Rewrite multimodal content lists to their text-only
        concatenation, in place on each message.
        """
        for msg in messages:
            content = self._read_content(msg)
            if not isinstance(content, list):
                continue
            self._write_content(msg, self._flatten_to_text(content))
        return messages

    @staticmethod
    def _read_content(msg: Any) -> Any:
        if isinstance(msg, dict):
            return msg.get("content")
        return getattr(msg, "content", None)

    @staticmethod
    def _write_content(msg: Any, new_content: str) -> None:
        if isinstance(msg, dict):
            msg["content"] = new_content
        else:
            msg.content = new_content

    @staticmethod
    def _flatten_to_text(content: list[Any]) -> str:
        text_parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
            elif isinstance(part, str):
                text_parts.append(part)
        return "\n".join(text_parts) if text_parts else ""
