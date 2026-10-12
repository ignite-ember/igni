"""The graph schema handed to the model must not contradict itself.

``GRAPH_SCHEMA_DESCRIPTION`` is the live contract: ``core/prompts`` substitutes
it wherever ``{{CODEINDEX_GRAPH_SCHEMA}}`` appears, so every CodeIndex-aware
agent reads it verbatim. It said both things about the same property —

    :Item … ``project_hash`` … **Do not filter on it.**
    Scoping rules … always pass ``project_hash`` on every query

— and one of them was wrong. ``project_hash`` holds the project id and
``$proj`` is bound to exactly that value by the cypher service, so
``{project_hash: $proj}`` is both correct and what every internal query does.
What genuinely does not work is a *literal*: the value is a SHA-256 prefix, not
a repository name, so ``STARTS WITH 'myrepo'`` returns an empty result that
reads like a real answer.
"""

from __future__ import annotations

import re

from ember_code.core.code_index.neo4j_schema import GRAPH_SCHEMA_DESCRIPTION
from ember_code.core.tools.codeindex.cypher_guard import (
    ALLOWED_PARAM_NAMES,
    assert_read_only_cypher,
)

_REFUSALS = re.compile(
    r"(do not|don't|never)[^.]{0,40}filter on (it|``project_hash``|project_hash)",
    re.IGNORECASE,
)


#: An instruction that the agent *must* scope. Paired with a refusal, these are
#: the two halves the agent cannot satisfy at once.
_REQUIREMENTS = re.compile(
    r"(always|every query|must)[^.]{0,60}(?:scope|pass|filter)[^.]{0,40}project_hash"
    r"|scope every query[^.]{0,40}project_hash",
    re.IGNORECASE,
)


class TestItSaysOneThingAboutProjectScope:
    def test_it_does_not_both_require_and_refuse_the_project_filter(self):
        """The contradiction, not either half of it.

        This test used to assert the schema never says "do not filter on it",
        which was right only while the document also said "always scope every
        query". The tenancy model settled the question the other way — one
        process holds one ``(project, commit)``, so the predicate is
        unnecessary and ``cypher_guard`` deleted its check for it — and the
        schema now says so in three places. Under the old assertion, telling
        the truth failed the test.

        What must never come back is the *pair*.
        """
        refusals = _REFUSALS.findall(GRAPH_SCHEMA_DESCRIPTION)
        requirements = _REQUIREMENTS.findall(GRAPH_SCHEMA_DESCRIPTION)
        assert not (refusals and requirements), (
            f"the schema refuses the project filter ({refusals}) and requires it "
            f"({requirements}) — the agent cannot satisfy both"
        )

    def test_it_states_the_filter_is_unnecessary(self):
        """The current doctrine, pinned so a revert is visible.

        22,801 of 22,802 recorded Cypher calls omit the predicate, so the
        demonstrations and the instruction now agree. If this fails, check
        ``cypher_guard`` — the two move together.
        """
        assert "you do not need it" in GRAPH_SCHEMA_DESCRIPTION.lower()
        assert "process boundary is what" in GRAPH_SCHEMA_DESCRIPTION

    def test_every_scoping_instruction_names_the_bound_parameter(self):
        """``$proj`` is injected by the service; the agent never supplies it.

        An instruction to 'pass project_hash' without naming ``$proj`` is what
        sent agents off inventing a literal for it.
        """
        offenders = [
            line
            for line in GRAPH_SCHEMA_DESCRIPTION.splitlines()
            if "project_hash" in line
            and re.search(r"\balways\b", line, re.IGNORECASE)
            and "$proj" not in line
        ]
        assert not offenders, offenders

    def test_the_measured_warning_about_literals_survives(self):
        """The real failure — 3 of 12 evaluation runs — is still described."""
        assert "STARTS WITH 'myrepo'" in GRAPH_SCHEMA_DESCRIPTION
        assert "3 of 12" in GRAPH_SCHEMA_DESCRIPTION


class TestItSaysWhereTheProjectFilterDoesNotApply:
    """:Chunk nodes carry no ``project_hash``.

    ``Neo4jClient`` writes chunk properties without one — they are scoped by
    the :Item they hang off — so the blanket "scope every query with
    ``{project_hash: $proj}``" is unsatisfiable for the one node type the
    document's own search recipes match on. An agent that generalises the
    rule to a chunk MATCH, or to the WHERE after a ``chunk_text`` call, gets
    an empty result that reads like "this repository does not do that".
    """

    def test_the_exception_for_chunks_is_stated(self):
        section = GRAPH_SCHEMA_DESCRIPTION.split("## Scoping rules")[1]
        assert "``:Chunk`` nodes carry **no** ``project_hash``" in section

    def test_it_shows_how_to_scope_a_chunk_instead(self):
        assert "(i:Item {project_hash: $proj})-[:HAS_CHUNK]->(c:Chunk)" in GRAPH_SCHEMA_DESCRIPTION

    def test_no_chunk_recipe_filters_a_chunk_on_project_hash(self):
        """Matching a chunk *through* its :Item is the taught form; putting the
        predicate on the chunk itself is the one that silently returns nothing."""
        offenders = re.findall(
            r"(?:c:Chunk \{[^}]*project_hash|\b(?:c|ch|node)\.project_hash)",
            GRAPH_SCHEMA_DESCRIPTION,
        )
        assert not offenders, offenders


class TestTheAdviceMatchesTheTool:
    def test_proj_is_a_parameter_the_guard_accepts(self):
        assert "proj" in ALLOWED_PARAM_NAMES

    def test_the_scoping_idiom_the_schema_teaches_passes_the_guard(self):
        query = (
            "MATCH (i:Item {project_hash: $proj}) WHERE i.security = 'critical' RETURN i LIMIT 10"
        )
        assert assert_read_only_cypher(query) == query
