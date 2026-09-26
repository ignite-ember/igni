"""Asking the artifact graph a question in English.

Three things carry this feature and each has a way of failing quietly:

* **The guard.** A model writes the query, so "it only reads" cannot
  be an assumption. A rejected write must be rejected before it
  reaches the database, not after.
* **The graph projection.** It is built from whatever columns the rows
  happen to have, which is what keeps the picture honest — it always
  matches the table, including when the query is odd.
* **The retry.** A first attempt that fails is normal. What matters is
  that the second attempt is told *why*, because a blind retry
  reproduces the same mistake.

Live Neo4j is not required: the service takes a driver, so the tests
that are about SQL-shaped behaviour use a fake one and the tests that
are about Cypher semantics live in
``test_artifacts_neo4j_backend.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from ember_code.core.artifacts.cypher import (
    MAX_CELL_CHARS,
    ArtifactCypherService,
    GraphNode,
)
from ember_code.core.artifacts.query_planner import (
    ArtifactQueryPlanner,
    _unfence,
    _wants_semantics,
)


class FakeResult:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records

    async def to_eager_result(self):
        class Eager:
            records = [dict(r) for r in self._records]
            keys = list(self._records[0].keys()) if self._records else []

        return Eager()


class FakeSession:
    def __init__(self, driver: FakeDriver) -> None:
        self._driver = driver

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def run(self, cypher: str, **params: Any):
        self._driver.ran.append((cypher, params))
        if self._driver.raises is not None:
            raise self._driver.raises
        return FakeResult(self._driver.records)


class FakeDriver:
    """Records what was executed; returns canned rows."""

    def __init__(
        self,
        records: list[dict[str, Any]] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.records = records or []
        self.raises = raises
        self.ran: list[tuple[str, dict[str, Any]]] = []

    def session(self, **_: Any) -> FakeSession:
        return FakeSession(self)


class FakeEmbedder:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 384 for _ in texts]


def service(records=None, raises=None, **kw) -> ArtifactCypherService:
    return ArtifactCypherService(
        FakeDriver(records, raises), "proj-hash", embedder=FakeEmbedder(), **kw
    )


READ = "MATCH (a:Artifact) RETURN a.name AS name LIMIT 10"


class TestTheGuardRunsFirst:
    async def test_a_write_never_reaches_the_database(self):
        driver = FakeDriver()
        svc = ArtifactCypherService(driver, "proj")
        result = await svc.run("MATCH (a:Artifact) DETACH DELETE a")

        assert not result.ok
        assert driver.ran == []

    @pytest.mark.parametrize(
        "cypher",
        [
            "CREATE (a:Artifact {name: 'x'})",
            "MATCH (a:Artifact) SET a.name = 'x'",
            "MERGE (a:Artifact {name: 'x'})",
            "MATCH (a:Artifact) REMOVE a.name",
            "DROP INDEX artifact_idx",
        ],
    )
    async def test_every_write_shape_is_refused(self, cypher):
        assert not (await service().run(cypher)).ok

    async def test_the_refusal_explains_itself(self):
        result = await service().run("MATCH (a:Artifact) DELETE a")
        assert "read-only" in result.error.lower() or "not allowed" in result.error.lower()

    async def test_a_keyword_inside_a_string_is_not_a_write(self):
        """Searching documents for the word DELETE is an ordinary
        thing to want."""
        result = await service(records=[{"name": "x"}]).run(
            "MATCH (v:Version) WHERE v.content CONTAINS 'DELETE FROM' "
            "RETURN v.content AS name LIMIT 5"
        )
        assert result.ok

    async def test_the_project_is_bound_without_being_asked_for(self):
        svc = service(records=[{"name": "x"}])
        await svc.run(READ)
        _, params = svc._driver.ran[0]  # type: ignore[attr-defined]
        assert params["proj"] == "proj-hash"


class TestResults:
    async def test_rows_come_back_with_their_columns(self):
        result = await service(records=[{"name": "Auth", "kind": "design"}]).run(READ)
        assert result.rows == [{"name": "Auth", "kind": "design"}]
        assert result.columns == ["name", "kind"]

    async def test_a_long_cell_is_trimmed_with_the_remainder_named(self):
        """A whole document in one table cell makes every other column
        unreadable, and the full text is one click away."""
        long_text = "x" * (MAX_CELL_CHARS + 500)
        result = await service(records=[{"name": long_text}]).run(READ)
        assert len(result.rows[0]["name"]) < len(long_text)
        assert "+500 chars" in result.rows[0]["name"]

    async def test_the_row_cap_is_enforced_and_reported(self):
        rows = [{"name": f"doc-{i}"} for i in range(30)]
        result = await service(records=rows, max_rows=10).run(READ)
        assert len(result.rows) == 10
        assert result.truncated is True

    async def test_a_database_error_comes_back_as_one_readable_line(self):
        boom = RuntimeError("Variable `x` not defined\n  at line 1\n  stack...")
        result = await service(raises=boom).run(READ)
        assert not result.ok
        assert result.error == "Variable `x` not defined"

    async def test_booleans_and_numbers_survive_unchanged(self):
        result = await service(records=[{"n": 3, "head": True, "score": 0.5}]).run(READ)
        assert result.rows[0] == {"n": 3, "head": True, "score": 0.5}


class TestSemanticQueries:
    async def test_the_query_vector_is_bound_from_the_question(self):
        svc = service(records=[{"name": "x"}])
        await svc.run(
            "CALL db.index.vector.queryNodes('version_embedding', 5, $query_vector) "
            "YIELD node, score RETURN node.content AS name LIMIT 5",
            semantic_query="auth tokens",
        )
        _, params = svc._driver.ran[0]  # type: ignore[attr-defined]
        assert len(params["query_vector"]) == 384

    async def test_without_an_embedder_it_says_so_rather_than_failing_oddly(self):
        svc = ArtifactCypherService(FakeDriver(), "proj", embedder=None)
        result = await svc.run(READ, semantic_query="anything")
        assert "embedder" in result.error


class TestTheGraphProjection:
    def _project(self, rows):
        return service().project(rows)

    def test_a_relation_becomes_two_nodes_and_an_edge(self):
        graph = self._project([{"dependent": "Login flow", "source": "Auth model"}])
        assert {n.label for n in graph.nodes} == {"Login flow", "Auth model"}
        assert len(graph.edges) == 1

    def test_a_bare_list_becomes_unconnected_nodes(self):
        graph = self._project([{"name": "A"}, {"name": "B"}])
        assert len(graph.nodes) == 2
        assert graph.edges == []

    def test_rows_with_nothing_identifiable_draw_nothing(self):
        """A `RETURN count(*)` has no graph, and an empty canvas is
        more honest than one mystery dot."""
        assert self._project([{"total": 12}]).is_empty

    def test_the_same_document_twice_is_one_node(self):
        graph = self._project(
            [
                {"artifact_id": "a1", "name": "Auth", "source": "X"},
                {"artifact_id": "a1", "name": "Auth", "source": "Y"},
            ]
        )
        assert len([n for n in graph.nodes if n.kind == "artifact"]) == 1
        assert len(graph.edges) == 2

    def test_a_pin_behind_its_source_is_marked_stale(self):
        graph = self._project(
            [{"dependent": "Login", "source": "Auth", "pinned_to": 1, "now_at": 3}]
        )
        assert graph.edges[0].stale is True
        assert graph.edges[0].label == "v1 → v3"

    def test_a_current_pin_is_not_marked_stale(self):
        graph = self._project(
            [{"dependent": "Login", "source": "Auth", "pinned_to": 3, "now_at": 3}]
        )
        assert graph.edges[0].stale is False

    def test_the_id_is_preferred_over_the_label_for_identity(self):
        graph = self._project([{"artifact_id": "a1", "name": "Auth"}])
        assert graph.nodes[0] == GraphNode(id="a1", label="Auth", kind="artifact")


class FakeAgent:
    """Answers with a scripted sequence, recording the prompts."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.prompts: list[str] = []

    async def arun(self, prompt: str):
        self.prompts.append(prompt)

        class Response:
            content = self._answers.pop(0) if self._answers else ""

        return Response()


def planner(
    answers: list[str], *, records=None, raises=None
) -> tuple[ArtifactQueryPlanner, FakeAgent]:
    agent = FakeAgent(answers)
    return (
        ArtifactQueryPlanner(
            service(records=records, raises=raises),
            model_factory=lambda: None,
            agent_factory=lambda: agent,
        ),
        agent,
    )


class TestPlanning:
    async def test_a_question_becomes_a_query_and_a_result(self):
        plan, _ = planner([READ], records=[{"name": "Auth"}])
        answer = await plan.ask("list everything")
        assert answer.ok
        assert answer.cypher == READ
        assert answer.result is not None
        assert answer.result.rows == [{"name": "Auth"}]

    async def test_a_markdown_fence_is_stripped(self):
        plan, _ = planner([f"```cypher\n{READ}\n```"], records=[{"name": "A"}])
        answer = await plan.ask("list everything")
        assert answer.cypher == READ

    async def test_an_empty_question_asks_nothing(self):
        plan, agent = planner([READ])
        answer = await plan.ask("   ")
        assert not answer.ok
        assert agent.prompts == []

    async def test_unanswerable_is_honoured_rather_than_guessed_at(self):
        """A model with no way to say no invents a query that returns
        nothing, which reads as "there are none"."""
        plan, agent = planner(["UNANSWERABLE"])
        answer = await plan.ask("what is the weather")
        assert answer.unanswerable
        assert "cannot be answered" in answer.error
        assert len(agent.prompts) == 1


class TestTheRetry:
    async def test_a_rejected_query_is_retried_with_the_reason(self):
        plan, agent = planner(["MATCH (a:Artifact) DELETE a", READ], records=[{"name": "Auth"}])
        answer = await plan.ask("list everything")

        assert answer.ok
        assert answer.attempts == 2
        # The second prompt carries the complaint, not just the question.
        assert "rejected" in agent.prompts[1]
        assert "read-only" in agent.prompts[1].lower()

    async def test_it_gives_up_after_one_retry(self):
        plan, agent = planner(["MATCH (a) DELETE a", "MATCH (a) DELETE a", READ])
        answer = await plan.ask("list everything")

        assert not answer.ok
        assert answer.attempts == 2
        assert len(agent.prompts) == 2

    async def test_a_failing_query_is_still_returned_for_editing(self):
        """A query that is nearly right should be one correction away,
        not a dead end."""
        plan, _ = planner(["MATCH (a:Artifact) DELETE a", "MATCH (a:Artifact) DELETE a"])
        answer = await plan.ask("delete everything")
        assert answer.cypher


class TestRunningEditedCypher:
    async def test_it_runs_without_planning(self):
        plan, agent = planner([])
        result = await plan.run_raw(READ)
        assert agent.prompts == []
        assert result.ok

    async def test_a_hand_written_write_is_still_refused(self):
        plan, _ = planner([])
        assert not (await plan.run_raw("MATCH (a) DETACH DELETE a")).ok


class TestHelpers:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("```cypher\nMATCH (a) RETURN a\n```", "MATCH (a) RETURN a"),
            ("```\nMATCH (a) RETURN a\n```", "MATCH (a) RETURN a"),
            ("MATCH (a) RETURN a", "MATCH (a) RETURN a"),
        ],
    )
    def test_fences_are_stripped(self, text, expected):
        assert _unfence(text) == expected

    @pytest.mark.parametrize(
        "question,semantic",
        [
            ("documents about authentication", True),
            ("anything similar to the auth model", True),
            ("list every document", False),
            ("what was the login flow built on", False),
        ],
    )
    def test_semantic_intent_is_detected_from_the_wording(self, question, semantic):
        assert _wants_semantics(question) is semantic
