"""Agent tools for plugin-declared background monitors.

Two read paths (``monitor_status``, ``monitor_output``) and two
control paths (``monitor_restart``, ``monitor_stop``). The agent
cannot START a monitor that isn't declared — monitors are
plugin-owned, not agent-spawnable, by design.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from agno.tools import Toolkit

if TYPE_CHECKING:
    from ember_code.core.monitors import MonitorManager

logger = logging.getLogger(__name__)


class MonitorTools(Toolkit):
    """Inspect / restart / stop plugin-declared monitors."""

    def __init__(self, manager: MonitorManager) -> None:
        super().__init__(name="ember_monitors")
        self._manager = manager
        self.register(self.monitor_status)
        self.register(self.monitor_output)
        self.register(self.monitor_restart)
        self.register(self.monitor_stop)

    def monitor_status(self) -> str:
        """Return a JSON list of every configured monitor with
        status, pid, uptime, exit code, restart policy, and
        crash count. Empty payload means no monitors are
        configured for this session."""
        snap = self._manager.snapshot_all()
        if not snap:
            return "No monitors configured for this session."
        return json.dumps([s.model_dump(mode="json") for s in snap], indent=2)

    def monitor_output(self, name: str, lines: int = 40) -> str:
        """Return the last ``lines`` lines of merged
        stdout+stderr for ``name``. The buffer is capped at 1000
        lines per monitor — older output is dropped. Use this to
        check what a watcher is reporting without grabbing the
        whole log."""
        if not self._manager.is_configured(name):
            return f"Error: monitor not configured: {name!r}"
        # Defensive normalisation — the model sometimes passes
        # ``"40"`` as a string for int params.
        try:
            lines_int = int(lines)
        except (TypeError, ValueError):
            return "Error: lines must be an integer"
        tail = self._manager.output_tail(name, lines=lines_int)
        if not tail:
            return f"(no output yet for {name})"
        return "\n".join(tail)

    async def monitor_restart(self, name: str) -> str:
        """Restart the named monitor — clears its crash counter
        and re-launches even a ``failed`` one. Use after fixing
        whatever was causing it to crash repeatedly.
        """
        try:
            result = await self._manager.restart(name)
        except Exception as exc:
            logger.warning("monitor_restart %s raised: %s", name, exc)
            return f"Error: {exc}"
        # ``MonitorControlResult.__str__`` returns ``.reason``,
        # preserving the wire string the Agno toolkit expects.
        # ``str(...)`` also handles the legacy path where the
        # manager is mocked to return a bare string.
        return str(result)

    async def monitor_stop(self, name: str) -> str:
        """Stop the named monitor. It stays stopped until
        ``monitor_restart`` is called — the supervisor doesn't
        auto-relaunch a deliberately-stopped monitor."""
        try:
            result = await self._manager.stop(name)
        except Exception as exc:
            logger.warning("monitor_stop %s raised: %s", name, exc)
            return f"Error: {exc}"
        return str(result)
