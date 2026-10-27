"""Granting a producer its role runs on a new producer's first start: it stays small.

``docs/CLOUD.md`` §9. The node agent runs it on the host before a
producer's VM can bind, so its import is on that path. It imports the
role code and SQLAlchemy, not Agno, not igni's settings and not the rest
of igni's CLI.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from click.testing import CliRunner
from igni_cloud.cli import main
from igni_cloud.db.admin_cli import db
from sqlalchemy import create_engine, text

from ember_code.cli.migrate import SchemaMigration
from ember_code.core.db.locator import StorageLocator
from ember_code.core.db.urls import DatabaseUrl
from tests.storage_backends import ThrowawayPostgres


def test_the_command_imports_none_of_the_rest_of_igni():
    probe = (
        "import sys, igni_cloud.db.admin_cli\n"
        "heavy = [m for m in ('agno', 'ember_code.cli', 'ember_code.core.config.settings', "
        "'ember_code.backend', 'openai', 'mcp') if m in sys.modules]\n"
        "print(heavy)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == "[]"


def test_igni_cloud_db_is_the_same_commands():
    assert main.commands["db"] is db


def test_without_a_database_it_says_how_to_name_one(monkeypatch):
    monkeypatch.delenv("IGNI_DATABASE_URL", raising=False)
    result = CliRunner().invoke(db, ["grant-producer", "--org", "o", "--producer", "person:p"])
    assert result.exit_code != 0 and "IGNI_DATABASE_URL" in result.output


def test_it_grants_and_revokes_a_producer(postgres_url: str):
    database = ThrowawayPostgres(postgres_url)
    admin = DatabaseUrl.parse(database.create())
    locator = StorageLocator(database_url=admin.url, data_dir="/nonexistent")
    for target in SchemaMigration(locator, project=Path("/")).targets():
        SchemaMigration.migrate(target)
    try:
        _grant_and_revoke(admin)
        space = CliRunner().invoke(
            db,
            [
                "grant-producer",
                *("--org", "acme", "--producer", "space:s-42", "--owner", "app:reporter"),
                *("--database-url", admin.url),
            ],
        )
        assert space.exit_code == 0, space.output
        assert json.loads(space.output)["owner"] == "app:reporter"
    finally:
        database.drop()


def _grant_and_revoke(admin: DatabaseUrl) -> None:
    granted = CliRunner().invoke(
        db,
        [
            "grant-producer",
            "--org",
            "acme",
            "--producer",
            "person:ada",
            "--database-url",
            admin.url,
        ],
    )
    assert granted.exit_code == 0, granted.output
    credentials = json.loads(granted.output)
    assert {"role", "password", "database_url"} <= set(credentials)
    engine = create_engine(DatabaseUrl.parse(credentials["database_url"]).sync)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT session_user")).scalar() == credentials["role"]
    finally:
        engine.dispose()
    revoked = CliRunner().invoke(
        db,
        [
            "revoke-producer",
            "--org",
            "acme",
            "--producer",
            "person:ada",
            "--database-url",
            admin.url,
        ],
    )
    assert revoked.output.strip() == "dropped"
