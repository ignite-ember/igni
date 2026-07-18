"""Hooks panel controller.

Owns the hooks-panel concern: snapshot the session's ``hooks_map``
as typed :class:`HookEntryView` records, plus the reload trigger
button. The reach-into-hook-fields defensive ``getattr`` block
lives in :meth:`HookEntryView.from_hook` — this controller just
iterates and projects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ember_code.backend.schemas_panels import HookEntryView
from ember_code.protocol import messages as msg

if TYPE_CHECKING:
    from ember_code.core.session import Session


class HooksPanelController:
    """Snapshot + reload trigger for the hooks panel."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def snapshot(self) -> list[HookEntryView]:
        """Snapshot of every active hook for the hooks panel."""
        out: list[HookEntryView] = []
        for event, hooks in self._session.hooks_map.items():
            for hook in hooks:
                out.append(HookEntryView.from_hook(hook, event))
        return out

    def reload(self) -> msg.Info:
        """Reload hooks from disk. Returns count for the panel toast."""
        count = self._session.reload_hooks()
        return msg.Info(text=f"Reloaded hooks — {count} active hook(s) across all events.")
