"""Tests for the ORG_GROUP tier wiring on agents, MCPs, and the cache.

Three surfaces covered:

  * ``GroupPolicyCache.materialize`` wraps per-server MCP overrides in
    the ``{mcpServers: {...}}`` envelope so :class:`MCPConfigLoader` can
    pick them up via the new ``group_mcps_dir`` parameter.

  * :class:`MCPConfigLoader` with ``group_mcps_dir`` reads each
    ``<name>.json`` file under it and merges the servers with
    ``source='group-policy'`` so ORG-pushed MCP servers beat
    user/project-local copies.

  * :class:`AgentDefinitionLoader` with ``group_agents_dir`` picks up
    ``.md`` files with :attr:`AgentPriority.ORG_GROUP`, beating the
    user/project tiers.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ember_code.core.agents.loader import AgentDefinitionLoader
from ember_code.core.agents.schemas import AgentPriority
from ember_code.core.config.group_policy import (
    GroupPolicyCache,
    GroupPolicyEntry,
    GroupPolicyPack,
)
from ember_code.core.mcp.config import MCPConfigLoader
from ember_code.core.paths import CONFIG_DIR

# ---------------------------------------------------------------------------
# Cache → MCP file format
# ---------------------------------------------------------------------------


def test_materialize_wraps_mcps_in_envelope(tmp_path: Path):
    """Per-server MCP entries get wrapped so MCPConfigLoader can read them."""
    cache = GroupPolicyCache(cache_dir=tmp_path)
    pack = GroupPolicyPack(
        group_id="g-1",
        group_name="Engineering",
        fetched_at=datetime.now(timezone.utc),
        entries=[
            GroupPolicyEntry(
                kind="mcps",
                entry_name="github",
                content=('{"command": "gh-mcp", "args": ["serve"], "env": {}}'),
                content_type="json",
                enabled=True,
            ),
        ],
    )
    cache.materialize(pack)

    on_disk = cache.mcps_dir / "github.json"
    assert on_disk.exists()
    import json

    blob = json.loads(on_disk.read_text(encoding="utf-8"))
    # Envelope shape, not raw MCPServerConfig
    assert "mcpServers" in blob
    assert "github" in blob["mcpServers"]
    assert blob["mcpServers"]["github"]["command"] == "gh-mcp"


def test_materialize_strips_disabled_mcp(tmp_path: Path):
    """An override that's ``enabled=False`` should not be written."""
    cache = GroupPolicyCache(cache_dir=tmp_path)
    pack = GroupPolicyPack(
        group_id="g-1",
        group_name="Engineering",
        fetched_at=datetime.now(timezone.utc),
        entries=[
            GroupPolicyEntry(
                kind="mcps",
                entry_name="off",
                content='{"command": "off"}',
                content_type="json",
                enabled=False,
            ),
        ],
    )
    cache.materialize(pack)
    assert not (cache.mcps_dir / "off.json").exists()


# ---------------------------------------------------------------------------
# MCPConfigLoader — group_mcps_dir
# ---------------------------------------------------------------------------


def test_mcp_config_loader_scans_group_dir(tmp_path: Path):
    """A per-server JSON file under group_mcps_dir shows up in load()."""
    group = tmp_path / "group-policy" / "mcps"
    group.mkdir(parents=True)
    (group / "github.json").write_text(
        '{"mcpServers": {"github": {"command": "gh", "args": [], "env": {}}}}',
        encoding="utf-8",
    )

    loader = MCPConfigLoader(project_dir=tmp_path / "proj", group_mcps_dir=group)
    servers = loader.load()

    assert "github" in servers
    assert servers["github"].command == "gh"
    assert servers["github"].source == "group-policy"


def test_mcp_config_loader_project_overrides_group_dir(tmp_path: Path):
    """A same-named project server wins over the group's.

    This asserted the opposite until the group tier was moved below the
    project's, for consistency with agents, skills, commands, output
    styles and workflows: an org sets the baseline and a repository can
    override one server by name.
    """
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".mcp.json").write_text(
        '{"mcpServers": {"github": {"command": "project-version"}}}',
        encoding="utf-8",
    )

    group = tmp_path / "group-policy" / "mcps"
    group.mkdir(parents=True)
    (group / "github.json").write_text(
        '{"mcpServers": {"github": {"command": "group-version"}}}',
        encoding="utf-8",
    )

    loader = MCPConfigLoader(project_dir=project, group_mcps_dir=group)
    servers = loader.load()

    # ``load()`` re-assigns in scan order, and the project's roots are
    # scanned after the group's.
    assert servers["github"].command == "project-version"


def test_mcp_config_loader_without_group_dir_unchanged(tmp_path: Path):
    """Back-compat — passing no group_mcps_dir skips the new tier."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".mcp.json").write_text(
        '{"mcpServers": {"solo": {"command": "x"}}}', encoding="utf-8"
    )
    # Sanity: the file in group dir must NOT be picked up when the
    # param is omitted.
    stray = tmp_path / "stray-group"
    stray.mkdir()
    (stray / "extra.json").write_text(
        '{"mcpServers": {"extra": {"command": "y"}}}', encoding="utf-8"
    )

    loader = MCPConfigLoader(project_dir=project)
    servers = loader.load()
    assert "solo" in servers
    assert "extra" not in servers


def test_the_project_now_outranks_the_group_for_agents():
    """The change this half of the work is for.

    ``ORG_GROUP`` used to be the highest tier, on the reasoning that a
    group's agent is the one that runs. The project outranks it now, so
    a repository can override one agent by name — which is only
    meaningful because the group's copy is read from the policy cache
    rather than written into the project.
    """
    assert AgentPriority.ORG_GROUP < AgentPriority.PROJECT_EMBER
    assert AgentPriority.ORG_GROUP < AgentPriority.PROJECT_LOCAL
    assert AgentPriority.ORG_GROUP < AgentPriority.PROJECT_CLAUDE
    # Still above the user's own globals: a group is a deliberate
    # decision by an organisation, a stray file in ``~`` is not.
    assert AgentPriority.ORG_GROUP > AgentPriority.USER_EMBER


def test_the_project_overrides_a_group_mcp_server(tmp_path: Path):
    """MCP servers rank like everything else a group ships.

    The group's used to be layered last and win outright; a repository
    can now override one by name. Asserted by behaviour rather than by a
    priority constant, because the ordering here is re-assignment order
    in ``load`` and there is no number to compare.
    """
    project = tmp_path / "proj"
    (project / CONFIG_DIR).mkdir(parents=True)
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"gateway": {"command": "project-binary"}}})
    )
    group_dir = tmp_path / "group" / "mcps"
    group_dir.mkdir(parents=True)
    # Same shape as a project ``.mcp.json`` — the loader reads the
    # ``mcpServers`` wrapper regardless of which tier the file came from.
    (group_dir / "gateway.json").write_text(
        json.dumps({"mcpServers": {"gateway": {"command": "org-approved-binary"}}})
    )

    servers = MCPConfigLoader(project_dir=project, group_mcps_dir=group_dir).load()
    assert servers["gateway"].command == "project-binary"


def test_a_group_mcp_server_still_loads_when_the_project_is_silent(tmp_path: Path):
    """Overriding by name must not mean the group is ignored."""
    project = tmp_path / "proj"
    (project / CONFIG_DIR).mkdir(parents=True)
    group_dir = tmp_path / "group" / "mcps"
    group_dir.mkdir(parents=True)
    (group_dir / "gateway.json").write_text(
        json.dumps({"mcpServers": {"gateway": {"command": "org-approved-binary"}}})
    )

    servers = MCPConfigLoader(project_dir=project, group_mcps_dir=group_dir).load()
    assert servers["gateway"].command == "org-approved-binary"
    assert servers["gateway"].source == "group-policy"


def test_a_group_mcp_server_beats_the_user_home_config(tmp_path: Path, monkeypatch):
    """Still above the user's own, as with every other kind."""
    home = tmp_path / "home"
    (home / CONFIG_DIR).mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    (home / CONFIG_DIR / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"gateway": {"command": "my-own-binary"}}})
    )
    project = tmp_path / "proj"
    (project / CONFIG_DIR).mkdir(parents=True)
    group_dir = tmp_path / "group" / "mcps"
    group_dir.mkdir(parents=True)
    (group_dir / "gateway.json").write_text(
        json.dumps({"mcpServers": {"gateway": {"command": "org-approved-binary"}}})
    )

    servers = MCPConfigLoader(project_dir=project, group_mcps_dir=group_dir).load()
    assert servers["gateway"].command == "org-approved-binary"


# ---------------------------------------------------------------------------
# AgentDefinitionLoader — where a group's agents are read from
#
# The policy cache is a root of its own again, ranked above the user's
# globals and below anything the project declares. Nothing of the
# server's is written into the project, so "the local one wins" comes
# out of the ordering rather than out of there being only one file.
#
# It was the other way round for a while: the group's agents were copied
# into ``<project>/.igni/agents`` by a sync so a person could edit one,
# and reading the cache as well would have shadowed that edit. Reading
# the cache directly gets the same outcome, leaves the repository holding
# only what the repository declares, and removed the sync along with the
# checksums and conflict prompts it needed.
# ---------------------------------------------------------------------------


def _write_agent(path: Path, name: str, body: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    md = path / f"{name}.md"
    md.write_text(body, encoding="utf-8")
    return md


def _bare_settings():
    from types import SimpleNamespace

    return SimpleNamespace(agents=SimpleNamespace(cross_tool_support=False))


def test_agent_loader_reads_the_project_dir(tmp_path: Path):
    """A project's own agents load at the project tier."""
    project = tmp_path / "proj"
    _write_agent(
        project / CONFIG_DIR / "agents",
        "contract-review",
        "---\nname: contract-review\ndescription: the group's own\n---\nBody.",
    )

    report = AgentDefinitionLoader(
        settings=_bare_settings(),
        project_dir=project,
        codeindex_available=False,
    ).load()

    assert report.entries["contract-review"].priority == AgentPriority.PROJECT_EMBER


def test_the_policy_cache_is_an_agent_root(tmp_path: Path):
    """An agent only the group ships still loads.

    Nothing copies it into the project any more, so if the cache were
    not a root the group would simply have no agents.
    """
    project = tmp_path / "proj"
    cache = tmp_path / "group-policy" / "agents"
    _write_agent(
        cache,
        "only-in-the-cache",
        "---\nname: only-in-the-cache\ndescription: d\n---\nBody.",
    )

    report = AgentDefinitionLoader(
        settings=_bare_settings(),
        project_dir=project,
        codeindex_available=False,
        group_dir=cache,
    ).load()

    assert "only-in-the-cache" in report.entries
    assert report.entries["only-in-the-cache"].priority == AgentPriority.ORG_GROUP


def test_the_cache_is_not_read_unless_it_is_passed(tmp_path: Path):
    """No implicit path — the session decides where the pack lives, and
    a loader that guessed would read a directory nobody asked for."""
    project = tmp_path / "proj"
    cache = tmp_path / "group-policy" / "agents"
    _write_agent(cache, "unasked", "---\nname: unasked\ndescription: d\n---\nBody.")

    report = AgentDefinitionLoader(
        settings=_bare_settings(),
        project_dir=project,
        codeindex_available=False,
    ).load()

    assert "unasked" not in report.entries


def test_the_project_wins_a_name_collision(tmp_path: Path):
    """The reason for all of the above.

    Both roots are read, so the outcome is decided by priority rather
    than by one of them being absent.
    """
    project = tmp_path / "proj"
    cache = tmp_path / "group-policy" / "agents"
    _write_agent(
        project / CONFIG_DIR / "agents",
        "reviewer",
        "---\nname: reviewer\ndescription: the project version\n---\nBody.",
    )
    _write_agent(
        cache,
        "reviewer",
        "---\nname: reviewer\ndescription: the group version\n---\nBody.",
    )

    report = AgentDefinitionLoader(
        settings=_bare_settings(),
        project_dir=project,
        codeindex_available=False,
        group_dir=cache,
    ).load()

    assert "project version" in report.entries["reviewer"].definition.description
    assert report.entries["reviewer"].priority == AgentPriority.PROJECT_EMBER


def test_the_group_beats_the_user_globals(tmp_path: Path, monkeypatch):
    """A group is a deliberate decision by an organisation; a file left
    in ``~`` is not."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    project = tmp_path / "proj"
    cache = tmp_path / "group-policy" / "agents"
    _write_agent(
        home / CONFIG_DIR / "agents",
        "reviewer",
        "---\nname: reviewer\ndescription: my personal one\n---\nBody.",
    )
    _write_agent(
        cache,
        "reviewer",
        "---\nname: reviewer\ndescription: the group version\n---\nBody.",
    )

    report = AgentDefinitionLoader(
        settings=_bare_settings(),
        project_dir=project,
        codeindex_available=False,
        group_dir=cache,
    ).load()

    assert "group version" in report.entries["reviewer"].definition.description
