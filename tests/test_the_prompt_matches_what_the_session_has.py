"""The prompt promises a document store only when there is one.

A document store is an extension, and it switches off like CodeIndex
does. While it is READY it declares ``project-documents`` and supplies
the ``PROJECT_DOCUMENTS_*`` anchors — the text about the store and the
agent that keeps it. When it is not, a prompt telling the orchestrator
to hand project knowledge to that agent describes a capability the
session does not have.

That defect has shipped twice already. ``knowledge_*`` was taught in
7,542 system prompts against zero manifests and called zero times, and
when the versioned store replaced the knowledge base, the section was
rewritten to ``artifact_add`` — a tool that exists only as a backend
RPC for the UI's own button. Same mistake, new spelling, and the first
one cost 7,875 repaired prompts in the training corpus.

So these tests check both directions, because only checking the
present branch is how the absent branch rots:

* documents ON  → the extension's delegation text is there, the absent notice is not
* documents OFF → the absent notice is there, and nothing tells the agent to delegate

The absent branch is deliberately not empty. A missing capability is
not only a deletion: the agent still has to know where a project
convention goes when there is nowhere to file one, and that it must
not offer to record what it cannot record.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import ember_code
from ember_code.core.prompts import fragments, load_prompt
from ember_code.core.prompts.conditionals import ConditionalRegistry
from ember_code.core.session.agent_builder.prompt_builder import PromptBuilder

#: A stand-in for the extension that keeps the documents. igni's prompts
#: name none, so neither does this.
_DOCS = "acme.docs"

_ANCHORS = {
    "PROJECT_DOCUMENTS_PERSISTING": (
        '- **`spawn_agent("docs-keeper", ...)`** — hand it to the docs keeper.'
    ),
    "PROJECT_DOCUMENTS_WHEN_TO_PERSIST": "- A root cause → the docs keeper.",
    "PROJECT_DOCUMENTS_READING": "Ask the docs keeper why.",
    "PROJECT_DOCUMENTS_REFERENCE": "## The Project's Documents\n\nDelegate to the docs keeper.",
}


@pytest.fixture(autouse=True)
def _documents_text() -> Iterator[None]:
    for anchor, text in _ANCHORS.items():
        assert fragments.register(anchor, _DOCS, lambda text=text: text) is None
    yield
    fragments.remove_owner(_DOCS)


def _with_documents(live: bool) -> ConditionalRegistry:
    """A registry in which an installed extension keeps the project's
    documents — declares ``project-documents`` — or nobody does."""
    registry = ConditionalRegistry()
    if live:
        registry.declare(_DOCS, {"project-documents", "can-persist"})
    return registry


def _loader(name: str) -> str:
    """The prompt as a session loads it: anchors filled by their suppliers."""
    return load_prompt(name)


def _render(*, codeindex: bool, artifacts: bool) -> str:
    builder = PromptBuilder(
        project_dir=Path("."),
        settings=None,
        codeindex_available=codeindex,
        conditionals=_with_documents(artifacts),
        agent_catalog="(catalog)",
        skill_descriptions="",
        prompt_loader=_loader,
    )
    return builder.resolve_conditionals(builder.substitute_placeholders(builder.load_base()))


COMBINATIONS = [
    pytest.param(ci, art, id=f"codeindex={ci}-documents={art}")
    for ci in (False, True)
    for art in (False, True)
]


class TestEveryCombinationRenders:
    @pytest.mark.parametrize(("codeindex", "artifacts"), COMBINATIONS)
    def test_no_placeholder_survives(self, codeindex, artifacts):
        """An unsubstituted ``{{…}}`` reaches the model verbatim."""
        assert "{{" not in _render(codeindex=codeindex, artifacts=artifacts)

    @pytest.mark.parametrize(("codeindex", "artifacts"), COMBINATIONS)
    def test_the_prompt_is_not_truncated(self, codeindex, artifacts):
        """Guards the extraction that created these sections: a bad
        slice would silently drop everything after the heading."""
        rendered = _render(codeindex=codeindex, artifacts=artifacts)

        assert len(rendered) > 20_000
        assert "## Safety" in rendered


class TestWithTheStore:
    @pytest.mark.parametrize("codeindex", [False, True])
    def test_it_says_to_delegate(self, codeindex):
        rendered = _render(codeindex=codeindex, artifacts=True)

        assert 'spawn_agent("docs-keeper"' in rendered

    @pytest.mark.parametrize("codeindex", [False, True])
    def test_it_does_not_also_say_there_is_nowhere_to_file(self, codeindex):
        """Both branches at once would be worse than either."""
        assert "nowhere to file" not in _render(codeindex=codeindex, artifacts=True)


class TestWithoutTheStore:
    @pytest.mark.parametrize("codeindex", [False, True])
    def test_nothing_tells_the_agent_to_delegate(self, codeindex):
        """Nothing of the extension's text survives without it, and igni's
        own text names no agent to delegate to."""
        rendered = _render(codeindex=codeindex, artifacts=False)

        assert "docs keeper" not in rendered and "docs-keeper" not in rendered
        assert "archivist" not in rendered.lower()

    @pytest.mark.parametrize("codeindex", [False, True])
    def test_it_says_where_project_knowledge_goes_instead(self, codeindex):
        """Not an empty string — the deletion is only half the answer."""
        rendered = _render(codeindex=codeindex, artifacts=False)

        assert "nowhere to file project knowledge" in rendered

    @pytest.mark.parametrize("codeindex", [False, True])
    def test_user_memory_still_works_and_is_not_a_substitute(self, codeindex):
        """The tempting wrong fix: file the codebase convention under
        the person who mentioned it."""
        rendered = _render(codeindex=codeindex, artifacts=False)

        assert "update_user_memory" in rendered
        assert "Do not put project knowledge into user memory" in rendered


class TestNoDeadToolSurvivesInEitherBranch:
    """Every spelling this section has had, in every render."""

    @pytest.mark.parametrize(("codeindex", "artifacts"), COMBINATIONS)
    @pytest.mark.parametrize(
        "dead",
        [
            "knowledge_add",
            "knowledge_search",
            "knowledge_status",
            "knowledge_delete",
            "artifact_add",
        ],
    )
    def test_it_is_gone(self, codeindex, artifacts, dead):
        assert dead not in _render(codeindex=codeindex, artifacts=artifacts)

    @pytest.mark.parametrize(("codeindex", "artifacts"), COMBINATIONS)
    def test_the_orchestrator_is_never_given_an_artifact_tool(self, codeindex, artifacts):
        """It holds none in any configuration — one grant site, keyed
        on the agent's name. Naming one as callable is the defect."""
        rendered = _render(codeindex=codeindex, artifacts=artifacts)

        for line in rendered.splitlines():
            if "`artifact_" in line:
                assert "docs keeper" in rendered[: rendered.index(line)][-4000:], (
                    f"an artifact tool is named outside the archivist's context: {line[:120]}"
                )


class TestTheSwitchIsTheOneTheExtensionDeclares:
    def test_the_coordinator_does_not_decide_it(self):
        """Whether the store is there is the extension's to say, by
        declaring ``project-documents`` while it runs. A host flag — a settings
        read, a check on a manager the session holds — could disagree
        with the extension, and would be igni naming one extension."""
        from pathlib import Path as _Path

        source = (
            _Path(ember_code.__file__).parent
            / "core"
            / "session"
            / "agent_builder"
            / "coordinator.py"
        ).read_text(encoding="utf-8")

        assert "artifacts" not in source

    def test_a_missing_section_file_omits_rather_than_promises(self):
        """Degrading toward silence, not toward a false promise."""
        builder = PromptBuilder(
            project_dir=Path("."),
            settings=None,
            codeindex_available=False,
            conditionals=_with_documents(True),
            agent_catalog="x",
            skill_descriptions="",
            prompt_loader=lambda name: (_ for _ in ()).throw(FileNotFoundError(name)),
        )

        assert builder._load_section("persisting") == ""


class TestUnbalancedMarkersRaise:
    """A prompt that silently loses a section is the failure the
    resolver exists to prevent — so a missing close tag has to be loud,
    not a quietly shorter prompt."""

    @pytest.mark.parametrize("artifacts", [False, True])
    def test_an_unclosed_span_is_rejected(self, artifacts):
        builder = PromptBuilder(
            project_dir=Path("."),
            settings=None,
            codeindex_available=False,
            conditionals=_with_documents(artifacts),
            agent_catalog="x",
            skill_descriptions="",
            prompt_loader=_loader,
        )

        with pytest.raises(ValueError, match="unbalanced"):
            builder.resolve_conditionals("<!-- project-documents:only -->orphan")

    def test_a_balanced_span_round_trips(self):
        builder = PromptBuilder(
            project_dir=Path("."),
            settings=None,
            codeindex_available=False,
            conditionals=_with_documents(True),
            agent_catalog="x",
            skill_descriptions="",
            prompt_loader=_loader,
        )
        text = (
            "a <!-- project-documents:only -->b<!-- /project-documents:only -->"
            "<!-- project-documents:absent -->c<!-- /project-documents:absent --> d"
        )

        assert builder.resolve_conditionals(text) == "a b d"

    def test_the_other_branch_is_kept_when_absent(self):
        builder = PromptBuilder(
            project_dir=Path("."),
            settings=None,
            codeindex_available=False,
            conditionals=_with_documents(False),
            agent_catalog="x",
            skill_descriptions="",
            prompt_loader=_loader,
        )
        text = (
            "a <!-- project-documents:only -->b<!-- /project-documents:only -->"
            "<!-- project-documents:absent -->c<!-- /project-documents:absent --> d"
        )

        assert builder.resolve_conditionals(text) == "a c d"

    def test_every_declared_conditional_has_a_capability(self):
        """``_CONDITIONALS`` and ``_capabilities()`` are two lists that
        must agree; a name in one and not the other is a KeyError at
        render time, on a real session."""
        builder = PromptBuilder(
            project_dir=Path("."),
            settings=None,
            codeindex_available=False,
            conditionals=_with_documents(True),
            agent_catalog="x",
            skill_descriptions="",
            prompt_loader=_loader,
        )

        assert set(PromptBuilder._CONDITIONALS) <= set(builder._capabilities())
