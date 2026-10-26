"""Shared test fixtures."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from dotenv import load_dotenv

from ember_code.core.config.schemas.storage_and_paths import StorageConfig
from ember_code.core.config.settings import load_settings
from ember_code.core.db.scope import tenancy
from ember_code.core.db.urls import DatabaseUrl, Dialect
from ember_code.core.hooks.extension_gate import ExtensionHookGate
from ember_code.core.paths import CONFIG_DIR
from ember_code.extensions.faults import ExtensionFaults
from ember_code.extensions.host.hooks import ExtensionHooks
from ember_code.extensions.host.processes import ExtensionProcesses
from tests.storage_backends import ThrowawayPostgres

# Load environment variables from .env at the repo root before any test
# runs. This lets developers keep credentials for live tests
# (IGNI_TEST_LLM_API_KEY, etc.) in .env instead of exporting them per
# shell. ``override=False`` so an explicit env var still wins over .env.
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
if _ENV_FILE.is_file():
    load_dotenv(_ENV_FILE, override=False)


@pytest.fixture(autouse=True)
def _isolate_user_settings(tmp_path_factory, monkeypatch):
    """Globally redirect ``Path.home()``, ``Path.cwd()``, and the
    ``HOME`` env var to temp directories so ``load_settings()``
    can't read the developer's real ``~/.igni/settings.json`` or
    the project's ``.igni/settings.local.json``. Both files
    contain real permission rules (deny/ask/allow) the developer
    set on their machine (e.g. ``Bash(echo *PERM_TEST_BLOCKED*)``
    from the row-9 walkthrough, ``ask: [Edit]`` from an /accept
    session) that otherwise leak into every test calling
    ``load_settings`` without an explicit ``project_dir``. The
    ``HOME`` env override covers code paths that use
    ``os.path.expanduser`` (auth credentials path, etc.) instead
    of ``Path.home`` so the two stay consistent. Tests that need
    specific contents write files under the tmp home / tmp cwd
    themselves; the default state is empty.
    """
    fake_home = tmp_path_factory.mktemp("home")
    fake_cwd = tmp_path_factory.mktemp("cwd")
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    monkeypatch.setattr("pathlib.Path.cwd", lambda: fake_cwd)
    monkeypatch.setenv("HOME", str(fake_home))


@pytest.fixture(autouse=True)
def _desktop_storage(monkeypatch):
    """The suite runs against the desktop's SQLite files, whatever the
    shell says: a developer's ``IGNI_DATABASE_URL`` must not point every
    store at a shared database. The PostgreSQL tests name theirs
    explicitly (``IGNI_TEST_DATABASE_URL``, ``tests/test_storage_backends.py``)."""
    monkeypatch.delenv(StorageConfig.DATABASE_URL_ENV, raising=False)
    yield
    tenancy.use(None)


#: Names a PostgreSQL server the storage tests may create and drop databases on.
POSTGRES_TEST_URL_ENV = "IGNI_TEST_DATABASE_URL"


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """A throwaway PostgreSQL database for the run, or a skip saying why not."""
    server = os.environ.get(POSTGRES_TEST_URL_ENV, "").strip()
    if not server:
        pytest.skip(
            f"{POSTGRES_TEST_URL_ENV} is unset: the PostgreSQL storage tests need a server "
            "they may create and drop databases on (tests/storage_backends.py has a "
            "docker one-liner)"
        )
    database = ThrowawayPostgres(server)
    url = database.create()
    try:
        yield url
    finally:
        database.drop()


@pytest.fixture(params=[Dialect.SQLITE, Dialect.POSTGRESQL], ids=lambda d: d.value)
def database_url(request, tmp_path: Path) -> DatabaseUrl:
    """A shared database, on each dialect (``tests/storage_backends.py``)."""
    if request.param is Dialect.POSTGRESQL:
        return DatabaseUrl.parse(request.getfixturevalue("postgres_url"))
    return DatabaseUrl.for_file(tmp_path / "shared.db")


@pytest.fixture(autouse=True)
def _no_extension_hooks_left_behind():
    """Each test starts with no extension hooking anything.

    The extensions' hooks and the core's gate are one per process, as in
    the app; a test that installs a hooking extension must not leave
    every later test's tool calls waiting on a process that is not there.
    """
    yield
    ExtensionHookGate.install(None)
    ExtensionHooks.reset_shared()
    ExtensionFaults.reset_shared()
    ExtensionProcesses.reset_shared()


@pytest.fixture
def tmp_dir(tmp_path):
    """Provide a temporary directory as Path."""
    return tmp_path


@pytest.fixture
def settings():
    """Settings instance with a test-safe model registry.

    The package no longer ships a hardcoded model (hosted models come
    from cloud discovery on session start). The fixture builds a
    minimal openai_like entry so tests can resolve a model without
    optional provider packages and without needing a real cloud
    connection.
    """
    s = load_settings()
    s.models.registry["MiniMax-M2.7"] = {
        "provider": "openai_like",
        "model_id": "MiniMaxAI/MiniMax-M2.7",
        "url": "https://api.ignite-ember.sh/v1",
        "api_key": "cloud_token",
        "context_window": 204_800,
        "vision": False,
    }
    s.models.default = "MiniMax-M2.7"
    return s


@pytest.fixture
def project_dir(tmp_path):
    """Temporary project directory with .igni/ structure."""
    ember_dir = tmp_path / CONFIG_DIR
    ember_dir.mkdir()
    (ember_dir / "agents").mkdir()
    (ember_dir / "skills").mkdir()
    return tmp_path


@pytest.fixture
def sample_agent_md(tmp_path):
    """Create a sample agent .md file and return its path."""
    md = tmp_path / "test-agent.md"
    md.write_text(
        "---\n"
        "name: test-agent\n"
        "description: A test agent\n"
        "tools: Read, Grep\n"
        "model: MiniMax-M2.7\n"
        "tags: test, example\n"
        "reasoning: true\n"
        "reasoning_min_steps: 2\n"
        "reasoning_max_steps: 8\n"
        "---\n"
        "You are a test agent. Do test things.\n"
    )
    return md


@pytest.fixture
def sample_skill_md(tmp_path):
    """Create a sample SKILL.md file and return its path."""
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\n"
        "name: test-skill\n"
        "description: A test skill\n"
        "argument-hint: <arg>\n"
        "---\n"
        "Do something with $ARGUMENTS\n"
    )
    return skill_file
