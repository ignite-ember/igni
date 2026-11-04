"""Which process group is each extension's, so a hung one can be stopped.

igni starts an extension's process as an MCP server over stdio, in a
session of its own (the MCP SDK's ``start_new_session``), so the
process group is the extension and everything it started. The MCP
client keeps that process to itself, and dropping the client does not
end it (``MCPClientManager.disconnect_one``): a hung extension restarted
by reconnecting would leave the old one running until igni exits.

So the process says which group it is, when it connects to the host
channel (``X-Igni-Process-Group``), and the host believes it only when
that group's leader is igni's own child — a claim about some other
process is refused, and nothing outside igni's children can be stopped
from here. :meth:`ExtensionProcesses.kill` ends the group. POSIX only:
on Windows the claim is ignored, and a restart reconnects without
ending the old process.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from typing import ClassVar

logger = logging.getLogger(__name__)

#: The header a process names its group in.
PROCESS_GROUP_HEADER = "X-Igni-Process-Group"


class ExtensionProcesses:
    """Each extension's process group, as its process claimed and igni checked."""

    _shared: ClassVar[ExtensionProcesses | None] = None

    def __init__(self) -> None:
        self._groups: dict[str, int] = {}

    @classmethod
    def shared(cls) -> ExtensionProcesses:
        if cls._shared is None:
            cls._shared = cls()
        return cls._shared

    @classmethod
    def reset_shared(cls) -> None:
        cls._shared = None

    def group_of(self, extension_id: str) -> int | None:
        return self._groups.get(extension_id)

    async def claim(self, extension_id: str, raw: str) -> bool:
        """Record the group a process says it is in, if igni started its leader."""
        if sys.platform == "win32":
            return False
        try:
            group = int(raw)
        except ValueError:
            return False
        if group <= 1 or not await self._is_our_child(group):
            logger.warning(
                "%s claims process group %s, whose leader igni did not start; ignored",
                extension_id,
                raw,
            )
            return False
        self._groups[extension_id] = group
        return True

    def forget(self, extension_id: str) -> None:
        self._groups.pop(extension_id, None)

    def kill(self, extension_id: str) -> bool:
        """End the extension's process group. ``True`` when there was one to end."""
        group = self._groups.pop(extension_id, None)
        if group is None:
            return False
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(group, signal.SIGKILL)
            logger.info("ended %s's process group %d", extension_id, group)
            return True
        return False

    @staticmethod
    async def _is_our_child(pid: int) -> bool:
        """Whether ``pid``'s parent is this process."""
        try:
            ps = await asyncio.create_subprocess_exec(
                "ps",
                "-o",
                "ppid=",
                "-p",
                str(pid),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(ps.communicate(), 5)
        except (OSError, TimeoutError):
            return False
        try:
            return int(out.strip() or b"0") == os.getpid()
        except ValueError:
            return False


__all__ = ["PROCESS_GROUP_HEADER", "ExtensionProcesses"]
