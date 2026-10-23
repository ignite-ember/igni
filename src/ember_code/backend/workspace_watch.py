"""What the project is doing, polled, for the two workspace events.

``workspace.head-changed`` is the event CodeIndex exists to wait
for: it indexes a commit, so a commit that has not moved is work it
does not need to do. Without the event it polls ``git rev-parse``
itself, once a second, forever — which is the thing an extension
platform is supposed to stop every extension having to write.

So the host polls once, and tells whoever asked.

## Why polling, and why git

There is no filesystem watcher in this process and no dependency
that would give us one. ``git`` is already required, already fast,
and already knows which files are ignored — a watcher built on
``inotify`` would have had to learn ``.gitignore`` before it was
usable, and would have reported every write inside ``node_modules``
until it did.

The cost of the choice, stated plainly: changes to ignored files
are invisible, and a project that is not a git repository produces
no workspace events at all. Both are better than the alternative,
which is an event that fires ten thousand times during a build.

## Nobody listening means nothing runs

Every poll is gated on there being an audience. A deployment with
no extension that asked for these topics does no git work at all,
and the baseline is dropped when the last listener goes away so
that the first tick after one arrives re-learns the state instead
of announcing it as a change.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ember_code.extensions.events import HostTopic

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Head:
    """Where the project's git HEAD is pointing."""

    commit_sha: str
    #: The branch name, or ``HEAD`` when detached — which is what
    #: ``git`` itself says, so an extension that renders it is
    #: rendering something its user will recognise.
    branch: str


@dataclass(frozen=True, slots=True)
class FileStamp:
    """Enough of a file's identity to notice a second edit.

    Membership of the dirty set is not enough on its own: a file
    edited twice between two polls is dirty at both, so comparing
    sets would report the first edit and swallow the second.
    """

    mtime_ns: int
    size: int


class GitWorkspace:
    """What git can say about the project, without raising.

    Deliberately not :class:`~ember_code.core.plugins.git.GitClient`
    — that one clones, fetches and resets, and a thing polled every
    second should not be holding something that can. Deliberately
    not ``core.code_index.sync.GitHead`` either: that module belongs
    to CodeIndex, and the entire point of ``workspace.head-changed``
    is that CodeIndex stops being part of the host.

    Every method returns ``None`` rather than raising. A project
    that is not a repository, a ``git`` that is not installed and a
    repository with no commits yet are all ordinary states for a
    watcher to be running in, not failures to report.
    """

    _TIMEOUT_SECONDS = 5.0

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir

    def _git(self, *args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.project_dir,
                capture_output=True,
                text=True,
                timeout=self._TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout

    def head(self) -> Head | None:
        """The current commit and branch, in one ``git`` call."""
        out = self._git("rev-parse", "HEAD", "--abbrev-ref", "HEAD")
        if out is None:
            return None
        lines = out.splitlines()
        if len(lines) < 2 or not lines[0].strip():
            return None
        return Head(commit_sha=lines[0].strip(), branch=lines[1].strip())

    def dirty(self) -> dict[str, FileStamp] | None:
        """Every path git reports as changed, stamped.

        ``--no-renames`` so a rename arrives as a delete and an add
        rather than as the only record in the format that carries
        two paths: an extension is being told what to re-read, and
        two records say that correctly.

        A path git lists but we cannot ``stat`` is a deletion, and
        is kept with a zeroed stamp — dropping it would make the
        delete invisible the moment the entry left the set.
        """
        out = self._git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames")
        if out is None:
            return None
        stamps: dict[str, FileStamp] = {}
        for record in out.split("\0"):
            # ``XY<space>path``. Shorter than four characters is the
            # trailing empty field after the final NUL.
            if len(record) < 4:
                continue
            path = record[3:]
            try:
                info = (self.project_dir / path).stat()
            except OSError:
                stamps[path] = FileStamp(mtime_ns=0, size=0)
                continue
            stamps[path] = FileStamp(mtime_ns=info.st_mtime_ns, size=info.st_size)
        return stamps


class WorkspaceWatcher:
    """Poll git, and report what moved — while anyone is listening.

    Two cadences, because the two calls do not cost the same.
    ``git rev-parse`` is microseconds and runs at 1 Hz;
    ``git status`` walks the index and runs at half that. Both are
    offloaded with :func:`asyncio.to_thread`, so a slow repository
    delays the next poll and nothing else.
    """

    HEAD_INTERVAL_SECONDS: float = 1.0
    FILES_INTERVAL_SECONDS: float = 2.0

    def __init__(
        self,
        *,
        workspace: GitWorkspace,
        on_head: Callable[[Head], None],
        on_files: Callable[[frozenset[str]], None],
        wants: Callable[[HostTopic], bool],
        head_interval_seconds: float | None = None,
        files_interval_seconds: float | None = None,
    ) -> None:
        self._workspace = workspace
        self._on_head = on_head
        self._on_files = on_files
        self._wants = wants
        self._head_interval = (
            head_interval_seconds
            if head_interval_seconds is not None
            else self.HEAD_INTERVAL_SECONDS
        )
        self._files_interval = (
            files_interval_seconds
            if files_interval_seconds is not None
            else self.FILES_INTERVAL_SECONDS
        )
        self._task: asyncio.Task | None = None
        self._head: Head | None = None
        self._dirty: dict[str, FileStamp] | None = None
        self._files_due: float = 0.0

    async def start(self) -> None:
        """Begin polling. A no-op if the loop is already running."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Cancel the poll task and wait for it to go."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task
        self._task = None

    async def poll_once(self) -> None:
        """One tick, without the loop. What the tests drive."""
        await self._check_head()
        await self._check_files()

    async def _check_head(self) -> None:
        if not self._wants(HostTopic.WORKSPACE_HEAD_CHANGED):
            # Forget where we were. Re-learning silently when a
            # listener arrives is right; telling a freshly installed
            # extension that HEAD "changed" to where it has been all
            # along is not, and it would do so on every install.
            self._head = None
            return
        head = await asyncio.to_thread(self._workspace.head)
        if head is None:
            return
        previous, self._head = self._head, head
        if previous is None or previous == head:
            return
        self._on_head(head)

    async def _check_files(self) -> None:
        loop = asyncio.get_running_loop()
        if not self._wants(HostTopic.WORKSPACE_FILES_CHANGED):
            self._dirty = None
            return
        if loop.time() < self._files_due:
            return
        self._files_due = loop.time() + self._files_interval
        dirty = await asyncio.to_thread(self._workspace.dirty)
        if dirty is None:
            return
        previous, self._dirty = self._dirty, dirty
        if previous is None:
            return
        changed = frozenset(
            path for path in previous.keys() | dirty.keys() if previous.get(path) != dirty.get(path)
        )
        if not changed:
            return
        self._on_files(changed)

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._head_interval)
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover — defensive
                logger.exception("workspace poll failed")


__all__ = ["FileStamp", "GitWorkspace", "Head", "WorkspaceWatcher"]
