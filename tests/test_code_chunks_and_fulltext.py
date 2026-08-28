"""Code chunks, their line spans, and the term search that can say "no".

Until now the graph held no source at all — only model-written summaries — so
the only way to confirm a claim about the code was to leave the index and read
the working tree. Two things fix that: chunks of the actual source, tagged
``chunk_kind='code'`` and carrying absolute line numbers, and a Lucene full-text
index over chunk text.

The full-text index matters for a reason vector search cannot fix. Similarity
always returns its k nearest neighbours: asked for "validates a JWT bearer
token" a SQL parser with no auth code returned a token-grouping function at
0.72, against 0.82 for real hits. Term search returns an empty set when the term
is absent, and that empty set is the answer.
"""

from __future__ import annotations

import pytest

from ember_code.core.code_index.neo4j_schema import COMMIT_SCHEMA_STATEMENTS
from ember_code.core.code_index.schema.items import ChunkRow
from ember_code.core.tools.codeindex.cypher_guard import (
    CypherGuardError,
    assert_read_only_cypher,
)


class TestChunkRow:
    def test_the_two_field_form_still_unpacks(self):
        """``_carryover_from_parent`` and older callers pass a plain tuple."""
        row = ChunkRow("text", [0.1, 0.2])
        text, embedding = row[0], row[1]
        assert (text, embedding) == ("text", [0.1, 0.2])
        assert row.chunk_kind == "summary"
        assert row.line_from is None

    def test_a_code_row_carries_its_place(self):
        row = ChunkRow("def f():", [0.1], "code", 40, 79)
        assert (row.chunk_kind, row.line_from, row.line_to) == ("code", 40, 79)


class TestSourceChunking:
    """Line windows, because a chunk that cannot say where is only a file name."""

    @staticmethod
    def _chunk(source: str, line_from: int | None = 1):
        from ember_code.core.code_index.index import CodeIndex

        return CodeIndex._chunk_source(CodeIndex, source, line_from)  # type: ignore[arg-type]

    def test_spans_are_absolute_file_lines(self):
        """An entity starting at line 500 must report 500, not 1 — the whole
        point is being able to open the file at the answer."""
        source = "\n".join(f"line {i}" for i in range(1, 61))
        windows = self._chunk(source, 500)
        assert windows[0][1] == 500
        assert windows[0][2] == 539

    def test_windows_overlap_so_a_construct_is_whole_somewhere(self):
        source = "\n".join(f"line {i}" for i in range(1, 101))
        windows = self._chunk(source, 1)
        assert len(windows) > 1
        assert windows[1][1] < windows[0][2], "second window must start inside the first"

    def test_a_short_body_is_one_window(self):
        windows = self._chunk("def f():\n    return 1", 7)
        assert windows == [("def f():\n    return 1", 7, 8)]

    @pytest.mark.parametrize("source", ["", "   ", "\n\n\n"])
    def test_nothing_to_chunk(self, source):
        assert self._chunk(source, 1) == []

    def test_no_line_from_defaults_to_one(self):
        assert self._chunk("a\nb", None)[0][1] == 1


class TestFullTextIsAvailable:
    def test_the_index_exists(self):
        statements = " ".join(COMMIT_SCHEMA_STATEMENTS)
        assert "FULLTEXT INDEX chunk_text" in statements
        assert "chunk_kind" in statements

    def test_the_guard_allows_term_search(self):
        """Without this the arm cannot ask "is it really there?" at all."""
        assert_read_only_cypher(
            "CALL db.index.fulltext.queryNodes('chunk_text', 'pickle') "
            "YIELD node, score RETURN node.path LIMIT 10"
        )

    def test_the_guard_allows_vector_search(self):
        assert_read_only_cypher(
            "CALL db.index.vector.queryNodes('chunk_embedding', 10, $query_vector) "
            "YIELD node, score RETURN node.path LIMIT 10"
        )

    def test_other_procedures_are_still_refused(self):
        with pytest.raises(CypherGuardError):
            assert_read_only_cypher(
                "CALL db.schema.visualization() YIELD nodes RETURN nodes LIMIT 1"
            )

    def test_writes_are_still_refused(self):
        with pytest.raises(CypherGuardError):
            assert_read_only_cypher("MATCH (i:Item) SET i.name = 'x' RETURN i LIMIT 1")
