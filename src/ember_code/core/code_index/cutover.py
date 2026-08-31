"""Idempotent migration cutover from chroma + sqlite to Neo4j.

First-launch one-shot. Replaces the per-project SQLite
``code_index_*`` tables + every per-commit ``<sha>.chroma/`` dir +
the ``knowledge.chroma`` dir + ``manifest.json`` with a Neo4j
sidecar.

The cutover is gated on a sentinel file at
``<data_dir>/neo4j/state/<project_id>/.migrated``. The marker
is written LAST, so a crash mid-cutover re-runs cleanly on the
next BE startup.

Run order:

1. If the sentinel file exists → return (already cut over).
2. Drop ``code_index_file_reference`` + ``code_index_commit_metadata``
   from ``state.db``. Other tables (agno, loop, scheduler, …) stay.
3. ``rmtree`` every ``<sha>.chroma/`` under ``code_index/``.
4. ``rmtree`` every project's ``knowledge.chroma/``.
5. Delete ``manifest.json``.
6. Create the sentinel file with the ISO timestamp as content.

Steps 2-5 are individually idempotent (DROP TABLE IF EXISTS,
rmtree with ignore_errors, unlink-missing). Step 6 is the
completion signal.

The function is conservative — it never raises on the destructive
steps. A partial failure leaves the system in a "cutover in
progress" state that re-runs cleanly.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from ember_code.core.code_index.paths import (
    code_index_dir,
    legacy_knowledge_index_path,
    manifest_path,
    neo4j_migrated_marker_path,
)
from ember_code.core.code_index.project import resolve_project_id
from ember_code.core.db.database import Database
from ember_code.core.db.engine import get_async_engine

logger = logging.getLogger(__name__)


# Tables that move from SQLite to Neo4j. Listed here so the cutover
# script is the single source of truth — adding a new code_index
# table means adding one line below (the ``drop_table`` step reads
# this constant) and one matching Cypher migration in
# :mod:`neo4j_schema`.
_LEGACY_CODE_INDEX_TABLES: tuple[str, ...] = (
    "code_index_file_reference",
    "code_index_commit_metadata",
)


async def maybe_cutover(
    *,
    project: str | Path,
    data_dir: str | Path,
) -> bool:
    """Run the cutover if it hasn't run yet. Returns True if executed.

    Idempotent: subsequent calls are no-ops once the sentinel file
    exists.
    """
    project_path = Path(project)
    project_id = resolve_project_id(project_path)
    marker_path = neo4j_migrated_marker_path(project_path, data_dir=data_dir)

    if marker_path.exists():
        logger.debug("neo4j cutover already complete for %s", project_id)
        return False

    logger.info("running neo4j cutover for project %s", project_id)
    await _drop_legacy_tables(project_path, data_dir)
    _rmtree_chroma_dirs(project_path, data_dir)
    _rmtree_knowledge_chroma(project_path, data_dir)
    _drop_manifest_json(project_path, data_dir)

    marker_path.parent.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    marker_path.write_text(now_iso, encoding="utf-8")
    logger.info("neo4j cutover complete for project %s at %s", project_id, now_iso)
    return True


# ── Internals (synchronous I/O) ─────────────────────────────────────────


async def _drop_legacy_tables(project: Path, data_dir: str | Path) -> None:
    """Drop the code_index_* tables from the per-project state.db.

    Other tables (agno memory, loop, scheduler, process_store,
    session prefs) are untouched. ``DROP TABLE IF EXISTS`` is
    idempotent — running on an already-empty DB is a no-op.
    """
    from ember_code.core.code_index.paths import state_db_path

    db_path = state_db_path(project, data_dir=data_dir)
    if not db_path.exists():
        logger.debug("no state.db at %s; skipping table drop", db_path)
        return
    engine = get_async_engine(db_path)
    async with Database(engine).session() as session, session.begin():
        for table in _LEGACY_CODE_INDEX_TABLES:
            await session.execute(text(f"DROP TABLE IF EXISTS {table}"))
    logger.info("dropped legacy code_index tables from %s", db_path)


def _rmtree_chroma_dirs(project: Path, data_dir: str | Path) -> None:
    """``rmtree`` every ``<sha>.chroma/`` under the project's code_index dir."""
    base = code_index_dir(project, data_dir=data_dir)
    if not base.is_dir():
        logger.debug("no code_index dir at %s; skipping", base)
        return
    for child in base.iterdir():
        if not child.is_dir() or not child.name.endswith(".chroma"):
            continue
        shutil.rmtree(child, ignore_errors=True)
        logger.debug("removed chroma dir: %s", child)


def _rmtree_knowledge_chroma(project: Path, data_dir: str | Path) -> None:
    """``rmtree`` the project's knowledge.chroma directory."""
    path = legacy_knowledge_index_path(project, data_dir=data_dir)
    if not path.exists():
        logger.debug("no knowledge.chroma at %s; skipping", path)
        return
    shutil.rmtree(path, ignore_errors=True)
    logger.info("removed knowledge.chroma at %s", path)


def _drop_manifest_json(project: Path, data_dir: str | Path) -> None:
    """Delete the legacy ``manifest.json`` file.

    The head pointer + last_used + branch_pins now live in the
    meta DB; the JSON file is no longer authoritative.
    """
    path = manifest_path(project, data_dir=data_dir)
    if not path.exists():
        logger.debug("no manifest.json at %s; skipping", path)
        return
    try:
        path.unlink()
    except OSError as exc:
        logger.warning("failed to delete %s: %s", path, exc)
        return
    logger.info("removed manifest.json at %s", path)
