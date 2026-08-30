"""A model per agent, carried from the group to the file that runs it.

The group's default answers "what does this team use" but not "what does
*this* agent use" — a legal team plausibly wants contract review on one
adapter and case summaries on another. An entry may name its own, and we
write it into the materialised file's frontmatter, which is the key the
agent loader already reads.

The dangerous failure is a model that reaches the file and stops there,
so one of these follows the whole path: cache → sync → project → loader
→ definition.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from ember_code.core.agents.loader import AgentDefinitionLoader
from ember_code.core.config.group_policy import (
    GroupPolicyCache,
    GroupPolicyEntry,
    GroupPolicyPack,
)
from ember_code.core.init.group_agent_sync import GroupAgentSync


def _pack(*, entries=(), default_model=None) -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g-1",
        group_name="Legal",
        fetched_at=datetime.now(timezone.utc),
        entries=list(entries),
        default_model=default_model,
    )


def _agent_entry(name: str, *, model: str | None = None, body: str = "Body.") -> GroupPolicyEntry:
    return GroupPolicyEntry(
        kind="agents",
        entry_name=name,
        content=f"---\nname: {name}\ndescription: {name} agent\n---\n{body}",
        content_type="markdown",
        model=model,
    )


@pytest.fixture
def bare_settings():
    """Enough of ``Settings`` for the loader's directory shape."""
    return SimpleNamespace(agents=SimpleNamespace(cross_tool_support=False))


class TestWhatSurvivesOnDisk:
    """The loaders read the cache directory, not the pack."""

    def test_the_group_default_is_recorded(self, tmp_path: Path):
        cache = GroupPolicyCache(cache_dir=tmp_path)
        cache.materialize(_pack(default_model="legal-reviewer"))

        assert cache.read_pack_meta()["default_model"] == "legal-reviewer"


class TestTheModelAnAgentRunsAgainst:
    def test_it_lands_in_the_frontmatter(self, tmp_path: Path):
        """Where the agent loader already reads it — no new plumbing."""
        cache = GroupPolicyCache(cache_dir=tmp_path)
        cache.materialize(_pack(entries=[_agent_entry("contracts", model="legal-reviewer")]))

        written = (cache.agents_dir / "contracts.md").read_text(encoding="utf-8")
        frontmatter = yaml.safe_load(written.split("---")[1])
        assert frontmatter["model"] == "legal-reviewer"

    def test_it_survives_the_round_trip_through_the_loader(self, tmp_path: Path, bare_settings):
        """Cache → sync → project → loader → definition. The whole path,
        because a model that reaches the file and stops there is worth
        nothing."""
        cache = GroupPolicyCache(cache_dir=tmp_path / "group-policy")
        cache.materialize(_pack(entries=[_agent_entry("contracts", model="legal-reviewer")]))
        project = tmp_path / "proj"
        (project / ".ember").mkdir(parents=True)
        GroupAgentSync(project_dir=project, source_dir=cache.agents_dir).run()

        report = AgentDefinitionLoader(
            settings=bare_settings,
            project_dir=project,
            codeindex_available=False,
        ).load()

        assert report.entries["contracts"].definition.model == "legal-reviewer"

    def test_an_agent_naming_none_keeps_its_content_verbatim(self, tmp_path: Path):
        """Absent means "inherit", so there is nothing to write."""
        cache = GroupPolicyCache(cache_dir=tmp_path)
        entry = _agent_entry("general")
        cache.materialize(_pack(entries=[entry]))

        assert (cache.agents_dir / "general.md").read_text(encoding="utf-8") == entry.content

    def test_the_body_is_not_lost(self, tmp_path: Path):
        """Rewriting frontmatter means re-emitting the file, which is a
        good way to drop the prompt it carries."""
        cache = GroupPolicyCache(cache_dir=tmp_path)
        cache.materialize(
            _pack(entries=[_agent_entry("contracts", model="m", body="Review the contract.")])
        )

        assert "Review the contract." in (cache.agents_dir / "contracts.md").read_text(
            encoding="utf-8"
        )

    def test_existing_frontmatter_keys_are_kept(self, tmp_path: Path):
        cache = GroupPolicyCache(cache_dir=tmp_path)
        cache.materialize(_pack(entries=[_agent_entry("contracts", model="m")]))

        written = (cache.agents_dir / "contracts.md").read_text(encoding="utf-8")
        frontmatter = yaml.safe_load(written.split("---")[1])
        assert frontmatter["name"] == "contracts"
        assert frontmatter["description"] == "contracts agent"

    def test_content_we_cannot_parse_is_left_alone(self, tmp_path: Path):
        """It will not load as an agent either way, and the loader
        reports that with the file name — better than writing a file
        nobody wrote."""
        cache = GroupPolicyCache(cache_dir=tmp_path)
        broken = GroupPolicyEntry(
            kind="agents",
            entry_name="broken",
            content="no frontmatter here",
            content_type="markdown",
            model="legal-reviewer",
        )
        cache.materialize(_pack(entries=[broken]))

        assert (cache.agents_dir / "broken.md").read_text(encoding="utf-8") == "no frontmatter here"
