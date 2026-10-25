"""Migrations run only for a database that is new or behind.

:class:`ember_code.core.db.schema.SchemaKeeper` reads a file's revision
with :mod:`sqlite3` and imports alembic only when it has to.
:data:`HEAD_REVISION` is its copy of alembic's head, so the first test
here is the one that fails when a migration is added without it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

import ember_code
from ember_code.core.db.engine import sync_url
from ember_code.core.db.schema import HEAD_REVISION, SchemaKeeper, SchemaRevision

PACKAGE = Path(ember_code.__file__).resolve().parent


def test_the_head_constant_is_alembics_head():
    config = Config(str(PACKAGE / "alembic.ini"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert heads == [HEAD_REVISION], (
        f"a migration was added: set HEAD_REVISION in ember_code/core/db/schema.py to {heads}"
    )


def test_a_missing_or_unmigrated_file_has_no_revision(tmp_path: Path):
    assert SchemaRevision.of(tmp_path / "absent.db") is None
    bare = tmp_path / "bare.db"
    sqlite3.connect(bare).close()
    assert SchemaRevision.of(bare) is None


def test_a_new_file_is_brought_to_head(tmp_path: Path):
    path = tmp_path / "state.db"
    SchemaKeeper().ensure_current(path)
    assert SchemaRevision.of(path) == HEAD_REVISION


def test_a_file_behind_head_is_migrated(tmp_path: Path):
    path = tmp_path / "state.db"
    config = Config(str(PACKAGE / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", sync_url(str(path)))
    config.attributes["configure_logging"] = False
    command.upgrade(config, "9318ebdb0db5")
    assert SchemaRevision.of(path) == "9318ebdb0db5"
    SchemaKeeper().ensure_current(path)
    assert SchemaRevision.of(path) == HEAD_REVISION


def _modules_after(script: str, cwd: Path, env: dict[str, str]) -> list[str]:
    out = subprocess.run(
        [sys.executable, "-c", script], cwd=cwd, env=env, capture_output=True, text=True, check=True
    ).stdout
    return json.loads(out.strip().splitlines()[-1])


def test_a_current_file_never_imports_alembic(tmp_path: Path):
    path = tmp_path / "state.db"
    SchemaKeeper().ensure_current(path)
    script = textwrap.dedent(
        f"""
        import json, sys
        from ember_code.core.db.database import Database
        Database({str(path)!r})
        print(json.dumps(sorted(m for m in sys.modules if m.split(".")[0] == "alembic")))
        """
    )
    assert _modules_after(script, tmp_path, dict(os.environ)) == []


def test_igni_migrate_prepares_the_projects_database(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    env = dict(os.environ, HOME=str(home))
    first = subprocess.run(
        [sys.executable, "-m", "ember_code", "migrate"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert f"new -> {HEAD_REVISION}" in first
    again = subprocess.run(
        [sys.executable, "-m", "ember_code", "migrate"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert f"already at {HEAD_REVISION}" in again
