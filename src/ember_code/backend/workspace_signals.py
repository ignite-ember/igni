"""The project's git state, as host-model signals.

``host.workspace.head_changed`` and ``host.workspace.files_changed``
(``docs/HOST_OBJECT_MODEL.md``) are the two signals nothing in igni
already broadcasts: igni does not watch git for itself. This owns the
poll that produces them and hands what it sees to the host object
model, which filters each delivery to what the extension may see —
nothing without ``permissions.workspace``, and a changed file outside
``workspacePaths`` is not in the list.

The poll runs only while someone entitled is subscribed: the model's
signal bus answers "is anyone listening", so an igni with no extension
interested in the workspace does no git work at all.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ember_code.backend.workspace_watch import GitWorkspace, Head, WorkspaceWatcher
from ember_code.extensions.events import HostTopic

logger = logging.getLogger(__name__)


class WorkspaceSignals:
    """Watch git while somebody listens, and tell the host model."""

    #: How many paths one ``workspace.files_changed`` carries. A branch
    #: switch can dirty tens of thousands, and an extension handed all of
    #: them is being handed a directory listing rather than a signal.
    #: Past the cap it is told the list is partial, which is its cue to
    #: rescan what it cares about instead of trusting the paths.
    MAX_PATHS = 500

    def __init__(self, *, project_dir: Path) -> None:
        #: The :class:`~ember_code.extensions.host.model.HostModel`,
        #: once it is built.
        self._model: Any = None
        self._watcher = WorkspaceWatcher(
            workspace=GitWorkspace(project_dir),
            on_head=self.head_changed,
            on_files=self.files_changed,
            wants=self._wanted,
        )

    def attach_model(self, model: Any) -> None:
        """The model to tell, and whose subscribers decide whether to poll."""
        self._model = model

    async def start(self) -> None:
        """Start the workspace poll."""
        await self._watcher.start()

    async def stop(self) -> None:
        await self._watcher.stop()

    async def poll_once(self) -> None:
        """One tick of the poll, for tests."""
        await self._watcher.poll_once()

    def _wanted(self, topic: HostTopic) -> bool:
        """Is anyone entitled subscribed to this signal?"""
        return self._model is not None and bool(self._model.bus.wanted(str(topic)))

    def head_changed(self, head: Head) -> None:
        if self._model is not None:
            self._model.head_changed(head)

    def files_changed(self, paths: frozenset[str]) -> None:
        if self._model is not None:
            self._model.files_changed(paths, self.MAX_PATHS)


__all__ = ["WorkspaceSignals"]
