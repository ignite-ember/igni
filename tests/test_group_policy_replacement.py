"""A group's entries replacing what ember ships, and a model per agent.

Two things the server can now say that this client has to honour.

**Replacement.** Overrides used to be purely additive: a group's agents
arrived alongside the shipped ones. A legal team has no use for the
coding agents, so ``exclusive_kinds`` lets a group declare that for a
given kind its list is the whole list. Per-kind, because a group that
replaces the agents usually still wants the MCP servers.

**A model per agent.** The group's default answers "what does this team
use" but not "what does *this* agent use". An entry may name its own
adapter, which we write into the materialised file's frontmatter — the
key the agent loader already reads.

The failure that matters most here is the quiet one: replacement that
does not replace. A group believing the coding agents are gone while
every one of them is still in the pool is worse than an error, so the
tests below check what is *absent*, not only what is present.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from ember_code.core.agents.loader import AgentDefinitionLoader
from ember_code.core.config.group_policy import (
    GroupPolicyCache,
    GroupPolicyOverrideEntry,
    GroupPolicyPack,
)
from ember_code.core.init.group_agent_sync import GroupAgentSync
from ember_code.core.mcp.config import MCPConfigLoader
from ember_code.core.plugins.loader import PluginLoader


def _pack(*, overrides=(), exclusive_kinds=(), default_model=None) -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g-1",
        group_name="Legal",
        fetched_at=datetime.now(timezone.utc),
        overrides=list(overrides),
        exclusive_kinds=list(exclusive_kinds),
        default_model=default_model,
    )


def _agent_entry(
    name: str, *, model: str | None = None, body: str = "Body."
) -> GroupPolicyOverrideEntry:
    return GroupPolicyOverrideEntry(
        kind="agents",
        entry_name=name,
        content=f"---\nname: {name}\ndescription: {name} agent\n---\n{body}",
        content_type="markdown",
        model=model,
    )


def _write_agent(directory: Path, name: str, description: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nBody.\n",
        encoding="utf-8",
    )


@pytest.fixture
def bare_settings():
    """Enough of ``Settings`` for the loader's directory shape."""
    return SimpleNamespace(agents=SimpleNamespace(cross_tool_support=False))


class TestWhatThePackSays:
    def test_a_group_replaces_only_what_it_names(self):
        pack = _pack(exclusive_kinds=["agents"])
        assert pack.replaces("agents")
        assert not pack.replaces("mcps")

    def test_an_ordinary_group_replaces_nothing(self):
        assert not _pack().replaces("agents")

    def test_settings_is_never_replaceable(self):
        """It is a merge tier whose lowest layer is the built-in
        defaults, so honouring this would leave a session with none.
        The server refuses it; a pack from an older deployment might
        still carry it."""
        assert not _pack(exclusive_kinds=["settings"]).replaces("settings")

    def test_an_unknown_kind_degrades_to_additive(self):
        """A newer server naming a kind we do not have should leave this
        client behaving as it always did, not emptying something."""
        assert not _pack(exclusive_kinds=["skills"]).replaces("skills")


class TestWhatSurvivesOnDisk:
    """The loaders read the cache directory, not the pack, so the
    instruction has to survive materialisation."""

    def test_the_replaced_kinds_reach_the_cache(self, tmp_path: Path):
        cache = GroupPolicyCache(cache_dir=tmp_path)
        cache.materialize(_pack(exclusive_kinds=["agents", "mcps"]))

        assert cache.exclusive_kinds() == {"agents", "mcps"}

    def test_settings_is_dropped_on_the_way_in(self, tmp_path: Path):
        cache = GroupPolicyCache(cache_dir=tmp_path)
        cache.materialize(_pack(exclusive_kinds=["agents", "settings"]))

        assert cache.exclusive_kinds() == {"agents"}

    def test_no_pack_replaces_nothing(self, tmp_path: Path):
        """The additive behaviour every session had before, and the safe
        answer when we cannot tell."""
        assert GroupPolicyCache(cache_dir=tmp_path).exclusive_kinds() == set()

    def test_an_unreadable_meta_replaces_nothing(self, tmp_path: Path):
        (tmp_path / "pack_meta.json").write_text("{not json", encoding="utf-8")

        assert GroupPolicyCache(cache_dir=tmp_path).exclusive_kinds() == set()

    def test_the_group_default_is_recorded(self, tmp_path: Path):
        cache = GroupPolicyCache(cache_dir=tmp_path)
        cache.materialize(_pack(default_model="legal-reviewer"))

        assert cache.read_pack_meta()["default_model"] == "legal-reviewer"


class TestTheModelAnAgentRunsAgainst:
    def test_it_lands_in_the_frontmatter(self, tmp_path: Path):
        """Where the agent loader already reads it — no new plumbing."""
        cache = GroupPolicyCache(cache_dir=tmp_path)
        cache.materialize(_pack(overrides=[_agent_entry("contracts", model="legal-reviewer")]))

        written = (cache.agents_dir / "contracts.md").read_text(encoding="utf-8")
        frontmatter = yaml.safe_load(written.split("---")[1])
        assert frontmatter["model"] == "legal-reviewer"

    def test_it_survives_the_round_trip_through_the_loader(self, tmp_path: Path, bare_settings):
        """Cache → sync → project → loader → definition. The whole path,
        because a model that reaches the file and stops there is worth
        nothing."""
        cache = GroupPolicyCache(cache_dir=tmp_path / "group-policy")
        cache.materialize(_pack(overrides=[_agent_entry("contracts", model="legal-reviewer")]))
        project = tmp_path / "proj"
        (project / ".ember").mkdir(parents=True)
        GroupAgentSync(project_dir=project, source_dir=cache.agents_dir).run()

        report = AgentDefinitionLoader(
            settings=bare_settings,
            project_dir=project,
            codeindex_available=False,
            group_agents_only=True,
        ).load()

        assert report.entries["contracts"].definition.model == "legal-reviewer"

    def test_an_agent_naming_none_keeps_its_content_verbatim(self, tmp_path: Path):
        """Absent means "inherit", so there is nothing to write."""
        cache = GroupPolicyCache(cache_dir=tmp_path)
        entry = _agent_entry("general")
        cache.materialize(_pack(overrides=[entry]))

        assert (cache.agents_dir / "general.md").read_text(encoding="utf-8") == entry.content

    def test_the_body_is_not_lost(self, tmp_path: Path):
        """Rewriting frontmatter means re-emitting the file, which is a
        good way to drop the prompt it carries."""
        cache = GroupPolicyCache(cache_dir=tmp_path)
        cache.materialize(
            _pack(overrides=[_agent_entry("contracts", model="m", body="Review the contract.")])
        )

        assert "Review the contract." in (cache.agents_dir / "contracts.md").read_text(
            encoding="utf-8"
        )

    def test_existing_frontmatter_keys_are_kept(self, tmp_path: Path):
        cache = GroupPolicyCache(cache_dir=tmp_path)
        cache.materialize(_pack(overrides=[_agent_entry("contracts", model="m")]))

        written = (cache.agents_dir / "contracts.md").read_text(encoding="utf-8")
        frontmatter = yaml.safe_load(written.split("---")[1])
        assert frontmatter["name"] == "contracts"
        assert frontmatter["description"] == "contracts agent"

    def test_content_we_cannot_parse_is_left_alone(self, tmp_path: Path):
        """It will not load as an agent either way, and the loader
        reports that with the file name — better than writing a file
        nobody wrote."""
        cache = GroupPolicyCache(cache_dir=tmp_path)
        broken = GroupPolicyOverrideEntry(
            kind="agents",
            entry_name="broken",
            content="no frontmatter here",
            content_type="markdown",
            model="legal-reviewer",
        )
        cache.materialize(_pack(overrides=[broken]))

        assert (cache.agents_dir / "broken.md").read_text(encoding="utf-8") == "no frontmatter here"


class TestAgentsThatReplace:
    """Exclusivity narrowed the roots to one: the project's synced
    ``.ember/agents``. Everything else — the user's own
    ``~/.ember/agents``, ``agents.local``, the cross-tool ``.claude``
    roots — drops out. It cannot also drop what is *in* that directory,
    because that is where the group's own agents live now."""

    def test_the_other_roots_are_gone(self, tmp_path: Path, bare_settings):
        project = tmp_path / "proj"
        _write_agent(project / ".ember" / "agents.local", "my-personal", "mine")
        _write_agent(project / ".ember" / "agents", "contract-review", "the group's own")

        report = AgentDefinitionLoader(
            settings=bare_settings,
            project_dir=project,
            codeindex_available=False,
            group_agents_only=True,
        ).load()

        assert set(report.entries) == {"contract-review"}

    def test_the_ordinary_group_still_gets_both(self, tmp_path: Path, bare_settings):
        project = tmp_path / "proj"
        _write_agent(project / ".ember" / "agents.local", "my-personal", "mine")
        _write_agent(project / ".ember" / "agents", "contract-review", "the group's own")

        report = AgentDefinitionLoader(
            settings=bare_settings,
            project_dir=project,
            codeindex_available=False,
        ).load()

        assert {"my-personal", "contract-review"} <= set(report.entries)

    def test_the_coding_agents_go_when_the_group_drops_them(self, tmp_path: Path, bare_settings):
        """The end-to-end point. The sync removes what the group no
        longer ships, so exclusivity is about the other roots — between
        them a legal team sees no coding agents at all."""
        project = tmp_path / "proj"
        (project / ".ember").mkdir(parents=True)
        source = tmp_path / "group-policy" / "agents"
        _write_agent(source, "code-reviewer", "shipped by ember")
        GroupAgentSync(project_dir=project, source_dir=source).run()

        (source / "code-reviewer.md").unlink()
        _write_agent(source, "contract-review", "the group's own")
        GroupAgentSync(project_dir=project, source_dir=source).run()

        report = AgentDefinitionLoader(
            settings=bare_settings,
            project_dir=project,
            codeindex_available=False,
            group_agents_only=True,
        ).load()

        assert set(report.entries) == {"contract-review"}


class TestServersThatReplace:
    def test_the_local_servers_are_gone(self, tmp_path: Path):
        project = tmp_path / "proj"
        project.mkdir()
        (project / ".mcp.json").write_text(
            '{"mcpServers": {"local-thing": {"command": "x"}}}', encoding="utf-8"
        )
        group = tmp_path / "group-policy" / "mcps"
        group.mkdir(parents=True)
        (group / "case-law.json").write_text(
            '{"mcpServers": {"case-law": {"command": "case-law-mcp"}}}', encoding="utf-8"
        )

        servers = MCPConfigLoader(
            project_dir=project, group_mcps_dir=group, group_mcps_only=True
        ).load()

        assert set(servers) == {"case-law"}

    def test_the_ordinary_group_still_gets_both(self, tmp_path: Path):
        project = tmp_path / "proj"
        project.mkdir()
        (project / ".mcp.json").write_text(
            '{"mcpServers": {"local-thing": {"command": "x"}}}', encoding="utf-8"
        )
        group = tmp_path / "group-policy" / "mcps"
        group.mkdir(parents=True)
        (group / "case-law.json").write_text(
            '{"mcpServers": {"case-law": {"command": "case-law-mcp"}}}', encoding="utf-8"
        )

        servers = MCPConfigLoader(project_dir=project, group_mcps_dir=group).load()

        assert {"local-thing", "case-law"} == set(servers)

    def test_a_group_with_no_directory_is_not_emptied(self, tmp_path: Path):
        project = tmp_path / "proj"
        project.mkdir()
        (project / ".mcp.json").write_text(
            '{"mcpServers": {"local-thing": {"command": "x"}}}', encoding="utf-8"
        )

        servers = MCPConfigLoader(project_dir=project, group_mcps_only=True).load()

        assert "local-thing" in servers


class TestPluginsThatReplace:
    @staticmethod
    def _plant(root: Path, name: str) -> None:
        (root / name / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (root / name / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": name}), encoding="utf-8"
        )

    def test_the_local_plugins_are_gone(self, tmp_path: Path):
        project = tmp_path / "proj"
        self._plant(project / ".ember" / "plugins", "local-helper")
        data_dir = tmp_path / "data"
        self._plant(data_dir / "group-policy" / "plugins", "case-law")

        loader = PluginLoader(data_dir=data_dir)
        loader.load_all(project, group_only=True)

        assert [p.name for p in loader.list_plugins()] == ["case-law"]

    def test_the_ordinary_group_still_gets_both(self, tmp_path: Path):
        project = tmp_path / "proj"
        self._plant(project / ".ember" / "plugins", "local-helper")
        data_dir = tmp_path / "data"
        self._plant(data_dir / "group-policy" / "plugins", "case-law")

        loader = PluginLoader(data_dir=data_dir)
        loader.load_all(project)

        assert {"local-helper", "case-law"} <= {p.name for p in loader.list_plugins()}
