"""Where igni keeps a project's data on disk.

The sibling of :mod:`ember_code.core.paths`, and the split between
them is worth one sentence each: that module holds *what things are
called* — the config directory, the context filenames, the agent's
name — and this one holds *where a project's state lives*.

    ~/.igni/
      ember.db                        # GLOBAL: which folder a session is
                                      #         in, window state, local
                                      #         notifications
      projects/<project_id>/
        state.db                      # PROJECT: scheduler, sessions,
                                      #          loop, background procs

The data root is configurable (``settings.storage.data_dir``) and
defaults to ``~/.igni``. These are the desktop's files; with
``storage.database_url`` set (the cloud) both are one database instead
(:class:`ember_code.core.db.locator.StorageLocator`). What an extension keeps on this machine is
not here: each has its own directory beside the extension store
(``IGNI_EXTENSION_DATA``).
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import threading
from pathlib import Path

from ember_code.core.paths import DEFAULT_DATA_DIR

logger = logging.getLogger(__name__)

#: How long ``git remote get-url`` may take before we fall back to
#: the path. A watcher-free one-shot, but it runs on a directory the
#: user named and a network filesystem can hang.
_GIT_TIMEOUT_SECONDS = 5.0


def canonical_project_dir(project_dir: str | Path) -> Path:
    """The one spelling of ``project_dir`` every per-project path is built from.

    ``~`` expanded, symlinks resolved, made absolute — so ``/var/x`` and
    ``/private/var/x`` on macOS, a relative path and a trailing slash all
    name the same project, and the backend, ``igni migrate`` and every
    store open the same ``state.db``. ``strict=False``: a project that
    does not exist yet still has a name.
    """
    return Path(str(project_dir)).expanduser().resolve(strict=False)


class ProjectIds:
    """Project ids, asked of git once per repository state.

    The id comes from ``git remote get-url origin``, a subprocess — and
    one backend boot asks for it about a dozen times (every store that
    opens a scope). Kept per project directory until the repository's
    ``.git`` or its ``config`` changes, which is where a remote is
    added, removed or renamed; so a remote set mid-session is seen as
    before, and an unchanged repository costs a few ``stat`` calls.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ids: dict[str, tuple[tuple[object, ...], str]] = {}

    def of(self, project_dir: str | Path) -> str:
        project = canonical_project_dir(project_dir)
        fingerprint = self._fingerprint(project)
        key = str(project)
        with self._lock:
            known = self._ids.get(key)
        if known is not None and known[0] == fingerprint:
            return known[1]
        project_id = self._ask_git(project)
        with self._lock:
            self._ids[key] = (fingerprint, project_id)
        return project_id

    def clear(self) -> None:
        with self._lock:
            self._ids.clear()

    @staticmethod
    def _fingerprint(project: Path) -> tuple[object, ...]:
        """The state of the nearest ``.git`` and its ``config``; empty outside a repository."""
        for directory in (project, *project.parents):
            git = directory / ".git"
            try:
                stat = git.stat()
            except OSError:
                continue
            marks: list[object] = [str(git), stat.st_mtime_ns, stat.st_ino]
            try:
                config = (git / "config").stat()
            except OSError:
                pass
            else:
                marks += [config.st_mtime_ns, config.st_size, config.st_ino]
            return tuple(marks)
        return ()

    @staticmethod
    def _ask_git(project: Path) -> str:
        project_id = str(project)
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                cwd=str(project),
                timeout=_GIT_TIMEOUT_SECONDS,
            )
            if result.returncode == 0 and result.stdout.strip():
                project_id = result.stdout.strip()
        except Exception as exc:
            logger.debug("git remote lookup failed for %s: %s", project, exc)
        return hashlib.sha256(project_id.encode()).hexdigest()[:16]


#: The process's one cache of them.
project_ids = ProjectIds()


def resolve_project_id(project_dir: str | Path) -> str:
    """A stable id for ``project_dir``, for naming its data directory.

    Derived from the git remote origin URL when there is one, so two
    clones of the same repository share an index, and from the
    absolute path when there is not. Both are SHA-256 hashed and
    truncated — long enough that a collision across one user's
    projects is not worth reasoning about. Cached (:class:`ProjectIds`).
    """
    return project_ids.of(project_dir)


def data_root(data_dir: str | Path = DEFAULT_DATA_DIR) -> Path:
    return Path(str(data_dir)).expanduser()


def global_db_path(data_dir: str | Path = DEFAULT_DATA_DIR) -> Path:
    """The one database that is not per-project: which folder a session
    is in, window state, local notifications."""
    return data_root(data_dir) / "ember.db"


def project_data_dir(project: str | Path, *, data_dir: str | Path = DEFAULT_DATA_DIR) -> Path:
    """Per-project data directory, keyed by :func:`resolve_project_id`.

    Named ``project_data_dir`` rather than ``project_dir``: that is the name
    of a *parameter* in 238 places in this repository, and a function
    that shadows it in every module that imports it is a function
    somebody will call by accident.
    """
    return data_root(data_dir) / "projects" / resolve_project_id(project)


def state_db_path(project: str | Path, *, data_dir: str | Path = DEFAULT_DATA_DIR) -> Path:
    """The project's SQLite database — scheduler, sessions, loop, procs."""
    return project_data_dir(project, data_dir=data_dir) / "state.db"


__all__ = [
    "ProjectIds",
    "canonical_project_dir",
    "data_root",
    "global_db_path",
    "project_data_dir",
    "project_ids",
    "resolve_project_id",
    "state_db_path",
]
