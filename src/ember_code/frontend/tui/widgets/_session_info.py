"""Session metadata model — shared between the session picker widget
and the ``SessionManager`` orchestrator.

Kept in its own module so the schema doesn't pull the whole
``_dialogs.py`` (which imports Textual and a fair amount of UI
scaffolding) into non-UI callers. Pattern 7 — separate wire/domain
from UI.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class SessionInfo(BaseModel):
    """Lightweight session metadata for the picker UI."""

    session_id: str
    name: str = ""
    created_at: int = 0
    updated_at: int = 0
    run_count: int = 0
    summary: str = ""
    agent_name: str = ""

    @property
    def display_name(self) -> str:
        """Session name, falling back to the session_id."""
        return self.name or self.session_id

    @property
    def display_time(self) -> str:
        """Human-readable timestamp."""
        ts = self.updated_at or self.created_at
        if not ts:
            return "unknown"
        dt = datetime.fromtimestamp(ts)
        now = datetime.now()
        delta = now - dt
        if delta.days == 0:
            return dt.strftime("%H:%M")
        if delta.days == 1:
            return "yesterday"
        if delta.days < 7:
            return f"{delta.days}d ago"
        return dt.strftime("%Y-%m-%d")

    @property
    def label(self) -> str:
        """Two-part label: name line + summary line."""
        parts = [f"[bold]{self.display_name}[/bold]"]
        parts.append(f"[dim]{self.display_time}[/dim]")
        if self.run_count:
            parts.append(f"[dim]{self.run_count} runs[/dim]")
        line1 = "  ".join(parts)

        if self.summary:
            short = self.summary[:80]
            if len(self.summary) > 80:
                short += "..."
            return f"{line1}\n    [dim italic]{short}[/dim italic]"
        return line1
