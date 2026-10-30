"""Every spelling of a project opens the same ``state.db``.

The backend, ``igni migrate`` and every store name the project's database
through :func:`ember_code.core.data_paths.canonical_project_dir`. Before,
the backend resolved symlinks and ``igni migrate`` did not, so on macOS
``/var/x`` and ``/private/var/x`` hashed to two databases and a volume
prepared by ``igni migrate`` was not the one the backend opened.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from ember_code.core.data_paths import canonical_project_dir, resolve_project_id, state_db_path
from ember_code.core.db.schema import HEAD_REVISION, SchemaRevision


def _project_with_alias(tmp_path: Path) -> tuple[Path, Path]:
    real = tmp_path / "real" / "project"
    real.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real.parent, target_is_directory=True)
    return real, alias / "project"


def test_a_symlinked_spelling_is_the_same_project(tmp_path: Path):
    real, aliased = _project_with_alias(tmp_path)
    assert canonical_project_dir(aliased) == canonical_project_dir(real)
    assert resolve_project_id(aliased) == resolve_project_id(real)
    data = tmp_path / "data"
    assert state_db_path(aliased, data_dir=data) == state_db_path(real, data_dir=data)


def test_a_relative_spelling_is_the_same_project(tmp_path: Path, monkeypatch):
    real, _ = _project_with_alias(tmp_path)
    monkeypatch.chdir(real.parent)
    data = tmp_path / "data"
    assert state_db_path("project", data_dir=data) == state_db_path(real, data_dir=data)
    assert state_db_path("project/", data_dir=data) == state_db_path(real, data_dir=data)


def _env(tmp_path: Path, home: Path, **extra: str) -> dict[str, str]:
    return dict(
        os.environ,
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_DATA_HOME=str(home / ".local" / "share"),
        **extra,
    )


def _project_dbs(home: Path) -> list[Path]:
    return sorted(home.glob(".igni/projects/*/state.db"))


def test_igni_migrate_from_elsewhere_prepares_the_named_projects_database(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    real, aliased = _project_with_alias(tmp_path)
    subprocess.run(
        [sys.executable, "-m", "ember_code", "migrate", "--project-dir", str(aliased)],
        cwd=elsewhere,
        env=_env(tmp_path, home),
        check=True,
        capture_output=True,
    )
    data = home / ".igni"
    assert SchemaRevision.of(state_db_path(real, data_dir=data)) == HEAD_REVISION
    assert _project_dbs(home) == [state_db_path(real, data_dir=data)]


# Started the way the app starts it, from a working directory that is
# not the project; dumps ``sys.modules`` once the test asks.
_RUNNER = textwrap.dedent(
    """
    import json, os, runpy, sys, threading, time

    def dump_when_asked():
        trigger, out = os.environ["DUMP_TRIGGER"], os.environ["DUMP_OUT"]
        while not os.path.exists(trigger):
            time.sleep(0.05)
        with open(out + ".tmp", "w") as f:
            json.dump(sorted(sys.modules), f)
        os.replace(out + ".tmp", out)

    threading.Thread(target=dump_when_asked, daemon=True).start()
    sys.argv = ["ember_code.backend", "--ws-port", "0", "--project-dir", os.environ["PROJECT"]]
    runpy.run_module("ember_code.backend", run_name="__main__", alter_sys=True)
    """
)


def test_the_backend_opens_the_database_igni_migrate_prepared(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    real, aliased = _project_with_alias(tmp_path)
    env = _env(
        tmp_path,
        home,
        PROJECT=str(real),
        DUMP_TRIGGER=str(tmp_path / "dump"),
        DUMP_OUT=str(tmp_path / "modules.json"),
    )
    # Prepared through the alias, started through the real path, from a
    # directory that is neither.
    subprocess.run(
        [sys.executable, "-m", "ember_code", "migrate", "--project-dir", str(aliased)],
        cwd=elsewhere,
        env=env,
        check=True,
        capture_output=True,
    )
    backend = subprocess.Popen(
        [sys.executable, "-c", _RUNNER],
        cwd=elsewhere,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert backend.stdout is not None
        ready = json.loads(backend.stdout.readline())
        assert ready["status"] == "ready"
        time.sleep(1.0)
        (tmp_path / "dump").touch()
        out = tmp_path / "modules.json"
        deadline = time.monotonic() + 15
        while not out.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        modules = json.loads(out.read_text())
    finally:
        backend.kill()
        backend.wait(10)

    data = home / ".igni"
    assert _project_dbs(home) == [state_db_path(real, data_dir=data)], "one project, one database"
    assert [m for m in modules if m.split(".")[0] == "alembic"] == [], (
        "it found its database current"
    )
