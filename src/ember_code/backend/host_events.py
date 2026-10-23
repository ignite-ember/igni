"""Everything igni tells an extension, in one place.

:class:`~ember_code.extensions.events.EventRouter` answers *who may
be told*. This answers *what they are told*, and the two are not
the same question — which is the whole reason this module exists
rather than a handful of ``router.emit`` calls scattered through
the backend.

## The grant is not the content

``permissions.events`` says an extension may hear
``workspace.files-changed``. It does not say which files. An
extension granted ``workspacePaths: ["docs/**"]`` that is handed
the unfiltered list has just been told every path in the
repository, through an event, without ever having been granted the
workspace — and the grant check passed, so nothing looked wrong.

So workspace events obey both permissions at once: the topic grant
decides whether you are addressed, and
:class:`~ember_code.extensions.permissions.WorkspaceAccess` decides
what survives into the payload. An extension with
``workspace: none`` hears no workspace event at all, however the
topic was granted — a branch name is workspace information, and so
is the knowledge that a file called ``acquisition.md`` exists.

## Producers live here, not at the call site

The backend calls one method with what it knows — a session id, a
:class:`~ember_code.backend.workspace_watch.Head` — and never
builds a payload or names a topic. That keeps the vocabulary's
shape in the module that documents the vocabulary, and it means a
topic can change what it carries without a grep through the
backend for string literals.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ember_code.backend.workspace_watch import GitWorkspace, Head, WorkspaceWatcher
from ember_code.extensions.events import EventRouter, HostTopic
from ember_code.extensions.permissions import WorkspaceAccess
from ember_code.extensions.registry import InstalledExtension

logger = logging.getLogger(__name__)


class HostEvents:
    """Produce the published vocabulary, and nothing else."""

    #: How many paths one ``workspace.files-changed`` carries. A
    #: branch switch can dirty tens of thousands, and an extension
    #: that is handed all of them is being handed a directory
    #: listing rather than an event. Past the cap it is told the
    #: list is partial, which is its cue to rescan what it cares
    #: about instead of trusting the paths.
    MAX_PATHS = 500

    def __init__(self, *, router: EventRouter, project_dir: Path) -> None:
        self._router = router
        #: Last session we announced. ``attach_session`` fires on
        #: every page reload, and a reload is not a switch.
        self._session_id: str | None = None
        self._watcher = WorkspaceWatcher(
            workspace=GitWorkspace(project_dir),
            on_head=self.head_changed,
            on_files=self.files_changed,
            wants=self._wanted,
        )

    async def start(self) -> None:
        """Start the workspace poll."""
        await self._watcher.start()

    async def stop(self) -> None:
        await self._watcher.stop()

    # ── The content rule ────────────────────────────────────────

    def _wanted(self, topic: HostTopic) -> bool:
        """Is there anyone who would actually receive this?

        Both halves, so the watcher does no git work for an
        extension that granted the topic and would be filtered out
        of every payload it produced.
        """
        return any(self._sees_workspace(entry) for _, entry in self._router.audience_entries(topic))

    @staticmethod
    def _sees_workspace(entry: InstalledExtension) -> bool:
        return WorkspaceAccess(entry.manifest).granted

    # ── Producers ───────────────────────────────────────────────

    def extension_state_changed(self, extension_id: str, *, state: str, reason: str) -> None:
        """One extension's health, told to that extension.

        :meth:`EventRouter.emit_to` rather than ``emit``: this is
        one extension's health, and sending it to everyone who
        granted the topic would tell each of them about the others.
        """
        self._router.emit_to(
            extension_id,
            HostTopic.EXTENSION_STATE_CHANGED,
            {"state": state, "reason": reason},
        )

    def session_switched(self, session_id: str) -> None:
        """The user moved to a different session.

        Called from the pool's ``attach_session``, which is also
        what a client sends when it reconnects after a reload. The
        same id twice is not a switch, so the second one is
        dropped here rather than in every extension that would
        otherwise re-fetch on each refresh.
        """
        if not session_id or session_id == self._session_id:
            return
        self._session_id = session_id
        self._router.emit(HostTopic.SESSION_SWITCHED, {"session_id": session_id})

    def head_changed(self, head: Head) -> None:
        """The project's git HEAD moved.

        Only to extensions that were granted the workspace: a
        branch name is something the repository knows about itself,
        and an extension that was never told where the project is
        should not learn what it is called.
        """

        def payload(entry: InstalledExtension) -> dict[str, object] | None:
            if not self._sees_workspace(entry):
                return None
            # A fresh dict per recipient. Nothing mutates a payload
            # today, and one shared object handed to several frames
            # is the kind of thing that stays harmless right up
            # until something does.
            return {"commit_sha": head.commit_sha, "branch": head.branch}

        self._router.emit_each(HostTopic.WORKSPACE_HEAD_CHANGED, payload)

    def files_changed(self, paths: frozenset[str]) -> None:
        """Files git reports as changed, each listener its own share."""
        self._router.emit_each(
            HostTopic.WORKSPACE_FILES_CHANGED,
            lambda entry: self._visible(entry, paths),
        )

    def _visible(
        self, entry: InstalledExtension, paths: frozenset[str]
    ) -> dict[str, object] | None:
        access = WorkspaceAccess(entry.manifest)
        if not access.granted:
            return None
        visible = sorted(path for path in paths if access.permits(path))
        if not visible:
            # Nothing this extension may see moved. A skip rather
            # than an empty event: waking a docs extension because
            # ``src/`` was touched is the poll we just removed,
            # re-introduced from the other end.
            return None
        truncated = len(visible) > self.MAX_PATHS
        return {"paths": visible[: self.MAX_PATHS], "truncated": truncated}


__all__ = ["HostEvents"]
