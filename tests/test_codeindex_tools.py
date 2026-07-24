"""Tests for ``CodeIndexTools`` — the structured ``codeindex_query`` tool."""

from __future__ import annotations

import json
import uuid

import pytest

from ember_code.core.code_index.enums import (
    FileSystemType,
    Kind,
    QualityLevel,
    Relation,
    SecurityLevel,
)
from ember_code.core.code_index.index import CodeIndex
from ember_code.core.code_index.schema.items import CodeIndexItem
from ember_code.core.tools.codeindex import (
    CodeIndexTools,
    _build_where,
    _CategoricalFilters,
)


def _make_item(
    name: str,
    content: str,
    *,
    quality: str | None = None,
    security: str | None = None,
    domain: list[str] | None = None,
    vulnerabilities: list[str] | None = None,
    entity_type: str | None = None,
    kind: str = "code",
) -> CodeIndexItem:
    return CodeIndexItem(
        item_id=str(uuid.uuid4()),
        name=name,
        content=content,
        type=FileSystemType.FILE,
        kind=kind,
        path=f"src/{name}",
        repository_id="test-repo",
        entity_type=entity_type,
        quality=quality,
        security=security,
        domain=domain or [],
        vulnerabilities=vulnerabilities or [],
    )


@pytest.fixture
async def index(tmp_path):
    idx = CodeIndex(project=tmp_path / "proj", data_dir=str(tmp_path / "data"))
    await idx.set_head("c1")
    await idx.prepare_commit("c1")
    yield idx
    await idx.close()


@pytest.fixture
def tools(index):
    return CodeIndexTools(index=index)


# ── Tool registration ───────────────────────────────────────────────


class TestRegistration:
    def test_registers_two_tools(self, tools):
        names = set()
        for f in tools.functions.values():
            names.add(f.name)
        for f in tools.async_functions.values():
            names.add(f.name)
        assert names == {"codeindex_query", "codeindex_tree"}


# ── _build_where helper ─────────────────────────────────────────────


class TestBuildWhere:
    """``_build_where`` returns a :class:`ChromaWhereFilter` (or None);
    ``.to_chroma_where()`` renders it into the dict shape Chroma expects."""

    def test_no_args_returns_none(self):
        assert _build_where(_CategoricalFilters()) is None

    def test_single_categorical_no_and_wrap(self):
        where = _build_where(_CategoricalFilters(security=SecurityLevel.CRITICAL))
        assert where.to_chroma_where() == {"security": "critical"}

    def test_categorical_list_uses_in(self):
        where = _build_where(
            _CategoricalFilters(security=[SecurityLevel.MAJOR_ISSUES, SecurityLevel.CRITICAL])
        )
        assert where.to_chroma_where() == {"security": {"$in": ["major-issues", "critical"]}}

    def test_multiple_categoricals_wrapped_in_and(self):
        where = _build_where(
            _CategoricalFilters(security=SecurityLevel.CRITICAL, quality=QualityLevel.POOR)
        )
        rendered = where.to_chroma_where()
        assert "$and" in rendered
        clauses = rendered["$and"]
        assert {"security": "critical"} in clauses
        assert {"quality": "poor"} in clauses

    def test_kind_handled_as_enum_value(self):
        where = _build_where(_CategoricalFilters(kind=Kind.DOCS))
        assert where.to_chroma_where() == {"kind": "docs"}

    def test_needs_refactoring_bool(self):
        where = _build_where(_CategoricalFilters(needs_refactoring=True))
        assert where.to_chroma_where() == {"needs_refactoring": True}


# ── Items: semantic ──────────────────────────────────────────────────


class TestSemanticSearch:
    @pytest.mark.asyncio
    async def test_query_text_returns_items(self, tools, index):
        await index.add_item("c1", _make_item("auth.py", "JWT authentication and access tokens."))
        await index.add_item("c1", _make_item("db.py", "Database pool with retries."))
        result = json.loads(await tools.codeindex_query(query_text="JWT", limit=5))
        assert result["commit"] == "c1"
        names = [i["name"] for i in result["items"]]
        assert "auth.py" in names

    @pytest.mark.asyncio
    async def test_query_text_with_security_filter(self, tools, index):
        """Quality filters compose with semantic search."""
        await index.add_item(
            "c1",
            _make_item(
                "risky.py",
                "raw SQL with user input concatenation",
                security="critical",
            ),
        )
        await index.add_item(
            "c1",
            _make_item(
                "safe.py",
                "parameterized SQL queries",
                security="secure",
            ),
        )
        result = json.loads(
            await tools.codeindex_query(query_text="SQL", security=SecurityLevel.CRITICAL)
        )
        names = {i["name"] for i in result["items"]}
        assert names == {"risky.py"}


# ── Items: filter / fetch ───────────────────────────────────────────


class TestFilterFetch:
    @pytest.mark.asyncio
    async def test_get_by_ids(self, tools, index):
        item = _make_item("seed.py", "seed content")
        await index.add_item("c1", item)
        result = json.loads(await tools.codeindex_query(ids=[item.item_id]))
        ids = [i["item_id"] for i in result["items"]]
        assert ids == [item.item_id]

    @pytest.mark.asyncio
    async def test_filter_by_quality(self, tools, index):
        await index.add_item("c1", _make_item("good.py", "x", quality="good"))
        await index.add_item("c1", _make_item("poor.py", "x", quality="poor"))
        result = json.loads(await tools.codeindex_query(quality=QualityLevel.POOR))
        names = {i["name"] for i in result["items"]}
        assert names == {"poor.py"}

    @pytest.mark.asyncio
    async def test_filter_by_vulnerability_list(self, tools, index):
        await index.add_item(
            "c1",
            _make_item(
                "vuln.py",
                "x",
                vulnerabilities=["sql-injection", "xss"],
            ),
        )
        await index.add_item("c1", _make_item("clean.py", "x"))
        result = json.loads(await tools.codeindex_query(vulnerabilities=["sql-injection"]))
        names = {i["name"] for i in result["items"]}
        assert names == {"vuln.py"}

    @pytest.mark.asyncio
    async def test_filter_by_domain(self, tools, index):
        await index.add_item("c1", _make_item("a.py", "x", domain=["auth"]))
        await index.add_item("c1", _make_item("b.py", "x", domain=["billing"]))
        result = json.loads(await tools.codeindex_query(domain=["auth"]))
        assert {i["name"] for i in result["items"]} == {"a.py"}

    @pytest.mark.asyncio
    async def test_combined_quality_and_domain_is_and(self, tools, index):
        await index.add_item(
            "c1",
            _make_item("hot.py", "x", security="critical", domain=["auth"]),
        )
        await index.add_item(
            "c1",
            _make_item("cold.py", "x", security="critical", domain=["billing"]),
        )
        result = json.loads(
            await tools.codeindex_query(security=SecurityLevel.CRITICAL, domain=["auth"])
        )
        assert {i["name"] for i in result["items"]} == {"hot.py"}


# ── `truncated` flag semantics ──────────────────────────────────────


class TestTruncatedFlag:
    """The ``truncated`` field on ``ItemsResponse`` tells the agent
    whether more candidates existed upstream than what we returned.

    Pre-fix: the flag was computed AFTER post-filtering and AFTER
    slicing to ``limit`` — so a query that fetched a full batch and
    then had filters discard most rows reported ``truncated=False``,
    making the agent think it had exhausted the search space when in
    fact several matches were silently dropped. The fix computes the
    flag on the pre-filter row count.
    """

    @pytest.mark.asyncio
    async def test_truncated_false_when_within_limit(self, tools, index):
        # Two items, agent asks for limit=10 → no truncation.
        await index.add_item("c1", _make_item("a.py", "alpha", quality="good"))
        await index.add_item("c1", _make_item("b.py", "beta", quality="good"))
        result = json.loads(await tools.codeindex_query(quality=QualityLevel.GOOD, limit=10))
        assert result["truncated"] is False

    @pytest.mark.asyncio
    async def test_truncated_true_when_pre_filter_hits_fetch_cap(self, tools, index):
        """Pre-fix bug: post-filtering down to 1 row reported
        truncated=False even when chroma returned a full batch.

        Set up: 9 items all with quality=GOOD but only 1 with
        domain=['auth']. fetch_limit = limit * 4 = 12 when list_filters
        are in play. Chroma returns all 9 → after list_filter, 1 row
        remains. The agent must learn there were more pre-filter hits,
        not assume the result set was tiny.
        """
        for i in range(9):
            await index.add_item(
                "c1",
                _make_item(
                    f"f{i}.py",
                    "x",
                    quality="good",
                    domain=["auth"] if i == 0 else ["other"],
                ),
            )
        # limit=2 → fetch_limit=8. Pre-filter rows = 8 (matches
        # fetch_limit), post-filter = 1 (only one has domain=['auth']).
        # Truncated must be True because chroma returned the full batch.
        result = json.loads(
            await tools.codeindex_query(quality=QualityLevel.GOOD, domain=["auth"], limit=2)
        )
        assert result["truncated"] is True, (
            f"expected truncated=True; got {result['truncated']} with {len(result['items'])} items"
        )

    @pytest.mark.asyncio
    async def test_truncated_false_when_pre_filter_under_fetch_cap(self, tools, index):
        """When chroma didn't return a full batch, the result is
        comprehensive even if filters dropped some rows."""
        # 3 items total, limit=10 → fetch_limit=40, chroma returns 3
        # which is well under fetch_limit → not truncated.
        await index.add_item("c1", _make_item("a.py", "x", domain=["auth"]))
        await index.add_item("c1", _make_item("b.py", "x", domain=["billing"]))
        await index.add_item("c1", _make_item("c.py", "x", domain=["auth"]))
        result = json.loads(await tools.codeindex_query(domain=["auth"], limit=10))
        assert result["truncated"] is False


# ── codeindex_tree (single-item drill-down) ─────────────────────────


class TestTree:
    @pytest.mark.asyncio
    async def test_returns_single_item_with_references(self, tools, index):
        item = _make_item("a.py", "x")
        await index.add_item("c1", item)
        file_refs = index.file_reference_service()
        # ``item --imports--> b``: outgoing edge, item is on the FROM
        # side → b lands under references["imports"].
        await file_refs.create(
            from_uuid=item.item_id,
            to_uuid="b",
            relation="imports",
            meta={"to_entity_name": "B", "to_entity_path": "src/b.py"},
        )
        # ``x --called_by--> item``: edge stored as "x is called by item"
        # — from item's POV, item is the caller. Tree service flips the
        # to-side relation so x lands under references["calls"] (NOT
        # called_by — the relation key reflects item's perspective).
        # Pre-fix bug: bucketed x under "called_by", inverting direction.
        await file_refs.create(
            from_uuid="x",
            to_uuid=item.item_id,
            relation="called_by",
            meta={"from_entity_name": "X", "from_entity_path": "src/x.py"},
        )

        result = json.loads(await tools.codeindex_tree(id=item.item_id))
        assert result["total"] == 1
        refs = result["items"][0]["references"]
        assert {t["id"] for t in refs["imports"]} == {"b"}
        assert {t["id"] for t in refs["calls"]} == {"x"}, (
            "x should land under item's 'calls' (item calls x), not under 'called_by'"
        )

    @pytest.mark.asyncio
    async def test_relations_filter(self, tools, index):
        item = _make_item("a.py", "x")
        await index.add_item("c1", item)
        file_refs = index.file_reference_service()
        await file_refs.create(from_uuid=item.item_id, to_uuid="b", relation="calls", meta={})
        await file_refs.create(from_uuid=item.item_id, to_uuid="c", relation="imports", meta={})

        result = json.loads(await tools.codeindex_tree(id=item.item_id, relations=[Relation.CALLS]))
        refs = result["items"][0]["references"]
        assert set(refs.keys()) == {"calls"}

    @pytest.mark.asyncio
    async def test_no_edges_omits_references(self, tools, index):
        item = _make_item("loner.py", "x")
        await index.add_item("c1", item)

        result = json.loads(await tools.codeindex_tree(id=item.item_id))
        # exclude_none strips an empty ``references`` field.
        assert "references" not in result["items"][0]

    @pytest.mark.asyncio
    async def test_unknown_id_errors(self, tools):
        result = json.loads(await tools.codeindex_tree(id="does-not-exist"))
        assert "error" in result


# ── Empty-call guardrail ────────────────────────────────────────────


class TestEmptyCallGuardrail:
    """Catches the case-11-shape failure where the agent reaches for a
    typed filter but passes ``None`` as the value — yielding an empty
    filter call that returns arbitrary items, which the agent then
    misreads as "the worst offenders."
    """

    @pytest.mark.asyncio
    async def test_no_args_returns_didactic_error(self, tools):
        result = json.loads(await tools.codeindex_query())
        assert "error" in result
        assert "without any narrowing input" in result["error"]
        assert "security=['major-issues','critical']" in result["error"]

    @pytest.mark.asyncio
    async def test_typed_filter_as_none_is_blocked(self, tools):
        # Exact case-11 failure shape from the v7 telemetry
        result = json.loads(await tools.codeindex_query(security=None, sections=None, limit=15))
        assert "error" in result
        assert "None means 'no filter on this dimension'" in result["error"]

    @pytest.mark.asyncio
    async def test_only_output_control_args_is_blocked(self, tools):
        # sections / limit / commit don't narrow — they only shape output
        result = json.loads(await tools.codeindex_query(limit=5, sections=None))
        assert "error" in result

    @pytest.mark.asyncio
    async def test_real_typed_filter_passes(self, tools, index):
        await index.add_item("c1", _make_item("x.py", "x", security="critical"))
        result = json.loads(await tools.codeindex_query(security=SecurityLevel.CRITICAL))
        assert "error" not in result
        assert result["total"] == 1

    @pytest.mark.asyncio
    async def test_query_text_alone_passes(self, tools, index):
        await index.add_item("c1", _make_item("x.py", "x"))
        result = json.loads(await tools.codeindex_query(query_text="anything"))
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_ids_alone_passes(self, tools, index):
        item = _make_item("x.py", "x")
        await index.add_item("c1", item)
        result = json.loads(await tools.codeindex_query(ids=[item.item_id]))
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_needs_refactoring_false_passes(self, tools, index):
        # ``False`` is a meaningful filter value — items NOT flagged for
        # refactoring — even though it's falsy. Don't block it.
        await index.add_item("c1", _make_item("x.py", "x"))
        result = json.loads(await tools.codeindex_query(needs_refactoring=False))
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_list_filter_with_value_passes(self, tools, index):
        await index.add_item("c1", _make_item("x.py", "x", vulnerabilities=["sql-injection"]))
        result = json.loads(await tools.codeindex_query(vulnerabilities=["sql-injection"]))
        assert "error" not in result


# ── Error paths ─────────────────────────────────────────────────────


class TestErrorPaths:
    @pytest.mark.asyncio
    async def test_query_text_and_ids_mutually_exclusive(self, tools, index):
        item = _make_item("x.py", "x")
        await index.add_item("c1", item)
        result = json.loads(await tools.codeindex_query(query_text="anything", ids=[item.item_id]))
        assert "error" in result

    @pytest.mark.asyncio
    async def test_unknown_commit(self, tools):
        result = json.loads(await tools.codeindex_query(commit="deadbeef", query_text="x"))
        assert "error" in result

    @pytest.mark.asyncio
    async def test_internal_exception_surfaces_error(self, tools, monkeypatch):
        async def boom(*_a, **_kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(tools._explicit_index, "search", boom)
        result = json.loads(await tools.codeindex_query(query_text="x"))
        assert "error" in result
