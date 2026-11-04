"""An extension overrides one of igni's agents, or edits it.

An agent's prompt is its whole ``.md`` file, and its YAML header is a
section like any other (``frontmatter``). An override *is* the agent
while the extension runs (and while its condition holds, when it names
one): above everything a person or an organisation wrote, and gone the
moment the extension stops. An edit lands on whichever definition won —
igni's, the project's, a plugin's.

It replaces a convention that read ``<agent>.<variant>.md`` siblings
by suffix — which meant ``my.agent.md`` was an agent called
``my.agent`` until something named ``agent`` was installed. A target in
a manifest has no such ambiguity.
"""

from __future__ import annotations

from pathlib import Path

from ember_code.core.agents.loader import AgentDefinitionLoader
from ember_code.core.agents.pool import AgentPool
from ember_code.core.agents.schemas import AgentPriority
from ember_code.core.config.settings import Settings
from ember_code.core.prompts.contributions import (
    EditOp,
    PromptContributions,
    PromptEdit,
    PromptOverride,
)
from ember_code.core.prompts.document import Position

PROJECT_S = (
    "---\nname: explorer\ndescription: the project's\n---\n\n"
    "<!-- section:intro -->\nthe project's intro\n<!-- /section:intro -->\n\n"
    "<!-- section:rules -->\nthe project's rules\n<!-- /section:rules -->\n"
)


def _agent(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _pool(project: Path, contributions: PromptContributions) -> AgentPool:
    pool = AgentPool()
    pool.load_definitions(Settings(), project)
    pool.apply_prompt_contributions(contributions)
    return pool


def _override(contributions: PromptContributions, text: str, when: str | None = None) -> None:
    contributions.set_owner(
        "acme.scan",
        "Acme Scan",
        overrides=[PromptOverride(target="agent:explorer", text=text, when=when)],
    )


class TestAnOverride:
    def test_while_it_runs_it_is_the_agent_above_everything(self, tmp_path):
        _agent(tmp_path / ".igni" / "agents" / "explorer.md", PROJECT_S)
        contributions = PromptContributions()
        _override(contributions, "---\nname: whatever\ndescription: d\n---\n\nacme's\n")

        entry = _pool(tmp_path, contributions).get_entry("explorer")
        assert entry.priority is AgentPriority.EXTENSION_OVERRIDE
        assert entry.definition.name == "explorer"
        assert "acme's" in entry.definition.system_prompt

    def test_when_it_stops_the_agent_is_what_it_was(self, tmp_path):
        _agent(tmp_path / ".igni" / "agents" / "explorer.md", PROJECT_S)
        contributions = PromptContributions()
        _override(contributions, "---\nname: explorer\ndescription: d\n---\n\nacme's\n")
        contributions.remove_owner("acme.scan")

        entry = _pool(tmp_path, contributions).get_entry("explorer")
        assert "the project's intro" in entry.definition.system_prompt

    def test_it_defines_an_agent_igni_does_not_have(self, tmp_path):
        contributions = PromptContributions()
        _override(contributions, "---\nname: x\ndescription: d\n---\n\nacme's\n")
        assert _pool(tmp_path, contributions).has_definition("explorer")

    def test_under_a_condition_only_while_it_holds(self, tmp_path):
        contributions = PromptContributions()
        _override(contributions, "---\nname: x\ndescription: d\n---\n\nbody\n", when="covered")
        assert not _pool(tmp_path, contributions).has_definition("explorer")

        contributions.hold("acme.scan", {"covered": True})
        assert _pool(tmp_path, contributions).has_definition("explorer")

    def test_it_gets_the_plugin_security_envelope(self, tmp_path):
        """The header is the extension's; a plugin's agent may not grant
        itself what the policy strips."""
        contributions = PromptContributions()
        _override(
            contributions,
            "---\nname: explorer\ndescription: d\npermissionMode: bypassPermissions\n---\n\nbody\n",
        )
        entry = _pool(tmp_path, contributions).get_entry("explorer")
        assert entry.definition.force_isolation == "worktree"

    def test_one_that_will_not_parse_leaves_the_agent_as_it_was(self, tmp_path):
        _agent(tmp_path / ".igni" / "agents" / "explorer.md", PROJECT_S)
        contributions = PromptContributions()
        _override(contributions, "no frontmatter at all")
        entry = _pool(tmp_path, contributions).get_entry("explorer")
        assert "the project's intro" in entry.definition.system_prompt


class TestAnEdit:
    def test_it_lands_on_whichever_definition_won(self, tmp_path):
        _agent(tmp_path / ".igni" / "agents" / "explorer.md", PROJECT_S)
        contributions = PromptContributions()
        contributions.set_owner(
            "acme.scan",
            "Acme Scan",
            edits=[
                PromptEdit(
                    target="agent:explorer",
                    op=EditOp.INSERT,
                    section="intro",
                    position=Position.AFTER,
                    name="index",
                    text="ask the index",
                ),
                PromptEdit(
                    target="agent:explorer", op=EditOp.REPLACE, section="rules", text="acme's rules"
                ),
            ],
        )
        entry = _pool(tmp_path, contributions).get_entry("explorer")
        prompt = entry.definition.system_prompt
        assert prompt.index("the project's intro") < prompt.index("ask the index")
        assert "acme's rules" in prompt and "the project's rules" not in prompt
        assert entry.priority is AgentPriority.PROJECT_EMBER, "an edit is not an override"
        assert entry.definition.force_isolation is None, "igni's header, igni's envelope"

    def test_a_replaced_frontmatter_is_the_extension_s(self, tmp_path):
        _agent(tmp_path / ".igni" / "agents" / "explorer.md", PROJECT_S)
        contributions = PromptContributions()
        contributions.set_owner(
            "acme.scan",
            "Acme Scan",
            edits=[
                PromptEdit(
                    target="agent:explorer",
                    op=EditOp.REPLACE,
                    section="frontmatter",
                    text="---\nname: explorer\ndescription: acme's explorer\n---",
                )
            ],
        )
        entry = _pool(tmp_path, contributions).get_entry("explorer")
        assert entry.definition.description == "acme's explorer"
        assert entry.definition.force_isolation == "worktree"

    def test_the_model_never_reads_a_section_marker(self, tmp_path):
        _agent(tmp_path / ".igni" / "agents" / "explorer.md", PROJECT_S)
        entry = _pool(tmp_path, PromptContributions()).get_entry("explorer")
        assert "<!--" not in entry.definition.system_prompt


class TestDotsAreJustNames:
    def test_a_name_with_a_dot_is_a_plain_agent(self, tmp_path):
        """No suffix is special any more."""
        _agent(
            tmp_path / ".igni" / "agents" / "explorer.codeindex.md",
            "---\nname: explorer.codeindex\ndescription: d\n---\n\nbody\n",
        )
        report = AgentDefinitionLoader(Settings(), tmp_path).load()
        assert "explorer.codeindex" in report.entries

    def test_a_plugins_own_agents_stay_in_its_namespace(self, tmp_path):
        directory = tmp_path / "plugins" / "acme.scan" / "agents"
        _agent(directory / "explorer.md", "---\nname: explorer\ndescription: d\n---\n\nbody\n")
        loader = AgentDefinitionLoader(Settings(), tmp_path)
        report = loader.load_directory(
            directory, AgentPriority.PROJECT_EMBER, namespace="acme.scan"
        )
        assert list(report.entries) == ["acme.scan:explorer"]
