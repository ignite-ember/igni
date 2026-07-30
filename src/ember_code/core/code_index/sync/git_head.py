"""Local-git HEAD lookup as a small value object.

Extracted out of :class:`CodeIndexSyncManager` so the coordinator
doesn't own the ``subprocess.run`` call directly — the sync
manager delegates to :meth:`GitHead.current_sha`, and the HEAD
watcher receives this callable via composition rather than
reaching back to the manager.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitHead:
    """Return the project's current ``HEAD`` SHA via ``git rev-parse``.

    A stateless value object over a project directory. Wraps
    :func:`subprocess.run` with the same 5-second timeout the
    manager used inline; returns ``None`` (never raises) when
    the project isn't a git repo or the git binary isn't on
    ``PATH``.
    """

    _TIMEOUT_SECONDS = 5.0

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir

    def current_sha(self) -> str | None:
        """Return the project's current ``HEAD`` SHA, or ``None``.

        Sync — the ``asyncio.to_thread`` wrap belongs to the caller
        (both :class:`CodeIndexSyncManager` and :class:`HeadWatcher`
        offload this call so the event loop doesn't stall while
        ``git rev-parse`` runs).
        """
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.project_dir,
                capture_output=True,
                text=True,
                timeout=self._TIMEOUT_SECONDS,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        sha = result.stdout.strip()
        return sha or None


__all__ = ["GitHead"]
