"""Debug-level dump of an Agno team's last-run message list.

Extracted from :mod:`ember_code.core.session.core` — the
module-level ``_log_run_messages_debug`` free function graduates
to :class:`RunMessagesDebugDumper`. Session composes an instance
on demand (``RunMessagesDebugDumper(self.main_team).dump()``);
the class shape lets the dumper be unit-tested in isolation.

Used for diagnosing tool-result delivery issues — surfaces
``role`` / ``tool_call_id`` / ``tool_calls`` / compression state
/ ``from_history`` flag on every message plus a 200-char preview
of ``content``. Silent on any exception so an introspection
hiccup can't break the response path.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class RunMessagesDebugDumper:
    """Dump the messages from a team's last :attr:`run_response`
    at DEBUG level. Constructor takes the team object; call
    :meth:`dump` to emit the walk.
    """

    _PREVIEW_CAP = 200

    def __init__(self, team: Any) -> None:
        self._team = team

    def dump(self) -> None:
        """Emit the DEBUG walk. Silent on any exception."""
        try:
            self._emit()
        except Exception as exc:  # noqa: BLE001 — diagnostic path
            logger.debug("RUN_MESSAGES: error: %s", exc)

    @classmethod
    def dump_team(cls, team: Any) -> None:
        """Convenience classmethod: dump the messages of ``team``.

        New call sites should prefer this over the two-line
        ``RunMessagesDebugDumper(team).dump()`` idiom. Tests should
        patch this method (``patch(
        'ember_code.core.session.run_debug.RunMessagesDebugDumper.dump_team'
        )``) instead of the legacy module-level shim in ``core.py``.
        """
        cls(team).dump()

    def _emit(self) -> None:
        rr = getattr(self._team, "run_response", None)
        if rr is None:
            logger.debug("RUN_MESSAGES: no run_response")
            return
        messages = getattr(rr, "messages", None)
        if not messages:
            logger.debug("RUN_MESSAGES: no messages in run_response")
            return
        logger.debug("RUN_MESSAGES: %d messages total", len(messages))
        for i, msg in enumerate(messages):
            role = getattr(msg, "role", "?")
            content = getattr(msg, "content", None)
            tool_calls = getattr(msg, "tool_calls", None)
            tool_call_id = getattr(msg, "tool_call_id", None)
            compressed = getattr(msg, "compressed_content", None)
            from_hist = getattr(msg, "from_history", False)

            content_str = str(content) if content is not None else "<None>"
            preview = content_str[: self._PREVIEW_CAP]
            if len(content_str) > self._PREVIEW_CAP:
                preview += f"... ({len(content_str)} total)"

            extras: list[str] = []
            if tool_call_id:
                extras.append(f"tcid={tool_call_id}")
            if tool_calls:
                names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
                extras.append(f"calls={names}")
            if compressed is not None:
                extras.append(f"COMPRESSED({len(str(compressed))}ch)")
            if from_hist:
                extras.append("HIST")

            logger.debug(
                "  MSG[%d] role=%-9s %s | %s",
                i,
                role,
                " ".join(extras),
                preview,
            )
