"""Where igni keeps a project's data on disk.

The sibling of :mod:`ember_code.core.paths`, and the split between
them is worth one sentence each: that module holds *what things are
called* — the config directory, the context filenames, the agent's
name — and this one holds *where a project's state lives*.

    ~/.igni/
      ember.db                        # GLOBAL: agno memory + learning
      projects/<project_id>/
        state.db                      # PROJECT: scheduler, sessions,
                                      #          loop, background procs

The data root is configurable (``settings.storage.data_dir``) and
defaults to ``~/.igni``. What an extension keeps on this machine is
not here: each has its own directory beside the extension store
(``IGNI_EXTENSION_DATA``).
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from pathlib import Path

from ember_code.core.paths import DEFAULT_DATA_DIR

logger = logging.getLogger(__name__)

#: How long ``git remote get-url`` may take before we fall back to
#: the path. A watcher-free one-shot, but it runs on a directory the
#: user named and a network filesystem can hang.
_GIT_TIMEOUT_SECONDS = 5.0


def resolve_project_id(project_dir: str | Path) -> str:
    """A stable id for ``project_dir``, for naming its data directory.

    Derived from the git remote origin URL when there is one, so two
    clones of the same repository share an index, and from the
    absolute path when there is not. Both are SHA-256 hashed and
    truncated — long enough that a collision across one user's
    projects is not worth reasoning about.
    """
    project_id = str(Path(project_dir))
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            cwd=str(project_dir),
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        if result.returncode == 0 and result.stdout.strip():
            project_id = result.stdout.strip()
    except Exception as exc:
        logger.debug("git remote lookup failed for %s: %s", project_dir, exc)

    return hashlib.sha256(project_id.encode()).hexdigest()[:16]


def data_root(data_dir: str | Path = DEFAULT_DATA_DIR) -> Path:
    return Path(str(data_dir)).expanduser()


def global_db_path(data_dir: str | Path = DEFAULT_DATA_DIR) -> Path:
    """The one database that is not per-project: memory and learning."""
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
    "data_root",
    "global_db_path",
    "project_data_dir",
    "resolve_project_id",
    "state_db_path",
]
