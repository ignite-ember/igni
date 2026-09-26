"""A revision inherits the lineage of the version it replaces.

Derivation edges hang off a *version*, not off the document. So a
revision starts with none, and until this was fixed, a declaration
that simply forgot to re-state a source silently dropped it.

That produced the exact outcome the subsystem exists to prevent. Ask
an agent to fix a document that is out of date against its source and
let it reach for ``artifact_revise``: the text changes, the stale
warning disappears, and it disappears because the dependency was
erased rather than reconciled. The document then reports itself
healthy while standing on a version of a source nobody checked it
against. Measured on the archivist eval fixture before the fix —
Login flow v1 carried two confirmed sources, v2 carried none, and the
stale row went with them.

Inheritance is deliberately not a resolution: the pin comes across at
the version it was already on, so an inherited stale edge is still
stale. Only naming the source at its current version moves it, which
is what ``artifact_reconcile`` and an explicit declaration both do.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ember_code.core.artifacts.index import ArtifactIndex
from ember_code.core.artifacts.metadata_codec import ArtifactMetadataCodec
from ember_code.core.artifacts.models import EdgeState, SourceDeclaration, VersionPin
from evals.archivist.setup import build_seeded_index

CODEC = ArtifactMetadataCodec()
AUTH = CODEC.artifact_id("Auth model")


@pytest.fixture
async def index(tmp_path: Path) -> ArtifactIndex:
    """The archivist fixture: Login flow pinned to Auth model@1, now at @3."""
    return await build_seeded_index(tmp_path)


def sources_of(lineage) -> list[tuple[str, int, str]]:
    return sorted((e.to_name, e.pinned_n, e.state.value) for e in lineage.sources)


async def stale_names(index: ArtifactIndex) -> list[str]:
    return sorted(r.dependent_name for r in await index.stale())


class TestForgettingASourceDoesNotDropIt:
    async def test_confirmed_sources_survive_an_empty_declaration(self, index):
        await index.revise(ref="Login flow", content="Auth is a JWT.", sources=SourceDeclaration())
        assert sources_of(await index.lineage("Login flow")) == [
            ("Auth model", 1, "confirmed"),
            ("Error taxonomy", 1, "confirmed"),
        ]

    async def test_and_so_the_document_is_still_reported_stale(self, index):
        """The regression that matters. Before the fix this returned
        only Release checklist, and Login flow looked healthy."""
        await index.revise(ref="Login flow", content="Auth is a JWT.", sources=SourceDeclaration())
        assert "Login flow" in await stale_names(index)

    async def test_an_inherited_pin_keeps_the_version_it_was_on(self, index):
        await index.revise(ref="Login flow", content="x", sources=SourceDeclaration())
        edge = next(
            e for e in (await index.lineage("Login flow")).sources if e.to_name == "Auth model"
        )
        assert (edge.pinned_n, edge.source_head_n) == (1, 3)


class TestDeclaringASourceStillWins:
    async def test_naming_it_at_head_resolves_the_staleness(self, index):
        await index.revise(
            ref="Login flow",
            content="Auth is a 15-minute JWT.",
            sources=SourceDeclaration(
                pins=[VersionPin(artifact_id=AUTH, n=3, state=EdgeState.CONFIRMED)]
            ),
        )
        assert "Login flow" not in await stale_names(index)

    async def test_the_declaration_is_not_duplicated_by_inheritance(self, index):
        await index.revise(
            ref="Login flow",
            content="Auth is a 15-minute JWT.",
            sources=SourceDeclaration(
                pins=[VersionPin(artifact_id=AUTH, n=3, state=EdgeState.CONFIRMED)]
            ),
        )
        auth_edges = [
            e for e in (await index.lineage("Login flow")).sources if e.to_name == "Auth model"
        ]
        assert len(auth_edges) == 1
        assert auth_edges[0].pinned_n == 3


class TestRemovingASourceIsStillPossible:
    """Inheritance must not make an edge immortal — it only means
    removal takes a decision instead of an omission."""

    async def test_a_dismissed_source_is_not_inherited(self, index):
        await index.dismiss_source(ref="Login flow", source_ref="Error taxonomy")
        await index.revise(ref="Login flow", content="x", sources=SourceDeclaration())
        assert "Error taxonomy" not in [
            n for n, _, _ in sources_of(await index.lineage("Login flow"))
        ]

    async def test_an_unadjudicated_candidate_is_not_inherited(self, index):
        """A candidate records that a version was read, which is not a
        claim about the version replacing it. It re-captures on its own
        if the agent reads it again."""
        before = sources_of(await index.lineage("Login flow"))
        assert ("Rate limiting notes", 1, "candidate") in before
        await index.revise(ref="Login flow", content="x", sources=SourceDeclaration())
        states = {n: s for n, _, s in sources_of(await index.lineage("Login flow"))}
        assert "Rate limiting notes" not in states


class TestCreateIsUnaffected:
    async def test_a_new_document_starts_with_no_sources(self, index):
        await index.create(
            name="Fresh note", content="Nothing behind this.", sources=SourceDeclaration()
        )
        assert sources_of(await index.lineage("Fresh note")) == []
