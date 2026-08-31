"""Filesystem layout for ember-code data.

Single source of truth for where state lives on disk:

    ~/.igni/
      ember.db                          # GLOBAL: agno memory + learning
      projects/<project_hash>/
        state.db                        # PROJECT: scheduler, sessions
        knowledge.chroma/               # LEGACY: pre-Neo4j knowledge
        code_index/
          manifest.json                 # LEGACY
          <sha>.chroma/                 # LEGACY: pre-Neo4j code vectors
      neo4j/
        state/<project_id>/.migrated    # the cutover's per-project marker

The data root is configurable (``settings.storage.data_dir``); defaults
to ``~/.igni``.

## The chroma paths are legacy

Nothing writes them. ``chromadb`` is not imported anywhere and is no
longer a dependency; vectors live in the Neo4j sidecar. The helpers
below survive because :mod:`ember_code.core.code_index.cutover` still
has directories to delete — the cutover is **per-project and lazy**,
gated on a sentinel under ``neo4j/state/``, so a project nobody has
opened since it shipped still holds its old ones.

They are named ``legacy_`` for that reason: a caller reaching for one
outside the cutover is almost certainly measuring or reading something
that no longer exists.
"""

from __future__ import annotations

from pathlib import Path

from ember_code.core.code_index.project import resolve_project_id
from ember_code.core.paths import DEFAULT_DATA_DIR


def data_root(data_dir: str | Path = DEFAULT_DATA_DIR) -> Path:
    return Path(str(data_dir)).expanduser()


def global_db_path(data_dir: str | Path = DEFAULT_DATA_DIR) -> Path:
    return data_root(data_dir) / "ember.db"


def project_dir(project: str | Path, *, data_dir: str | Path = DEFAULT_DATA_DIR) -> Path:
    """Per-project data directory — derived from a stable git/path hash."""
    return data_root(data_dir) / "projects" / resolve_project_id(project)


def state_db_path(project: str | Path, *, data_dir: str | Path = DEFAULT_DATA_DIR) -> Path:
    return project_dir(project, data_dir=data_dir) / "state.db"


def legacy_knowledge_index_path(project: str | Path, *, data_dir: str | Path = DEFAULT_DATA_DIR) -> Path:
    return project_dir(project, data_dir=data_dir) / "knowledge.chroma"


def code_index_dir(project: str | Path, *, data_dir: str | Path = DEFAULT_DATA_DIR) -> Path:
    return project_dir(project, data_dir=data_dir) / "code_index"


def legacy_commit_index_path(
    project: str | Path, commit_sha: str, *, data_dir: str | Path = DEFAULT_DATA_DIR
) -> Path:
    return code_index_dir(project, data_dir=data_dir) / f"{commit_sha}.chroma"


def manifest_path(project: str | Path, *, data_dir: str | Path = DEFAULT_DATA_DIR) -> Path:
    return code_index_dir(project, data_dir=data_dir) / "manifest.json"


# ── Neo4j sidecar layout ──────────────────────────────────────────
#
#     ~/.igni/
#       neo4j/                            # shared: distribution + per-commit state
#         state/<project_hash>-<sha>/     # PER-COMMIT: data, auth, runtime.json
#
# The Neo4j distribution (and its JDK) is project-independent — one
# download shared by every project — so ``neo4j_data_dir`` takes no
# project argument. Per-commit process state lives under ``state/``.


def neo4j_data_dir(data_dir: str | Path = DEFAULT_DATA_DIR) -> Path:
    """Shared Neo4j root: distribution cache + per-commit process state.

    Project-independent — the downloaded Neo4j (and bundled JDK) is
    reused across every project, mirroring the Tauri backend-python
    cache pattern.
    """
    return data_root(data_dir) / "neo4j"


def neo4j_runtime_dir(
    project: str | Path, commit_sha: str, *, data_dir: str | Path = DEFAULT_DATA_DIR
) -> Path:
    """Per-(project, commit) Neo4j process state directory.

    Holds the commit's ``data/``, ``auth.txt``, and ``runtime.json``.
    Keyed by ``<project_hash>-<commit_sha>`` so each commit gets its
    own isolated Neo4j process/store.
    """
    slug = f"{resolve_project_id(project)}-{commit_sha}"
    return neo4j_data_dir(data_dir) / "state" / slug


def neo4j_auth_file(
    project: str | Path, commit_sha: str, *, data_dir: str | Path = DEFAULT_DATA_DIR
) -> Path:
    """Path to the generated Neo4j password file for this commit."""
    return neo4j_runtime_dir(project, commit_sha, data_dir=data_dir) / "auth.txt"


def neo4j_migrated_marker_path(project: str | Path, *, data_dir: str | Path = DEFAULT_DATA_DIR) -> Path:
    """Sentinel marking that the chroma+sqlite→Neo4j cutover ran.

    Lives in the per-project ``code_index/`` dir (not the shared
    Neo4j root) because the cutover is project-scoped — it drops that
    project's ``code_index_*`` SQLite tables and chroma dirs.
    """
    return code_index_dir(project, data_dir=data_dir) / ".neo4j_migrated"
