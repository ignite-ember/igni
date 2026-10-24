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
        knowledge.chroma/             # LEGACY: pre-Neo4j knowledge
      neo4j/
        state/<project_id>-<sha>/     # PER-COMMIT: one store per commit
        state/<project_id>-knowledge/ # PER-PROJECT: the knowledge store

The data root is configurable (``settings.storage.data_dir``) and
defaults to ``~/.igni``.

## Why this is not inside CodeIndex any more

It was. ``core/code_index/paths.py`` owned ``state_db_path``, and
``state.db`` is where the scheduler, the session list, ``/loop`` and
the background-process registry live — none of which is an index.
Nine modules outside CodeIndex imported it, and the number had grown
from four, because this is where a data path *is* and nobody moves a
directory to add a lookup.

That mattered beyond tidiness. CodeIndex is becoming an extension,
and an extension is a thing the host can be built without. A host
that could not find its own scheduler database without importing
CodeIndex was not one you could uninstall CodeIndex from.

What stayed behind is what is genuinely CodeIndex's: its manifest,
its per-commit chroma directories, and the cutover's marker.

## The Neo4j layout is shared, not CodeIndex's

One sidecar serves the per-commit code graph *and* the named
per-project stores extensions ask for, which is why
:func:`is_commit_slug` lives beside the per-commit helpers: anything
enumerating ``state/`` has to tell them apart, and reading a named
store as an untracked commit would make retention delete it.
"""

from __future__ import annotations

import hashlib
import re
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

    Named ``project_data_dir`` rather than ``project_dir``, which is
    what it was called inside CodeIndex: ``project_dir`` is the name
    of a *parameter* in 238 places in this repository, and a function
    that shadows it in every module that imports it is a function
    somebody will call by accident.
    """
    return data_root(data_dir) / "projects" / resolve_project_id(project)


def state_db_path(project: str | Path, *, data_dir: str | Path = DEFAULT_DATA_DIR) -> Path:
    """The project's SQLite database — scheduler, sessions, loop, procs."""
    return project_data_dir(project, data_dir=data_dir) / "state.db"


def legacy_artifact_index_path(
    project: str | Path, *, data_dir: str | Path = DEFAULT_DATA_DIR
) -> Path:
    """Pre-Neo4j knowledge vectors. Nothing writes it; the cutover
    deletes it."""
    return project_data_dir(project, data_dir=data_dir) / "knowledge.chroma"


# ── Neo4j sidecar layout ──────────────────────────────────────────


def neo4j_data_dir(data_dir: str | Path = DEFAULT_DATA_DIR) -> Path:
    """Shared Neo4j root: distribution cache + per-commit process state.

    Project-independent — the downloaded Neo4j (and bundled JDK) is
    reused across every project, mirroring the Tauri backend-python
    cache pattern.
    """
    return data_root(data_dir) / "neo4j"


_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def is_commit_slug(suffix: str) -> bool:
    """Whether ``<project>-<suffix>`` in ``state/`` is a commit's store.

    The same directory holds named stores (``<project>-<instance>``)
    that extensions asked for; the runtime refuses an instance name
    shaped like a sha, so shape alone tells them apart. Retention
    must only ever reclaim commits.
    """
    return bool(_COMMIT_SHA.match(suffix))


def neo4j_state_root(data_dir: str | Path = DEFAULT_DATA_DIR) -> Path:
    """Directory holding one sub-directory per Neo4j process.

    Enumerable on purpose: this is where per-commit stores
    accumulate, and retention needs to see the ones the manifest no
    longer tracks.
    """
    return neo4j_data_dir(data_dir) / "state"


def neo4j_runtime_dir(
    project: str | Path, commit_sha: str, *, data_dir: str | Path = DEFAULT_DATA_DIR
) -> Path:
    """Per-(project, commit) Neo4j process state directory.

    Holds the commit's ``data/``, ``auth.txt`` and ``runtime.json``.
    Keyed by ``<project_id>-<commit_sha>`` so each commit gets its own
    isolated store.
    """
    slug = f"{resolve_project_id(project)}-{commit_sha}"
    return neo4j_state_root(data_dir) / slug


def neo4j_auth_file(
    project: str | Path, commit_sha: str, *, data_dir: str | Path = DEFAULT_DATA_DIR
) -> Path:
    """The generated Neo4j password file for this commit."""
    return neo4j_runtime_dir(project, commit_sha, data_dir=data_dir) / "auth.txt"


__all__ = [
    "is_commit_slug",
    "data_root",
    "global_db_path",
    "legacy_artifact_index_path",
    "neo4j_auth_file",
    "neo4j_data_dir",
    "neo4j_runtime_dir",
    "neo4j_state_root",
    "project_data_dir",
    "resolve_project_id",
    "state_db_path",
]
