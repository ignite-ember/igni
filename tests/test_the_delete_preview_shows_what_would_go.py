"""``knowledge_delete(confirm=False)`` has to actually preview.

Its own docstring — and both shipped system prompts, verbatim — tell the
model that the unconfirmed call "returns a preview of what would be
deleted". The body was two lines of f-string that touched neither the
search nor the index, so it could not tell zero matches from fifty and
the model's only move was to confirm blind.

The cap matters too: ``KnowledgeIndex.delete_by_query`` takes at most
ten entries per call, so a preview that listed more than ten would be
promising a delete that will not happen.
"""

from __future__ import annotations

import pytest

from ember_code.core.knowledge.models import (
    KnowledgeSearchResponse,
    KnowledgeSearchResult,
)
from ember_code.core.tools.knowledge import KnowledgeTools


class _FakeIndex:
    """Stands in for :class:`KnowledgeIndex` — records the delete call."""

    def __init__(self) -> None:
        self.delete_calls: list[tuple[str, int]] = []

    async def delete_by_query(self, query: str, *, limit: int = 10):
        self.delete_calls.append((query, limit))
        return type("R", (), {"deleted": len(self.delete_calls)})()


class _FakeManager:
    """Stands in for :class:`SessionKnowledgeManager`."""

    def __init__(self, hits: list[KnowledgeSearchResult], *, index: object | None = None) -> None:
        self.knowledge = _FakeIndex() if index is None else index
        self._hits = hits
        self.search_calls: list[tuple[str, int]] = []

    async def search(self, query: str, limit: int = 5, cross_project: bool = False):
        self.search_calls.append((query, limit))
        hits = self._hits[:limit]
        return KnowledgeSearchResponse(query=query, results=hits, total=len(hits))


def _hit(name: str, content: str) -> KnowledgeSearchResult:
    return KnowledgeSearchResult(entry_id=name, name=name, content=content)


async def test_the_preview_names_the_entries_that_would_go():
    mgr = _FakeManager([_hit("deploy-notes", "run make deploy"), _hit("stale-url", "http://old")])
    tools = KnowledgeTools(mgr)

    preview = await tools.knowledge_delete("deploy")

    assert mgr.search_calls, "preview mode never searched — it cannot be a preview"
    assert "deploy-notes" in preview
    assert "stale-url" in preview
    assert "run make deploy" in preview
    assert "confirm=True" in preview


async def test_the_preview_searches_with_the_limit_the_delete_will_use():
    """Showing rows eleven and up would promise a delete that cannot happen."""
    mgr = _FakeManager([_hit(f"e{i}", f"c{i}") for i in range(40)])
    tools = KnowledgeTools(mgr)

    preview = await tools.knowledge_delete("everything")

    assert mgr.search_calls == [("everything", 10)]
    assert "e9" in preview
    assert "e10" not in preview


async def test_an_empty_preview_says_nothing_matches():
    mgr = _FakeManager([])
    tools = KnowledgeTools(mgr)

    preview = await tools.knowledge_delete("nothing-here")

    assert "no knowledge entries match" in preview.lower()
    assert "confirm=True" not in preview, (
        "offering the confirm step for a query that matches nothing invites a "
        "second round-trip that can only say 'No entries found to delete.'"
    )


async def test_the_preview_deletes_nothing():
    index = _FakeIndex()
    mgr = _FakeManager([_hit("a", "b")], index=index)
    tools = KnowledgeTools(mgr)

    await tools.knowledge_delete("a")

    assert index.delete_calls == []


async def test_confirm_deletes_with_the_same_limit_the_preview_showed():
    index = _FakeIndex()
    mgr = _FakeManager([_hit("a", "b")], index=index)
    tools = KnowledgeTools(mgr)

    result = await tools.knowledge_delete("a", confirm=True)

    assert index.delete_calls == [("a", 10)]
    assert "Deleted" in result


@pytest.mark.parametrize("confirm", [False, True])
async def test_a_missing_knowledge_base_says_so_on_both_branches(confirm):
    mgr = _FakeManager([], index=None)
    mgr.knowledge = None
    tools = KnowledgeTools(mgr)

    assert "not available" in await tools.knowledge_delete("q", confirm=confirm)
