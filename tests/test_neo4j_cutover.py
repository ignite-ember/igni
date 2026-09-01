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


# ── The table drop, which nothing here covered ──────────────────────────


async def test_drop_legacy_tables_drops_them_from_the_real_state_db(
    tmp_path: Path, monkeypatch
) -> None:
    """The one step that touches the database, and the one nothing tested.

    ``_drop_legacy_tables`` passed ``get_async_engine(db_path)`` to
    ``Database(...)``, which takes a *path* and does ``Path(str(db_path))``.
    So the engine's ``repr`` became a filename — a real SQLite file called
    ``<sqlalchemy.ext.asyncio.engine.AsyncEngine object at 0x...>`` appeared
    in the working directory, ``upgrade_to_head`` migrated it, and the DROP
    statements ran against *that*. The project's own ``state.db`` kept its
    legacy tables, and the next line logged success naming the real path.

    Which is why this asserts on the database rather than on the log: the
    log was already saying the right thing while nothing had happened.

    Setup order matters, and two earlier versions of this test got it
    wrong in opposite ways. Creating the legacy tables *before* the
    migration collides with alembic ("table code_index_commit_metadata
    already exists"), because the history creates them mid-way. Relying on
    the migration to leave them behind does not work either: at head they
    are gone, which is the whole reason this cutover exists — it cleans up
    what an *older* install left in a database that is otherwise current.

    So: migrate to head first, then plant the leftovers by hand, which is
    the state a real upgraded install is in.
    """
    import sqlite3

    from ember_code.core.code_index.cutover import _drop_legacy_tables
    from ember_code.core.code_index.paths import state_db_path
    from ember_code.core.db.database import Database

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    project = tmp_path / "proj"
    project.mkdir()

    db_path = state_db_path(project, data_dir=data_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    Database(db_path)  # migrate to head — at head the legacy tables are absent

    def tables() -> set[str]:
        connection = sqlite3.connect(db_path)
        try:
            return {
                r[0]
                for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            connection.close()

    # Plant what an older install would have left behind, plus one table the
    # cutover must not touch.
    connection = sqlite3.connect(db_path)
    try:
        for table in _LEGACY_CODE_INDEX_TABLES:
            connection.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE keep_me (id INTEGER PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()

    present = tables() & set(_LEGACY_CODE_INDEX_TABLES)
    assert present == set(_LEGACY_CODE_INDEX_TABLES), present

    # Run from a directory of our own, so a stray file lands somewhere
    # observable instead of in the repository root.
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    await _drop_legacy_tables(project, data_dir)

    remaining = tables()
    for table in present:
        assert table not in remaining, (
            f"{table} survived in the real state.db — the drop ran somewhere else"
        )
    assert "keep_me" in remaining, "the cutover dropped a table it does not own"

    # The symptom that gave the bug away, asserted directly.
    strays = [p.name for p in workdir.iterdir() if "AsyncEngine object at" in p.name]
    assert not strays, f"a database was created from an engine repr: {strays}"


async def test_drop_legacy_tables_is_a_no_op_without_a_state_db(tmp_path: Path) -> None:
    """It returns early rather than creating one."""
    from ember_code.core.code_index.cutover import _drop_legacy_tables
    from ember_code.core.code_index.paths import state_db_path

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    project = tmp_path / "proj"
    project.mkdir()

    await _drop_legacy_tables(project, data_dir)

    assert not state_db_path(project, data_dir=data_dir).exists()
