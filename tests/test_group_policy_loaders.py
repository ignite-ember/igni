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

from datetime import datetime, timezone
from pathlib import Path

from ember_code.core.agents.loader import AgentDefinitionLoader
from ember_code.core.agents.schemas import AgentPriority
from ember_code.core.config.group_policy import (
    GroupPolicyCache,
    GroupPolicyOverrideEntry,
    GroupPolicyPack,
)
from ember_code.core.mcp.config import MCP_PRIORITY_GROUP, MCPConfigLoader

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
        overrides=[
            GroupPolicyOverrideEntry(
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
        overrides=[
            GroupPolicyOverrideEntry(
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


def test_mcp_config_loader_group_dir_overrides_project(tmp_path: Path):
    """Group-policy server wins over a same-named project-local one."""
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

    # The later group scan overwrites project because ``load()``
    # re-assigns in dictionary order.
    assert servers["github"].command == "group-version"


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


def test_mcp_priority_group_constant_matches_agent_priority():
    """The MCP group tier (5) matches AgentPriority.ORG_GROUP (5)."""
    assert AgentPriority.ORG_GROUP.value == MCP_PRIORITY_GROUP


# ---------------------------------------------------------------------------
# AgentDefinitionLoader — group_agents_dir
# ---------------------------------------------------------------------------


def _write_agent(path: Path, name: str, body: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    md = path / f"{name}.md"
    md.write_text(body, encoding="utf-8")
    return md


def test_agent_loader_reads_group_agents_dir(tmp_path: Path):
    """An .md file under group_agents_dir loads with ORG_GROUP priority."""
    project = tmp_path / "proj"
    user_dir = tmp_path / "user-agents"
    user_dir.mkdir()
    group_dir = tmp_path / "group-agents"
    group_dir.mkdir()

    _write_agent(
        user_dir,
        "shared",
        "---\nname: shared\ndescription: user version\n---\nUser body.",
    )
    _write_agent(
        group_dir,
        "shared",
        "---\nname: shared\ndescription: group version\n---\nGroup body.",
    )

    # Bare-bones loader — no real Settings needed for this dir-shape test
    from types import SimpleNamespace

    settings = SimpleNamespace(
        agents=SimpleNamespace(cross_tool_support=False),
    )

    loader = AgentDefinitionLoader(
        settings=settings,
        project_dir=project,
        codeindex_available=False,
        group_agents_dir=group_dir,
    )
    report = loader.load()
    assert "shared" in report.entries
    assert report.entries["shared"].priority == AgentPriority.ORG_GROUP


def test_agent_loader_group_wins_over_user(tmp_path: Path):
    """Group-policy entry beats a same-name user entry on priority."""
    from types import SimpleNamespace

    settings = SimpleNamespace(
        agents=SimpleNamespace(cross_tool_support=False),
    )

    user_dir = tmp_path / "user"
    user_dir.mkdir()
    group_dir = tmp_path / "group"
    group_dir.mkdir()

    _write_agent(
        user_dir,
        "x",
        "---\nname: x\ndescription: user\n---\nuser body",
    )
    _write_agent(
        group_dir,
        "x",
        "---\nname: x\ndescription: group\n---\ngroup body",
    )

    loader = AgentDefinitionLoader(
        settings=settings,
        project_dir=tmp_path,
        codeindex_available=False,
        group_agents_dir=group_dir,
    )
    report = loader.load()
    entry = report.entries["x"]
    assert entry.priority == AgentPriority.ORG_GROUP
    assert "group" in entry.definition.description.lower()


def test_agent_loader_no_group_dir_default(tmp_path: Path):
    """Back-compat — passing no group_agents_dir keeps prior behavior."""
    from types import SimpleNamespace

    settings = SimpleNamespace(
        agents=SimpleNamespace(cross_tool_support=False),
    )

    # No group_agents_dir passed; the loader scans the same roots it
    # always did. The user-homed ``~/.ember/agents`` is one of them,
    # but since we can't write to Path.home() reliably in a test, we
    # only assert the loader runs without error and the ORG_GROUP
    # tier is absent.
    loader = AgentDefinitionLoader(
        settings=settings,
        project_dir=tmp_path,
        codeindex_available=False,
    )
    report = loader.load()
    # No group dir supplied → no ORG_GROUP entries can appear.
    assert all(e.priority != AgentPriority.ORG_GROUP for e in report.entries.values())
