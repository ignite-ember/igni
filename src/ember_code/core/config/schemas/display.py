"""``DisplayConfig`` — the ``display`` block of ``Settings``.

Solo file because display is a natural growth surface — color
themes, per-panel toggles, preview lines — and callers will
inevitably want to extend it independently of the other schemas.
"""

from __future__ import annotations

from pydantic import BaseModel


class DisplayConfig(BaseModel):
    markdown: bool = True
    show_tool_calls: bool = True
    show_routing: bool = False
    show_reasoning: bool = False
    color_theme: str = "auto"
    tool_result_preview_lines: int = 4
    message_truncate_lines: int = 10

    def toggle_show_routing(self) -> bool:
        """Flip :attr:`show_routing` and return the new value.

        Read-mutate-return that belongs on the settings type rather
        than on ``BackendServer``. Callers use the returned bool to
        surface the new state to the user without a re-read.
        """
        self.show_routing = not self.show_routing
        return self.show_routing
