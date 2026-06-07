"""Filesystem layout for ember-code data.

Single source of truth for where state lives on disk:

    ~/.ember/
      ember.db                          # GLOBAL: agno memory + learning
      projects/<project_hash>/
        state.db                        # PROJECT: scheduler, sessions, code_index SQL
        knowledge.chroma/               # PROJECT: knowledge entries
        code_index/
          manifest.json
          <sha>.chroma/                 # PER-COMMIT: code vectors

The data root is configurable (``settings.storage.data_dir``); defaults
to ``~/.ember``.
"""

from __future__ import annotations

from pathlib import Path

from ember_code.core.code_index.project import resolve_project_id


def data_root(data_dir: str | Path = "~/.ember") -> Path:
    return Path(str(data_dir)).expanduser()


def global_db_path(data_dir: str | Path = "~/.ember") -> Path:
    return data_root(data_dir) / "ember.db"


def project_dir(project: str | Path, *, data_dir: str | Path = "~/.ember") -> Path:
    """Per-project data directory — derived from a stable git/path hash."""
    return data_root(data_dir) / "projects" / resolve_project_id(project)


def state_db_path(project: str | Path, *, data_dir: str | Path = "~/.ember") -> Path:
    return project_dir(project, data_dir=data_dir) / "state.db"


def knowledge_chroma_path(project: str | Path, *, data_dir: str | Path = "~/.ember") -> Path:
    return project_dir(project, data_dir=data_dir) / "knowledge.chroma"


def code_index_dir(project: str | Path, *, data_dir: str | Path = "~/.ember") -> Path:
    return project_dir(project, data_dir=data_dir) / "code_index"


def commit_chroma_path(
    project: str | Path, commit_sha: str, *, data_dir: str | Path = "~/.ember"
) -> Path:
    return code_index_dir(project, data_dir=data_dir) / f"{commit_sha}.chroma"


def manifest_path(project: str | Path, *, data_dir: str | Path = "~/.ember") -> Path:
    return code_index_dir(project, data_dir=data_dir) / "manifest.json"
