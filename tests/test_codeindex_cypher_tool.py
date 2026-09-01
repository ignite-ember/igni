"""Tests for the ``codeindex_cypher`` read-only Cypher escape hatch.

The tool exists for specialist agents (the ``data-architect``
agent, primarily) to author ad-hoc Cypher that the typed
``codeindex_query`` surface can't express. The contract is:

  * **Hard-deny** without ``confirm_raw_cypher=True`` (defensive
    flag so a misbehaving agent can't accidentally invoke the
    un-typed path).
  * **Read-only** — every Cypher that reaches the driver must
    pass :func:`assert_read_only_cypher` (writes, admin,
    unknown ``$param`` names, multi-
    statements are all rejected).
  * **Tool surface only** — agents never call Neo4j directly.

Tests come in three groups:

  1. Guard tests (pure module, no toolkit): each reject
     category on its own.
  2. Tool tests (toolkit surface, mocked): the
     ``confirm_raw_cypher`` gate fires before the guard;
     even with the flag, a forbidden query is rejected; the
     happy path threads through to the service.
  3. Behaviour tests: the per-commit ``Neo4jClient.client_for``
     is the only driver seam (no parallel paths to the
     database).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ember_code.core.code_index.index import CodeIndex
from ember_code.core.tools.codeindex.cypher_guard import (
    CypherGuardError,
    CypherReadOnlyViolation,
    CypherUnknownParam,
    assert_read_only_cypher,
)
from ember_code.core.tools.codeindex.schemas import CypherInput
from ember_code.core.tools.codeindex.tool import CodeIndexTools

# ── Group 1: pure guard ───────────────────────────────────────────────


class TestCypherGuardHappyPath:
    """Cases that should be accepted and returned stripped."""

    def test_simple_match_return_passes(self):
        out = assert_read_only_cypher("MATCH (i:Item {project_hash: $proj}) RETURN i LIMIT 5")
        assert "MATCH" in out and "$proj" in out

    def test_comments_are_stripped(self):
        out = assert_read_only_cypher(
            "// find recent files\n"
            "/* block comment */\n"
            "MATCH (i:Item {project_hash: $proj}) RETURN i.name, i.path LIMIT 10"
        )
        assert "//" not in out and "/*" not in out
        assert out.strip().startswith("MATCH")

    def test_with_optional_match_passes(self):
        out = assert_read_only_cypher(
            "OPTIONAL MATCH (i:Item {project_hash: $proj})-[:IMPORTS]->(other:Item)\n"
            "RETURN i, other LIMIT 25"
        )
        assert "OPTIONAL MATCH" in out

    def test_unwind_union_orderby_passes(self):
        out = assert_read_only_cypher(
            "MATCH (i:Item {project_hash: $proj}) RETURN i.id AS id\n"
            "UNION\n"
            "MATCH (j:Item {project_hash: $proj}) RETURN j.id AS id\n"
            "ORDER BY id SKIP $skip_n LIMIT $limit_n"
        )
        assert out.count("RETURN") == 2

    def test_explain_passes(self):
        out = assert_read_only_cypher(
            "EXPLAIN MATCH (i:Item {project_hash: $proj}) RETURN i LIMIT 5"
        )
        assert out.startswith("EXPLAIN")


class TestCypherGuardRejections:
    """Cases that should each raise the correct guardrail subclass."""

    def test_create_is_rejected(self):
        with pytest.raises(CypherReadOnlyViolation, match="CREATE"):
            assert_read_only_cypher("CREATE (n:Item {project_hash: $proj}) RETURN n")

    def test_merge_set_is_rejected(self):
        with pytest.raises(CypherReadOnlyViolation, match="MERGE"):
            assert_read_only_cypher(
                "MERGE (i:Item {id: 'x', project_hash: $proj}) "
                "ON CREATE SET i.path = $path "
                "RETURN i"
            )

    def test_delete_remove_is_rejected(self):
        with pytest.raises(CypherReadOnlyViolation, match="DELETE"):
            assert_read_only_cypher("MATCH (i:Item {project_hash: $proj}) DELETE i")
        with pytest.raises(CypherReadOnlyViolation, match="REMOVE"):
            assert_read_only_cypher("MATCH (i:Item {project_hash: $proj}) REMOVE i.flag")

    def test_drop_alter_is_rejected(self):
        with pytest.raises(CypherReadOnlyViolation, match="DROP"):
            assert_read_only_cypher("DROP INDEX item_id_idx")
        with pytest.raises(CypherReadOnlyViolation, match="ALTER"):
            assert_read_only_cypher("ALTER DATABASE foo MODE READ_ONLY")

    def test_call_dbms_db_procedures_rejected(self):
        with pytest.raises(CypherReadOnlyViolation):
            assert_read_only_cypher("CALL dbms.security.showCurrentUser()")
        with pytest.raises(CypherReadOnlyViolation):
            assert_read_only_cypher("CALL db.info() RETURN 1")

    def test_show_rejected(self):
        with pytest.raises(CypherReadOnlyViolation, match="SHOW"):
            assert_read_only_cypher("SHOW INDEXES")

    def test_profile_rejected(self):
        with pytest.raises(CypherReadOnlyViolation, match="PROFILE"):
            assert_read_only_cypher("PROFILE MATCH (i:Item {project_hash: $proj}) RETURN i")

    def test_transaction_control_rejected(self):
        with pytest.raises(CypherReadOnlyViolation, match="BEGIN"):
            assert_read_only_cypher("BEGIN\nMATCH (i:Item {project_hash: $proj}) RETURN i")
        with pytest.raises(CypherReadOnlyViolation):
            assert_read_only_cypher("MATCH (i:Item {project_hash: $proj}) RETURN i LIMIT 5\nCOMMIT")

    def test_no_project_hash_predicate_required(self):
        # Project isolation is a PROCESS boundary (see neo4j_schema.py) —
        # the driver can only reach the current project's data, so a
        # `project_hash = $proj` predicate is architecturally redundant
        # and no longer required by the guardrail.
        assert_read_only_cypher("MATCH (i:Item) RETURN i LIMIT 5")

    def test_multi_statement_rejected(self):
        with pytest.raises(CypherReadOnlyViolation, match="single statement"):
            assert_read_only_cypher(
                "MATCH (i:Item {project_hash: $proj}) RETURN i; "
                "MATCH (j:Item {project_hash: $proj}) RETURN j"
            )

    def test_trailing_semicolon_ok(self):
        # Trailing ``;`` is fine — only post-statement content is
        # rejected, since that's where writes are usually hidden.
        assert_read_only_cypher("MATCH (i:Item {project_hash: $proj}) RETURN i LIMIT 5;")

    def test_unknown_param_name_rejected(self):
        with pytest.raises(CypherUnknownParam, match="evil"):
            assert_read_only_cypher(
                "MATCH (i:Item {project_hash: $proj, name: $evil}) RETURN i LIMIT 5"
            )

    def test_allowed_param_names_pass(self):
        for name in (
            "proj",
            "commit_sha",
            "ids",
            "limit_n",
            "skip_n",
            "kind",
            "type",
            "quality",
        ):
            assert_read_only_cypher(
                f"MATCH (i:Item {{project_hash: $proj, x: ${name}}}) RETURN i LIMIT 5"
            )

    def test_empty_string_rejected(self):
        with pytest.raises(CypherGuardError):
            assert_read_only_cypher("")

    def test_non_string_rejected(self):
        with pytest.raises(CypherGuardError):
            assert_read_only_cypher(None)  # type: ignore[arg-type]


# ── Group 2: tool surface ─────────────────────────────────────────────


def _make_tools(neo4j_rows=None, *, no_backend=False) -> tuple[CodeIndexTools, MagicMock]:
    """Build a ``CodeIndexTools`` with a mocked Neo4j client + client_for.

    Returns ``(tools, client_for_callable)`` so tests can also
    inspect how the toolkit routes to the driver seam.

    The toolkit owns a :class:`ToolInvocationRecorder` that
    expects ``coro`` to return a ``BaseModel`` (it goes through
    ``JsonSerializer.dumps``). The service already returns the
    typed ``CypherResponse`` / ``ErrorResponse`` model — don't
    double-serialise.
    """
    mock_index = MagicMock(spec=CodeIndex)
    mock_index.project_id = "test-project"
    if no_backend:
        mock_index.client_for = AsyncMock(return_value=None)
    else:
        client = MagicMock()
        client.execute_query = AsyncMock(return_value=neo4j_rows or [])
        mock_index.client_for = AsyncMock(return_value=client)
        mock_index._client = client

    tools = CodeIndexTools(project_dir=".", index=mock_index)
    return tools, mock_index.client_for


class TestCodeindexCypherToolGate:
    def test_missing_confirm_flag_is_hard_denied(self):
        tools, client_for = _make_tools()
        result = asyncio.run(
            tools.codeindex_cypher(
                cypher="MATCH (i:Item {project_hash: $proj}) RETURN i LIMIT 5",
                confirm_raw_cypher=False,
            )
        )
        envelope = json.loads(result)
        assert envelope["error"] == "confirm_required"
        assert "confirm_raw_cypher=True" in envelope["message"]
        # Crucially — the driver was never even asked for a
        # client. The gate fires before any DB seam is touched.
        assert client_for.await_count == 0

    def test_confirm_default_arg_is_hard_denied(self):
        tools, _ = _make_tools()
        # Forgetting the kwarg entirely is the common case.
        result = asyncio.run(
            tools.codeindex_cypher(
                cypher="MATCH (i:Item {project_hash: $proj}) RETURN i LIMIT 5",
            )
        )
        envelope = json.loads(result)
        assert envelope["error"] == "confirm_required"
        assert "confirm_raw_cypher=True" in envelope["message"]

    def test_confirm_false_with_truthy_nonbool_is_hard_denied(self):
        # A future agent / model might pass the string "true"
        # thinking Python's truthiness covers it. We require an
        # explicit ``True`` boolean — anything else is denied.
        tools, _ = _make_tools()
        for truthy in ("true", "yes", 1, 1.0):
            result = asyncio.run(
                tools.codeindex_cypher(
                    cypher=("MATCH (i:Item {project_hash: $proj}) RETURN i LIMIT 5"),
                    confirm_raw_cypher=truthy,  # type: ignore[arg-type]
                )
            )
            envelope = json.loads(result)
            assert envelope["error"] == "confirm_required"
        assert "confirm_raw_cypher=True" in envelope["message"], f"expected denial for {truthy!r}"

    def test_write_with_confirm_is_guardrail_rejected(self):
        tools, client_for = _make_tools()
        result = asyncio.run(
            tools.codeindex_cypher(
                cypher="CREATE (n:Item {project_hash: $proj}) RETURN n",
                confirm_raw_cypher=True,
            )
        )
        envelope = json.loads(result)
        # Confirm-flag bypassed, but the guard caught the write.
        assert envelope["error"] == "cypher_guard"
        assert "CREATE" in envelope["message"]
        # And the driver was still never asked for a client.
        assert client_for.await_count == 0

    def test_no_project_hash_is_accepted_process_is_the_boundary(self):
        # Project isolation is a PROCESS boundary (see neo4j_schema.py):
        # each (project, commit) pair has its own Neo4j process, so a
        # missing `project_hash` predicate is architecturally fine —
        # the driver can only see the current project's data. The
        # guard used to reject; this test pins the current behavior.
        tools, client_for = _make_tools(neo4j_rows=[])
        result = asyncio.run(
            tools.codeindex_cypher(
                cypher="MATCH (i:Item) RETURN i LIMIT 5",
                confirm_raw_cypher=True,
            )
        )
        envelope = json.loads(result)
        # No `error` envelope — the query reached the driver seam.
        assert "error" not in envelope
        assert client_for.await_count == 1


class TestCodeindexCypherToolHappyPath:
    def test_read_only_match_runs_through(self):
        tools, client_for = _make_tools(neo4j_rows=[{"name": "foo"}, {"name": "bar"}])
        result = asyncio.run(
            tools.codeindex_cypher(
                cypher=("MATCH (i:Item {project_hash: $proj}) RETURN i.name AS name"),
                confirm_raw_cypher=True,
                limit=10,
            )
        )
        envelope = json.loads(result)
        # CypherResponse shape: rows + bookkeeping.
        assert envelope["row_count"] == 2
        assert envelope["truncated"] is False
        assert envelope["limit"] == 10
        assert envelope["rows"] == [{"name": "foo"}, {"name": "bar"}]
        # The tool routed through the per-commit client.
        assert client_for.await_count == 1

    def test_limit_caps_and_signals_truncation(self):
        tools, _ = _make_tools(neo4j_rows=[{"i": i} for i in range(50)])
        result = asyncio.run(
            tools.codeindex_cypher(
                cypher=("MATCH (i:Item {project_hash: $proj}) RETURN i LIMIT 50"),
                confirm_raw_cypher=True,
                limit=5,
            )
        )
        envelope = json.loads(result)
        assert envelope["row_count"] == 5
        assert envelope["truncated"] is True
        assert envelope["limit"] == 5

    def test_proj_is_always_injected(self):
        """The toolkit injects ``proj = project_id`` even if the
        agent forgot to pass it."""
        tools, _ = _make_tools(neo4j_rows=[{"x": 1}])
        captured: dict = {}

        async def capture_run(*args, **kwargs):
            captured["params"] = kwargs
            return [{"x": 1}]

        # Patch the driver's ``execute_query`` to capture what
        # params are actually forwarded.
        tools._services._index.client_for = AsyncMock(  # type: ignore[attr-defined]
            return_value=MagicMock(execute_query=capture_run)
        )
        asyncio.run(
            tools.codeindex_cypher(
                cypher="MATCH (i:Item {project_hash: $proj}) RETURN i LIMIT 1",
                confirm_raw_cypher=True,
            )
        )
        # Cypher passed proj via $proj — the injected value
        # should be in scope.
        assert captured["params"]["proj"] == "test-project"

    def test_no_backend_returns_structured_error(self):
        tools, _ = _make_tools(no_backend=True)
        result = asyncio.run(
            tools.codeindex_cypher(
                cypher=("MATCH (i:Item {project_hash: $proj}) RETURN i LIMIT 1"),
                confirm_raw_cypher=True,
            )
        )
        envelope = json.loads(result)
        assert envelope["error"] == "no_backend"
        assert "No Neo4j backend" in envelope["message"] or "backend" in envelope["message"].lower()

    def test_driver_exception_surfaces_as_structured_error(self):
        tools, _ = _make_tools()
        # Replace client_for so the captured execute_query raises.
        fake_client = MagicMock()
        fake_client.execute_query = AsyncMock(side_effect=RuntimeError("neo4j bolt timeout"))
        tools._services._index.client_for = AsyncMock(  # type: ignore[attr-defined]
            return_value=fake_client
        )
        result = asyncio.run(
            tools.codeindex_cypher(
                cypher=("MATCH (i:Item {project_hash: $proj}) RETURN i LIMIT 1"),
                confirm_raw_cypher=True,
            )
        )
        envelope = json.loads(result)
        assert envelope["error"] == "cypher_failed"
        assert "bolt timeout" in envelope["message"]
        assert "bolt timeout" in envelope["message"]


# ── Group 3: boundary — only the toolkit touches Neo4j ────────────────


class TestCypherBoundary:
    def test_codeindex_cypher_is_the_only_neo4j_seam_for_agents(self):
        """Documented contract: the only agent-facing path
        to Neo4j is the ``codeindex_cypher`` tool surface.

        Inspect the toolkit registration and assert there is
        exactly one CodeIndex tool — ``codeindex_cypher`` —
        and no parallel route to the driver. The typed
        surface (``codeindex_query`` / ``codeindex_tree``) was
        intentionally removed; this test pins that removal
        so a future regression re-enabling them fails here.
        """
        tools, mock_index = _make_tools()
        registered = sorted(
            name
            for name, fn in vars(type(tools)).items()
            if not name.startswith("_") and callable(getattr(tools, name, None))
        )
        # The agent-callable CodeIndex tool — exactly one.
        assert "codeindex_cypher" in registered
        # The typed surface is gone — pinned absent.
        for removed in ("codeindex_query", "codeindex_tree"):
            assert removed not in registered, (
                f"CodeIndexTools.{removed}() reappeared on the "
                "toolkit — cypher-only contract broken."
            )

    def test_execute_query_helper_not_in_agent_surface(self):
        """Defence-in-depth: ``Neo4jClient.execute_query`` is the
        driver seam but must not be a directly-callable tool.
        """
        from ember_code.core.code_index.neo4j_client import Neo4jClient

        # Including the removed typed-tool names here too —
        # even if cypher-only is the agent surface, none of
        # these names should appear as a class attr on the
        # Neo4jClient (which would mean the client is exposing
        # its own bypass of the toolkit).
        for tool_name in ("codeindex_query", "codeindex_tree", "codeindex_cypher"):
            assert not hasattr(Neo4jClient, tool_name), (
                f"Neo4jClient.{tool_name} appearing on the class "
                "would be an agent-callable bypass of the toolkit."
            )


# ── Group 4: typed input/output boundary ──────────────────────────────


class TestCypherServiceTypedBoundary:
    """The toolkit↔service seam is the typed :class:`CypherInput`
    (not a borrowed dict). Pin it explicitly so a future
    regression that falls back to dict-spread surfaces here
    rather than at runtime.
    """

    def test_for_service_returns_typed_cypher_input(self):
        """``CypherInput.for_service()`` returns the same
        :class:`CypherInput`, identity-equivalent. A dict
        return type would be a regression — the service
        signature ``run(input: CypherInput)`` would no longer
        type-check."""
        ci = CypherInput(
            cypher="MATCH (i:Item {project_hash: $proj}) RETURN i LIMIT 5",
            params={"quality": "major-issues"},
            limit=42,
            confirm_raw_cypher=True,
            commit="abc123",
        )
        out = ci.for_service()
        assert isinstance(out, CypherInput)
        assert out is ci, (
            "CypherInput.for_service should return self — the "
            "service takes the typed model directly, no copy needed."
        )
        assert out.cypher == ci.cypher
        assert out.params == ci.params
        assert out.limit == ci.limit
        assert out.commit == ci.commit
        assert out.confirm_raw_cypher is True

    def test_cypher_service_run_signature_accepts_cypher_input(self):
        """``CypherService.run(input: CypherInput)`` is the
        typed seam. Verify the signature accepts a
        :class:`CypherInput` via runtime inspection — a
        regression to dict kwargs would break this.
        """
        # Pull the signature via ``inspect``; the parameter
        # name + annotation are what we care about.
        import inspect

        from ember_code.core.tools.codeindex.cypher_service import CypherService

        sig = inspect.signature(CypherService.run)  # type: ignore[attr-defined]
        params = list(sig.parameters.values())
        # ``self`` + ``input``: exactly two positional-or-keyword
        # parameters, no kwargs spread (the typed model is the
        # whole seam).
        assert [p.name for p in params] == ["self", "input"], (
            f"CypherService.run signature changed shape — "
            f"expected ['self', 'input'], got "
            f"{[p.name for p in params]!r}. The dict-spread "
            "seam must not return without an explicit test "
            "update so reviewers can audit it."
        )
        assert params[-1].annotation in ("CypherInput", "Optional[CypherInput]") or str(
            params[-1].annotation
        ).endswith("CypherInput"), (
            f"Last param of CypherService.run must be "
            f"annotated CypherInput, got {params[-1].annotation!r}."
        )

    def test_tool_routes_typed_input_into_service(self):
        """End-to-end: ``CodeIndexTools.codeindex_cypher``
        builds a typed :class:`CypherInput`, threads it
        through ``CypherService.run``, and never goes
        through a borrowed dict."""
        captured: dict[str, CypherInput] = {}

        async def capture_run(input: CypherInput):
            captured["input"] = input
            from ember_code.core.tools.codeindex.schemas import CypherResponse

            return CypherResponse(
                rows=[],
                row_count=0,
                truncated=False,
                limit=10,
                commit=None,
            )

        mock_index = MagicMock(spec=CodeIndex)
        mock_index.project_id = "x"
        mock_index.client_for = AsyncMock(return_value=None)
        # Stub the CypherService to capture the input.
        tools = CodeIndexTools(project_dir=".", index=mock_index)
        tools._services.cypher = MagicMock(return_value=MagicMock(run=capture_run))

        cypher = "MATCH (i:Item {project_hash: $proj}) RETURN i LIMIT 10"
        asyncio.run(
            tools.codeindex_cypher(
                cypher=cypher,
                confirm_raw_cypher=True,
                limit=10,
            )
        )

        # A CypherInput was passed in to ``run`` — not a dict.
        assert "input" in captured, (
            "CodeIndexTools.codeindex_cypher did not thread a "
            "CypherInput into CypherService.run (no dict spread)."
        )
        assert isinstance(captured["input"], CypherInput)
        assert captured["input"].cypher == cypher
        assert captured["input"].limit == 10
        assert captured["input"].commit is None


class TestTheStringLiteralBypass:
    """A write smuggled inside a string the procedure then executes.

    Found during the production check by probing the guard rather than
    reading it. Two individually-reasonable rules composed into a hole:

    * string literals are stripped before the forbidden-token scan, so
      that ``WHERE n.name = 'CREATE'`` is not rejected;
    * ``apoc.cypher.run`` was on the CALL allowlist, and it *executes*
      its first argument.

    So this passed the read-only guard on a tool the model drives::

        CALL apoc.cypher.run('CREATE (n:X) RETURN n', {})

    The comment beside ``_FORBIDDEN_TOKENS`` asserted the opposite — "a
    CALL that smuggles a write is still caught by the write tokens below"
    — which holds for a subquery and not for a string.

    Fixed by emptying ``_ALLOWED_APOC``: nothing referenced it, so the
    capability had no consumer and the hole was the whole of its effect.
    """

    @pytest.mark.parametrize(
        "query",
        [
            "CALL apoc.cypher.run('CREATE (n:X) RETURN n', {})",
            "CALL apoc.cypher.run('create (n:X) return n', {})",
            "CALL apoc.cypher.run('MATCH (n) DETACH DELETE n', {})",
            'CALL apoc.cypher.run("MERGE (n:X) SET n.p = 1", {})',
            "CALL apoc.cypher.runMany('CREATE (n:X)', {})",
            "CALL apoc.cypher.doIt('CREATE (n:X)', {})",
        ],
    )
    def test_no_apoc_procedure_is_reachable(self, query: str):
        with pytest.raises(CypherGuardError):
            assert_read_only_cypher(query)

    def test_the_allowlist_is_empty_and_stays_that_way(self):
        """A named constant so the reasoning survives, and asserted so
        re-adding an entry is a deliberate act with a test to change.
        Any procedure that runs its argument defeats the token scan."""
        from ember_code.core.tools.codeindex.cypher_guard import _ALLOWED_APOC

        assert frozenset() == _ALLOWED_APOC

    @pytest.mark.parametrize(
        "query",
        [
            # The reads that must survive the removal — the vector index
            # holds 93% of the nodes, and without it the chunk embeddings
            # are unreachable from the only tool an agent has.
            "CALL db.index.vector.queryNodes('chunk_embedding', 5, $query_vector) YIELD node RETURN node",
            "CALL db.index.fulltext.queryNodes('item_name', 'foo') YIELD node RETURN node",
            "MATCH (n:Item) WHERE n.proj = $proj RETURN n LIMIT $limit_n",
            # And the reason string literals are stripped in the first
            # place: a keyword inside one is data, not a statement.
            "MATCH (n) WHERE n.name = 'CREATE' RETURN n",
        ],
    )
    def test_legitimate_reads_are_untouched(self, query: str):
        assert_read_only_cypher(query)


class TestStringLiteralsAreDataNotKeywords:
    """A string literal's contents must not decide the guard's verdict.

    The tokeniser splits on whitespace and Cypher punctuation but not on
    quotes, so a quote glued itself to whichever word it touched. That made
    the verdict depend on where in a string a keyword sat:

        WHERE n.body CONTAINS 'DELETE FROM users'   → tokens 'DELETE, FROM  → allowed
        WHERE n.body CONTAINS 'and then DELETE it'  → tokens ..., DELETE     → rejected

    Same keyword, same kind of string, opposite outcomes — and the rejection
    said "this tool is read-only" about a query that only reads. Searching a
    code graph for source containing `DELETE` or `DROP TABLE` is an ordinary
    request; this tool is pointed at code, so those words are *data* here far
    more often than they are instructions.

    Literals are now redacted before the scan. That is safe because a keyword
    inside a string is only dangerous if something executes the string, and
    nothing can: `_ALLOWED_APOC` is empty precisely because
    `apoc.cypher.run` did exactly that, and the two allowed procedures take
    an index name and a search term.
    """

    @pytest.mark.parametrize(
        "query",
        [
            "MATCH (n) WHERE n.body CONTAINS 'and then DELETE it' RETURN n",
            "MATCH (n) WHERE n.body CONTAINS 'DELETE FROM users' RETURN n",
            "MATCH (n) WHERE n.body CONTAINS 'we CREATE a node here' RETURN n",
            "MATCH (n) WHERE n.body CONTAINS 'op.drop_table(\"users\")' RETURN n",
            'MATCH (n) WHERE n.body CONTAINS "MERGE (a)-[:R]->(b)" RETURN n',
            "MATCH (n) WHERE n.body =~ '.* SET .*' RETURN n",
            # A `;` inside a literal is not a statement separator.
            "MATCH (n) WHERE n.body = 'a;b' RETURN n",
            # Nor is a `$name` inside a literal a parameter reference.
            "MATCH (n) WHERE n.body CONTAINS '$not_a_param' RETURN n",
        ],
    )
    def test_a_keyword_inside_a_literal_is_allowed(self, query):
        assert assert_read_only_cypher(query) == query

    def test_the_query_comes_back_with_its_literals_intact(self):
        """The redaction is for scanning only. Returning the redacted form
        would hand Neo4j a query searching for the empty string, silently
        breaking every search this change exists to permit — a far worse
        outcome than the false rejection it fixes."""
        query = (
            "MATCH (n) WHERE n.body CONTAINS 'DELETE FROM users' AND n.path = \"src/a.py\" RETURN n"
        )

        assert assert_read_only_cypher(query) == query

    @pytest.mark.parametrize(
        "query",
        [
            # Redacting literals must not open any of these.
            "CREATE (n:X) RETURN n",
            "MATCH (n) DETACH DELETE n",
            "MATCH (n) SET n.x = 1 RETURN n",
            "MATCH (a) MERGE (a)-[:R]->(b) RETURN a",
            "MATCH (n) REMOVE n.x RETURN n",
            "DROP INDEX idx",
            "MATCH (n) RETURN n; CREATE (m:X) RETURN m",
            "CALL apoc.cypher.run('CREATE (n:X) RETURN n', {})",
            "CALL dbms.listQueries() YIELD query RETURN query",
            "CALL db.labels() YIELD label RETURN label",
            "PROFILE MATCH (n) RETURN n",
            "SHOW DATABASES",
        ],
    )
    def test_the_writes_and_admin_paths_are_still_closed(self, query):
        with pytest.raises(CypherGuardError):
            assert_read_only_cypher(query)

    def test_an_unterminated_literal_does_not_swallow_a_write(self):
        """The literal regexes require a closing quote, so an unbalanced one
        matches nothing and the text stays visible to the scan. Worth pinning:
        "redact up to the next quote or end of input" would let a lone quote
        hide the rest of the statement."""
        with pytest.raises(CypherGuardError):
            assert_read_only_cypher("MATCH (n) WHERE n.x = 'oops DETACH DELETE n RETURN n")
