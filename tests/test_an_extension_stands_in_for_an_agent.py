"""An extension stands in for one of igni's agents, under igni's name.

``contributes.prompt.agents`` names an agent igni has — ``explorer`` —
and a file the extension's plugin carries. While the extension runs
(and while its condition holds, when it names one) that file *is* the
agent: loaded under the plain name above everything a person or an
organisation wrote, and gone the moment the extension stops.

It replaces a convention that read ``<agent>.<variant>.md`` siblings
by suffix — which meant ``my.agent.md`` was an agent called
``my.agent`` until something named ``agent`` was installed. A name in
a manifest has no such ambiguity.
"""

from __future__ import annotations

from pathlib import Path

from ember_code.core.agents.loader import AgentDefinitionLoader
from ember_code.core.agents.schemas import AgentPriority
from ember_code.core.config.settings import Settings
from ember_code.core.prompts.contributions import (
    PromptClaim,
    PromptClaimKind,
    PromptContributions,
)


def _agent(path: Path, name: str, body: str = "body") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: d\n---\n\n{body}\n", encoding="utf-8")
    return path


def _loader(project: Path, contributions: PromptContributions) -> AgentDefinitionLoader:
    return AgentDefinitionLoader(Settings(), project, contributions=contributions)


def _stand_in(contributions: PromptContributions, path: Path, when: str | None = None) -> None:
    contributions.set_owner(
        "acme.scan",
        "Acme Scan",
        [PromptClaim(kind=PromptClaimKind.AGENT, target="explorer", path=path, when=when)],
    )


class TestStandingIn:
    def test_while_it_runs_its_file_is_the_agent_above_everything(self, tmp_path):
        _agent(tmp_path / ".igni" / "agents" / "explorer.md", "explorer", "the project's")
        contributions = PromptContributions()
        _stand_in(contributions, _agent(tmp_path / "plugin" / "x.md", "whatever", "acme's"))

        report = _loader(tmp_path, contributions).load()
        entry = report.entries["explorer"]
        assert entry.priority is AgentPriority.EXTENSION_OVERRIDE
        assert "acme's" in entry.definition.system_prompt

    def test_when_it_stops_the_agent_is_what_it_was(self, tmp_path):
        _agent(tmp_path / ".igni" / "agents" / "explorer.md", "explorer", "the project's")
        contributions = PromptContributions()
        _stand_in(contributions, _agent(tmp_path / "plugin" / "x.md", "explorer", "acme's"))
        contributions.remove_owner("acme.scan")

        entry = _loader(tmp_path, contributions).load().entries["explorer"]
        assert "the project's" in entry.definition.system_prompt

    def test_under_a_condition_only_while_it_holds(self, tmp_path):
        contributions = PromptContributions()
        _stand_in(contributions, _agent(tmp_path / "plugin" / "x.md", "explorer"), when="covered")
        assert "explorer" not in _loader(tmp_path, contributions).load().entries

        contributions.hold("acme.scan", {"covered": True})
        assert "explorer" in _loader(tmp_path, contributions).load().entries

    def test_it_gets_the_plugin_security_envelope(self, tmp_path):
        """The file is the extension's; a plugin's agent may not grant
        itself what the policy strips."""
        path = tmp_path / "plugin" / "x.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            "---\nname: explorer\ndescription: d\npermissionMode: bypassPermissions\n---\n\nbody\n"
        )
        contributions = PromptContributions()
        _stand_in(contributions, path)

        entry = _loader(tmp_path, contributions).load().entries["explorer"]
        assert entry.definition.force_isolation == "worktree"

    def test_a_file_that_will_not_parse_is_reported_not_loaded(self, tmp_path):
        broken = tmp_path / "plugin" / "x.md"
        broken.parent.mkdir(parents=True)
        broken.write_text("no frontmatter at all")
        contributions = PromptContributions()
        _stand_in(contributions, broken)

        report = _loader(tmp_path, contributions).load()
        assert "explorer" not in report.entries
        assert [e.path for e in report.errors] == [broken]


class TestDotsAreJustNames:
    def test_a_name_with_a_dot_is_a_plain_agent(self, tmp_path):
        """No suffix is special any more."""
        _agent(tmp_path / ".igni" / "agents" / "explorer.codeindex.md", "explorer.codeindex")
        report = _loader(tmp_path, PromptContributions()).load()
        assert "explorer.codeindex" in report.entries

    def test_a_plugins_own_agents_stay_in_its_namespace(self, tmp_path):
        directory = tmp_path / "plugins" / "acme.scan" / "agents"
        _agent(directory / "explorer.md", "explorer")
        loader = _loader(tmp_path, PromptContributions())
        report = loader.load_directory(
            directory, AgentPriority.PROJECT_EMBER, namespace="acme.scan"
        )
        assert list(report.entries) == ["acme.scan:explorer"]
