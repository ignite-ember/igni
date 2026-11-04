"""An agent made with ``/agents create`` is kept for the team or for you, or discarded.

The lifecycle, end to end in a git repository:

* **create** — ``.igni/agents.tmp/<name>.md``, ignored by git;
* **promote --project** (the default) — ``.igni/agents/``, committed and
  shared with the team;
* **promote --personal** — ``.igni/agents.local/``, ignored, yours only;
* **discard** — gone.

Promote used to have one target, the project, so the only way to keep an
agent for yourself was to move the file by hand. And it moved over a file
already there, replacing a team's agent of the same name without a word.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ember_code.backend.cmd_agents import AgentsCommand
from ember_code.backend.panels.agents_panel import AgentsPanelController
from ember_code.core.agents import AgentPool
from ember_code.core.agents.schemas import AgentPriority
from ember_code.core.init.project_initializer import ProjectInitializer
from ember_code.core.paths import CONFIG_DIR

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _visible(repo: Path) -> set[str]:
    out = _git(repo, "status", "--porcelain", "--untracked-files=all")
    return {line[3:] for line in out.splitlines()}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    ProjectInitializer(project_dir=repo, home_ember=tmp_path / "home" / CONFIG_DIR).run()
    return repo


@pytest.fixture
def session(repo: Path, settings):
    with patch("ember_code.core.agents.builder.AgentBuilder.build", return_value=MagicMock()):
        pool = AgentPool()
        pool._settings = settings
        pool._base_dir = str(repo)
        pool.init_ephemeral(repo)
        yield SimpleNamespace(pool=pool, project_dir=repo)


def _create(session, name: str) -> None:
    session.pool.register_ephemeral(name=name, description="d", system_prompt="p")


async def test_create_then_promote_for_the_project(session, repo: Path):
    _create(session, "migrator")
    assert f"{CONFIG_DIR}/agents.tmp/migrator.md" not in _visible(repo)

    result = await AgentsCommand(session).dispatch("promote migrator --project")

    assert result.kind == "info", result
    dest = repo / CONFIG_DIR / "agents" / "migrator.md"
    assert dest.exists()
    assert f"{CONFIG_DIR}/agents/migrator.md" in _visible(repo)
    assert session.pool.get_entry("migrator").priority is AgentPriority.PROJECT_EMBER


async def test_promote_defaults_to_the_project(session, repo: Path):
    _create(session, "migrator")
    await AgentsCommand(session).dispatch("promote migrator")
    assert (repo / CONFIG_DIR / "agents" / "migrator.md").exists()


async def test_create_then_keep_it_personal(session, repo: Path):
    _create(session, "mine")

    result = await AgentsCommand(session).dispatch("promote mine --personal")

    assert result.kind == "info", result
    dest = repo / CONFIG_DIR / "agents.local" / "mine.md"
    assert dest.exists()
    assert not (repo / CONFIG_DIR / "agents.tmp" / "mine.md").exists()
    assert not any("mine.md" in path for path in _visible(repo))
    assert session.pool.get_entry("mine").priority is AgentPriority.PROJECT_LOCAL
    assert not session.pool.is_ephemeral(session.pool.get_entry("mine").definition)


async def test_discard_removes_it(session, repo: Path):
    _create(session, "scratch")
    result = await AgentsCommand(session).dispatch("discard scratch")
    assert result.kind == "info"
    assert not (repo / CONFIG_DIR / "agents.tmp" / "scratch.md").exists()


async def test_an_unknown_target_is_refused(session):
    _create(session, "x")
    result = await AgentsCommand(session).dispatch("promote x --team")
    assert result.kind == "error"
    assert "--project" in result.content and "--personal" in result.content


async def test_promote_does_not_replace_an_agent_already_there(session, repo: Path):
    existing = repo / CONFIG_DIR / "agents" / "reviewer.md"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("the team's\n")
    _create_named_file = repo / CONFIG_DIR / "agents.tmp" / "reviewer.md"
    # Ephemeral of the same file name, registered under another name.
    session.pool.register_ephemeral(name="reviewer2", description="d", system_prompt="p")
    (repo / CONFIG_DIR / "agents.tmp" / "reviewer2.md").rename(_create_named_file)
    session.pool.get_entry("reviewer2").definition.source_path = _create_named_file

    result = await AgentsCommand(session).dispatch("promote reviewer2")

    assert result.kind == "error"
    assert "already exists" in result.content
    assert existing.read_text() == "the team's\n"
    assert _create_named_file.exists()


def test_the_panel_offers_both_targets(session, repo: Path):
    _create(session, "a")
    _create(session, "b")
    panel = AgentsPanelController(session)  # type: ignore[arg-type]

    shared = panel.promote("a")
    personal = panel.promote("b", target="personal")

    assert shared.ok and Path(shared.dest) == repo / CONFIG_DIR / "agents" / "a.md"
    assert personal.ok and Path(personal.dest) == repo / CONFIG_DIR / "agents.local" / "b.md"
    assert not panel.promote("a", target="elsewhere").ok
