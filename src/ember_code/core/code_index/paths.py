"""Where CodeIndex keeps its own files.

Four paths, all of them inside the project's data directory, and
every one of them about an index.

    ~/.igni/projects/<project_id>/
      code_index/
        manifest.json      # LIVE: head sha, commit list, branch pins
        .neo4j_migrated    # the cutover's per-project marker
        <sha>.chroma/      # LEGACY: pre-Neo4j code vectors

## What used to be here and is not

``data_root``, ``global_db_path``, ``project_dir``, ``state_db_path``
and the whole Neo4j layout lived in this module and have moved to
:mod:`ember_code.core.data_paths`. None of them was CodeIndex's:
``state.db`` holds the scheduler, the session list, ``/loop`` and the
background-process registry, and the Neo4j sidecar serves Artifacts'
knowledge store as well as the code graph.

Nine modules outside CodeIndex imported this file for them, up from
four, because this is where a data path *was* and nobody relocates a
directory to add one lookup. CodeIndex is becoming an extension, and
a host that could not find its own scheduler database without
importing CodeIndex is not one you can uninstall CodeIndex from.

## The chroma paths are legacy

Nothing writes them. ``chromadb`` is not imported anywhere and is no
longer a dependency; vectors live in the Neo4j sidecar. The helpers
survive because :mod:`ember_code.core.code_index.cutover` still has
directories to delete — the cutover is **per-project and lazy**,
gated on the sentinel :func:`neo4j_migrated_marker_path` returns, so
a project nobody has opened since it shipped still holds its old
ones.

They are named ``legacy_`` for that reason: a caller reaching for one
outside the cutover is almost certainly measuring or reading
something that no longer exists.
"""

from __future__ import annotations

from pathlib import Path

from ember_code.core.data_paths import project_data_dir
from ember_code.core.paths import DEFAULT_DATA_DIR


def code_index_dir(project: str | Path, *, data_dir: str | Path = DEFAULT_DATA_DIR) -> Path:
    return project_data_dir(project, data_dir=data_dir) / "code_index"


def legacy_commit_index_path(
    project: str | Path, commit_sha: str, *, data_dir: str | Path = DEFAULT_DATA_DIR
) -> Path:
    return code_index_dir(project, data_dir=data_dir) / f"{commit_sha}.chroma"


def manifest_path(project: str | Path, *, data_dir: str | Path = DEFAULT_DATA_DIR) -> Path:
    return code_index_dir(project, data_dir=data_dir) / "manifest.json"


def neo4j_migrated_marker_path(
    project: str | Path, *, data_dir: str | Path = DEFAULT_DATA_DIR
) -> Path:
    """Sentinel marking that the chroma+sqlite→Neo4j cutover ran.

    Lives in the per-project ``code_index/`` dir rather than the
    shared Neo4j root because the cutover is project-scoped — it
    drops that project's ``code_index_*`` SQLite tables and chroma
    directories.
    """
    return code_index_dir(project, data_dir=data_dir) / ".neo4j_migrated"


__all__ = [
    "code_index_dir",
    "legacy_commit_index_path",
    "manifest_path",
    "neo4j_migrated_marker_path",
]
