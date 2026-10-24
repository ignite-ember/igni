"""An agent written for a capability loads when that capability is live.

The convention is older than extensions: an agent ships as a pair,
``<name>.md`` for when nothing special is available and
``<name>.codeindex.md`` for when the index is. The loader read that
suffix literally, in three places.

Once a capability arrives by being installed rather than by being
compiled in, the base cannot know the suffix. So the loader reads two
sets instead — every variant name that could appear, and the subset
that is live — and ``codeindex`` is one name among them, known only
once the extension that declares it is installed. The last class is
what makes an extension able to rewrite an agent it does not own.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember_code.core.agents.loader import AgentDefinitionLoader
from ember_code.core.agents.schemas import AgentPriority
from ember_code.core.config.settings import Settings


def _agents(tmp_path: Path, *names: str) -> list[Path]:
    """Write an agent file per name and hand back the paths."""
    out = []
    for name in names:
        path = tmp_path / name
        path.write_text(
            f"---\nname: {name.split('.')[0]}\ndescription: d\n---\n\nbody\n",
            encoding="utf-8",
        )
        out.append(path)
    return out


def _loader(**over: object) -> AgentDefinitionLoader:
    """A loader that knows the ``codeindex`` variant, live when asked.

    ``codeindex_available`` is shorthand for what an installed CodeIndex
    extension declares: the variant known, and live while it runs.
    """
    live = bool(over.pop("codeindex_available", False))
    over.setdefault("known_variants", {"codeindex"})
    over.setdefault("active_variants", {"codeindex"} if live else set())
    return AgentDefinitionLoader(
        Settings(),
        Path("/tmp"),
        **over,  # type: ignore[arg-type]
    )


def _picked(loader: AgentDefinitionLoader, files: list[Path]) -> set[str]:
    return {f.name for f in loader._pick_variants(files)}


class TestItStillDoesWhatItDid:
    def test_the_index_being_up_prefers_the_codeindex_file(self, tmp_path):
        files = _agents(tmp_path, "explorer.md", "explorer.codeindex.md")
        assert _picked(_loader(codeindex_available=True), files) == {"explorer.codeindex.md"}

    def test_the_index_being_down_prefers_the_plain_file(self, tmp_path):
        files = _agents(tmp_path, "explorer.md", "explorer.codeindex.md")
        assert _picked(_loader(codeindex_available=False), files) == {"explorer.md"}

    def test_half_a_pair_loads_whichever_half_exists(self, tmp_path):
        only_variant = _agents(tmp_path, "explorer.codeindex.md")
        only_plain = _agents(tmp_path, "scout.md")

        assert _picked(_loader(codeindex_available=True), only_variant) == {"explorer.codeindex.md"}
        assert _picked(_loader(codeindex_available=False), only_plain) == {"scout.md"}

    def test_a_variant_for_a_down_capability_is_skipped_entirely(self, tmp_path):
        files = _agents(tmp_path, "explorer.codeindex.md")
        # Not loaded as an agent called "explorer.codeindex" either.
        assert _picked(_loader(codeindex_available=False), files) == set()


class TestWhatTheSuffixCouldNotSay:
    def test_an_extension_variant_wins_when_that_extension_is_live(self, tmp_path):
        files = _agents(tmp_path, "auditor.md", "auditor.acme-scan.md")
        loader = _loader(
            known_variants={"codeindex", "acme-scan"},
            active_variants={"acme-scan"},
        )
        assert _picked(loader, files) == {"auditor.acme-scan.md"}

    def test_the_plain_file_wins_when_that_extension_is_not(self, tmp_path):
        files = _agents(tmp_path, "auditor.md", "auditor.acme-scan.md")
        loader = _loader(known_variants={"codeindex", "acme-scan"}, active_variants=set())
        assert _picked(loader, files) == {"auditor.md"}

    def test_two_extensions_each_get_their_own_agent(self, tmp_path):
        files = _agents(
            tmp_path,
            "auditor.md",
            "auditor.acme-scan.md",
            "explorer.md",
            "explorer.codeindex.md",
        )
        loader = _loader(
            known_variants={"codeindex", "acme-scan"},
            active_variants={"codeindex", "acme-scan"},
        )
        assert _picked(loader, files) == {"auditor.acme-scan.md", "explorer.codeindex.md"}

    def test_a_name_with_a_dot_is_a_plain_agent_until_something_claims_it(self, tmp_path):
        files = _agents(tmp_path, "my.agent.md")

        # Nothing called "agent" is installed, so this is an agent
        # whose name contains a dot.
        assert _picked(_loader(known_variants={"codeindex"}), files) == {"my.agent.md"}

        # Install something called "agent" and the same file is read
        # as a variant — which is the ambiguity in the convention, and
        # the reason variant names should be namespaced.
        claimed = _loader(known_variants={"agent"}, active_variants={"agent"})
        assert _picked(claimed, files) == {"my.agent.md"}
        plain_too = _agents(tmp_path, "my.md")
        assert _picked(claimed, files + plain_too) == {"my.agent.md"}


class TestTwoLiveVariantsOfOneAgent:
    def test_one_is_chosen_and_it_is_the_same_one_every_time(self, tmp_path, caplog):
        files = _agents(tmp_path, "auditor.md", "auditor.acme-scan.md", "auditor.codeindex.md")
        loader = _loader(
            known_variants={"codeindex", "acme-scan"},
            active_variants={"codeindex", "acme-scan"},
        )

        first = _picked(loader, files)
        second = _picked(loader, list(reversed(files)))

        assert len(first) == 1
        assert first == second, "the same install must not load a different agent twice"

    def test_the_conflict_is_logged_with_both_names(self, tmp_path, caplog):
        files = _agents(tmp_path, "auditor.md", "auditor.acme-scan.md", "auditor.codeindex.md")
        loader = _loader(
            known_variants={"codeindex", "acme-scan"},
            active_variants={"codeindex", "acme-scan"},
        )

        with caplog.at_level("WARNING"):
            _picked(loader, files)

        assert "auditor.acme-scan.md" in caplog.text
        assert "auditor.codeindex.md" in caplog.text


@pytest.mark.parametrize("live", [True, False])
def test_an_agent_nobody_wrote_a_variant_for_is_untouched(tmp_path, live):
    files = _agents(tmp_path, "plain.md", "another.md")
    loader = _loader(codeindex_available=live)
    assert _picked(loader, files) == {"plain.md", "another.md"}


class TestAnExtensionsPluginReplacesTheAgentItRewrote:
    """An extension ships ``<agent>.<variant>.md`` in its plugin, without
    the plain ``<agent>.md`` — that one comes from igni, the org's group
    or the project. While the extension runs, its file *is* that agent:
    the same name, not ``<plugin>:<agent>``, and above every file a
    person or an organisation wrote. When it stops, the file is skipped
    and the agent is whatever it was before."""

    def test_while_live_it_loads_under_the_plain_name_above_everything(self, tmp_path):
        files = _agents(tmp_path, "security.codeindex.md")

        report = _loader(codeindex_available=True).load_directory(
            tmp_path, AgentPriority.USER_EMBER, namespace="igni-codeindex"
        )

        assert set(report.entries) == {"security"}, [f.name for f in files]
        assert report.entries["security"].priority == AgentPriority.EXTENSION_OVERRIDE
        assert AgentPriority.EXTENSION_OVERRIDE > AgentPriority.PROJECT_EMBER

    def test_when_the_extension_stops_the_file_is_skipped(self, tmp_path):
        _agents(tmp_path, "security.codeindex.md")

        report = _loader(codeindex_available=False).load_directory(
            tmp_path, AgentPriority.USER_EMBER, namespace="igni-codeindex"
        )

        assert report.entries == {}

    def test_an_agent_only_the_extension_has_exists_only_while_it_runs(self, tmp_path):
        """data-architect has no plain version: without CodeIndex it is
        not an agent at all, rather than one told about an index it
        cannot reach."""
        _agents(tmp_path, "data-architect.codeindex.md")

        live = _loader(codeindex_available=True).load_directory(
            tmp_path, 1, namespace="igni-codeindex"
        )
        down = _loader(codeindex_available=False).load_directory(
            tmp_path, 1, namespace="igni-codeindex"
        )

        assert set(live.entries) == {"data-architect"}
        assert down.entries == {}

    def test_its_own_plain_agents_stay_in_its_namespace(self, tmp_path):
        _agents(tmp_path, "indexer.md")

        report = _loader(codeindex_available=True).load_directory(
            tmp_path, 1, namespace="igni-codeindex"
        )

        assert set(report.entries) == {"igni-codeindex:indexer"}
