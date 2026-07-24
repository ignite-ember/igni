"""Assistant-content parsing for session-restore chat rebuilds.

Owns the two content-shaping helpers formerly split across
``server_helpers._split_assistant_content_for_restore`` and
``server_helpers._format_tool_args_for_restore``, plus the
``<think>...</think>`` regex ``_THINK_BLOCK_RE`` that used to live at
module scope.

:class:`AssistantContentRestorer` groups them under their shared
subject (rebuilding a persisted assistant turn into the same
``(thinking, text, thinking, text, ...)`` layout the user saw live).
Both methods are stateless so they're exposed as ``@staticmethod`` —
the class exists to give the regex a proper owner and to make the
formatter's per-value cap a class attribute rather than an inline
magic number.
"""

from __future__ import annotations

import json
import re
from typing import Any


class AssistantContentRestorer:
    """Parse persisted assistant content back into structured segments.

    * :meth:`split_content` — split an assistant message into
      ``(role, text)`` pairs so inline ``<think>...</think>`` blocks
      restore as thinking cards.
    * :meth:`format_tool_args` — one-line ``key=value`` summary for
      restored tool cards, matching the live tool-card args preview.
    """

    # Inline ``<think>...</think>`` block — many models emit reasoning
    # in the assistant content with these tags instead of Agno's
    # ``reasoning_content`` field. The trailing ``|$`` allows a final
    # unclosed block (cancelled run) to be captured up to end-of-content.
    _THINK_BLOCK_RE = re.compile(r"<think>([\s\S]*?)(?:</think>|$)")

    # Per-value cap on the ``key=value`` summary — longer values get
    # elided with ``...`` so a single giant string can't drown out the
    # other args in the tool-card preview.
    _MAX_ARG_VALUE_LEN = 80

    @staticmethod
    def split_content(content: str) -> list[tuple[str, str]]:
        """Split an assistant message's content into interleaved
        ``(role, text)`` segments, where ``role`` is ``"thinking"`` for
        ``<think>...</think>`` blocks and ``"assistant"`` for everything
        else. Preserves order so the rebuilt chat reads the same as the
        live stream.

        Returns ``[]`` when content has only whitespace / empty think
        blocks (degenerate runs); the caller should emit nothing then.
        """
        if "<think>" not in content:
            stripped = content.strip()
            return [("assistant", stripped)] if stripped else []
        parts: list[tuple[str, str]] = []
        cursor = 0
        for match in AssistantContentRestorer._THINK_BLOCK_RE.finditer(content):
            before = content[cursor : match.start()].strip()
            if before:
                parts.append(("assistant", before))
            thinking = match.group(1).strip()
            if thinking:
                parts.append(("thinking", thinking))
            cursor = match.end()
        trailing = content[cursor:].strip()
        if trailing:
            parts.append(("assistant", trailing))
        return parts

    @staticmethod
    def format_tool_args(args: Any) -> str:
        """One-line argument summary for restored tool cards.

        Matches the live ``args_summary`` shape: ``key=value`` pairs
        joined by spaces, with long values truncated. Strings are shown
        raw (not JSON-quoted) so a shell ``command="ls -la"`` reads
        like a command, not like JSON.

        Handles the four shapes the walker sees in practice: dict
        (structured args), list (positional arg-list), scalars, and
        ``None`` / other. ``Any`` remains untyped here pending a proper
        discriminated ``RestoreToolArgs`` union.
        """
        cap = AssistantContentRestorer._MAX_ARG_VALUE_LEN
        if isinstance(args, dict):
            parts: list[str] = []
            for k, v in args.items():
                if isinstance(v, str):
                    v_str = v if len(v) <= cap else v[: cap - 3] + "..."
                elif isinstance(v, (int, float, bool)) or v is None:
                    v_str = str(v)
                else:
                    try:
                        v_str = json.dumps(v, separators=(",", ":"))
                    except Exception:
                        v_str = str(v)
                    if len(v_str) > cap:
                        v_str = v_str[: cap - 3] + "..."
                parts.append(f"{k}={v_str}")
            return " ".join(parts)
        if isinstance(args, list):
            try:
                return json.dumps(args, separators=(",", ":"))
            except Exception:
                return str(args)
        return str(args)
