"""Unit tests for the cutover module.

Covers the file-system side of the cutover without requiring a
live Neo4j. The meta-DB guard (``neo4j.migrated`` Meta node) is
exercised by ``test_neo4j_integration.py``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from ember_code.core.code_index.cutover import (
    _LEGACY_CODE_INDEX_TABLES,
    _drop_manifest_json,
    _rmtree_chroma_dirs,
    _rmtree_knowledge_chroma,
)
from ember_code.core.code_index.paths import (
    code_index_dir,
    legacy_knowledge_index_path,
    manifest_path,
)
from ember_code.core.code_index.project import resolve_project_id


@pytest.fixture
def project_tree(tmp_path: Path) -> tuple[Path, Path]:
    """Build a fake project layout under ``tmp_path/data/``.

    Returns ``(project, data_dir)``. The cutover reads the
    project layout via :func:`code_index_dir` etc. which
    resolve from ``data_dir``, not from the project dir itself.

    Builds one per-commit chroma dir + a knowledge.chroma + a
    manifest.json so every destructive step has something to
    delete.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    base = code_index_dir(project, data_dir=data_dir)
    chroma = base / "deadbeef.chroma"
    chroma.mkdir(parents=True)
    (chroma / "chroma.sqlite3").write_text("fake")
    knowledge = legacy_knowledge_index_path(project, data_dir=data_dir)
    knowledge.mkdir()
    (knowledge / "chroma.sqlite3").write_text("fake")
    manifest_path(project, data_dir=data_dir).write_text("{}")
    return project, data_dir


def test_legacy_tables_constant_lists_what_gets_dropped() -> None:
    """The two code_index tables from SQLite move to Neo4j."""
    assert "code_index_file_reference" in _LEGACY_CODE_INDEX_TABLES
    assert "code_index_commit_metadata" in _LEGACY_CODE_INDEX_TABLES


def test_rmtree_chroma_dirs_removes_per_commit_dirs(project_tree) -> None:
    project, data_dir = project_tree
    _rmtree_chroma_dirs(project, data_dir)
    base = code_index_dir(project, data_dir=data_dir)
    assert not (base / "deadbeef.chroma").exists()


def test_rmtree_chroma_dirs_skips_when_no_base(project_tree) -> None:
    project, data_dir = project_tree
    shutil.rmtree(code_index_dir(project, data_dir=data_dir))
    # No raise, no error — just a no-op.
    _rmtree_chroma_dirs(project, data_dir)


def test_rmtree_chroma_dirs_ignores_non_chroma_dirs(project_tree) -> None:
    project, data_dir = project_tree
    base = code_index_dir(project, data_dir=data_dir)
    (base / "keep.txt").write_text("don't delete me")
    _rmtree_chroma_dirs(project, data_dir)
    assert (base / "keep.txt").exists()


def test_rmtree_knowledge_chroma_removes_dir(project_tree) -> None:
    project, data_dir = project_tree
    _rmtree_knowledge_chroma(project, data_dir)
    assert not legacy_knowledge_index_path(project, data_dir=data_dir).exists()


def test_rmtree_knowledge_chroma_skips_when_missing(project_tree) -> None:
    project, data_dir = project_tree
    shutil.rmtree(legacy_knowledge_index_path(project, data_dir=data_dir))
    # No raise.
    _rmtree_knowledge_chroma(project, data_dir)


def test_drop_manifest_json_deletes_file(project_tree) -> None:
    project, data_dir = project_tree
    manifest = manifest_path(project, data_dir=data_dir)
    assert manifest.exists()
    _drop_manifest_json(project, data_dir)
    assert not manifest.exists()


def test_drop_manifest_json_skips_when_missing(project_tree) -> None:
    project, data_dir = project_tree
    manifest_path(project, data_dir=data_dir).unlink()
    # No raise.
    _drop_manifest_json(project, data_dir)


def test_full_cutover_sweep_cleans_every_legacy_artifact(project_tree) -> None:
    """Run the destructive steps in order — every legacy artifact gone."""
    project, data_dir = project_tree
    _rmtree_chroma_dirs(project, data_dir)
    _rmtree_knowledge_chroma(project, data_dir)
    _drop_manifest_json(project, data_dir)
    base = code_index_dir(project, data_dir=data_dir)
    assert not (base / "deadbeef.chroma").exists()
    assert not legacy_knowledge_index_path(project, data_dir=data_dir).exists()
    assert not manifest_path(project, data_dir=data_dir).exists()


def test_resolve_project_id_is_deterministic(project_tree) -> None:
    """resolve_project_id produces the same hash for the same path."""
    project, _ = project_tree
    assert resolve_project_id(project) == resolve_project_id(project)
