"""Caller-context inspector — walks the Python stack, filters SDK
internals, formats an ember_code-relative caller chain.

Replaces the dead free ``_caller_context`` module function AND the
inline duplicate loop inside ``_LoggingModel._log_call`` in the old
``models.py``. Both fell into the audit's Rule 1 (a free function
whose first argument is really an object) — this class captures the
subject (the stack) and the filtering knobs as instance state.
"""

from __future__ import annotations

import inspect
import os

# Frames from these SDKs are noise — we want the ember_code call that
# originated the request, not the openai-python / httpx layer that
# forwarded it.
_SDK_FRAME_MARKERS = ("/agno/", "/openai/", "/httpx/", "/asyncio/")


class CallerContextInspector:
    """Formats a compact ember_code-frame call chain for logging."""

    def __init__(self, sdk_markers: tuple[str, ...] = _SDK_FRAME_MARKERS) -> None:
        self._sdk_markers = sdk_markers

    def format_caller_chain(
        self,
        depth: int = 2,
        max_frames: int = 4,
    ) -> str:
        """Return a compact ``file:lineno(function) <- ...`` string
        showing up to ``max_frames`` ember_code frames on the stack
        starting from ``depth``.

        SDK internals are skipped. Returns ``"unknown"`` when no
        ember_code frame is reachable — happens for tests that stub
        the model directly.
        """
        frames: list[str] = []
        for fi in inspect.stack()[depth : depth + 15]:
            module = fi.filename
            if any(marker in module for marker in self._sdk_markers):
                continue
            short = (
                module.rsplit("ember_code/", 1)[-1]
                if "ember_code/" in module
                else os.path.basename(module)
            )
            frames.append(f"{short}:{fi.lineno}({fi.function})")
            if len(frames) >= max_frames:
                break
        return " <- ".join(frames) or "unknown"
