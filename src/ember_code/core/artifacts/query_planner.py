"""Turn a question into Cypher, once, with the schema in front of it.

The panel's search box takes English. Something has to write the
query, and it is deliberately **not** the session's agent: that agent
carries a conversation, a toolset and a system prompt sized for coding
work, and routing a search box through it would make a panel
interaction cost a full turn and arrive whenever the agent finished
thinking. This is one model call with one job.

Two behaviours are worth knowing about:

**It retries once, with the error.** A model that writes
``MATCH (a:Artifact) RETURN a`` gets told the panel renders columns
and that a whole node is not one. Handing the guard's or the
database's own complaint back is far more effective than a second
blind attempt, and one retry is the whole budget — a second failure
means the question needs a human, and the panel says so while showing
the query that failed so it can be edited by hand.

**It is allowed to refuse.** ``UNANSWERABLE`` is a valid answer, and
an important one: asked something the graph cannot answer, a model
with no way to say no invents a query that returns an empty result,
which reads as "there are none" rather than "I did not understand".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from ember_code.core.artifacts.cypher import ArtifactCypherService, CypherResult
from ember_code.core.artifacts.graph_schema import (
    ARTIFACT_GRAPH_SCHEMA,
    CYPHER_AUTHOR_RULES,
)

logger = logging.getLogger(__name__)

UNANSWERABLE = "UNANSWERABLE"

# One retry. See the module docstring: a second failure is a signal
# that the question needs a person, not more attempts.
MAX_ATTEMPTS = 2

_FENCE_RE = re.compile(r"^\s*```(?:cypher|sql)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)

# Words that mean "find me things like this" rather than "walk this
# structure". Their presence decides whether the planner offers the
# vector index in the prompt — offering it unconditionally made the
# model reach for semantic search to answer "list everything".
_SEMANTIC_HINTS = (
    "about",
    "similar",
    "like",
    "mentions",
    "related to",
    "discuss",
    "explain",
    "concerning",
    "regarding",
)


@dataclass
class PlannedQuery:
    """A question, the query written for it, and what it returned."""

    question: str
    cypher: str = ""
    result: CypherResult | None = None
    attempts: int = 0
    unanswerable: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.result and self.result.ok)


class ArtifactQueryPlanner:
    """Authors Cypher for a natural-language question and runs it."""

    def __init__(
        self,
        service: ArtifactCypherService,
        *,
        model_factory: Any,
        agent_factory: Any | None = None,
    ) -> None:
        self._service = service
        self._model_factory = model_factory
        self._agent_factory = agent_factory

    async def ask(self, question: str) -> PlannedQuery:
        """Plan, run, and retry once on failure."""
        asked = (question or "").strip()
        if not asked:
            return PlannedQuery(question=question, error="Ask something first.")

        plan = PlannedQuery(question=asked)
        feedback = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            plan.attempts = attempt
            try:
                authored = await self._author(asked, feedback=feedback)
            except Exception as exc:
                logger.debug("cypher planning failed: %s", exc)
                plan.error = f"Could not reach the model to write a query: {exc}"
                return plan

            if authored.strip().upper().startswith(UNANSWERABLE):
                plan.unanswerable = True
                plan.error = (
                    "That question cannot be answered from the artifact graph. "
                    "Try asking about documents, their versions, what they were "
                    "built on, or what has gone out of date."
                )
                return plan

            plan.cypher = authored
            result = await self._service.run(
                authored, semantic_query=asked if _wants_semantics(asked) else ""
            )
            plan.result = result
            if result.ok:
                return plan
            # Hand the actual complaint back. A blind second attempt
            # reproduces the same mistake far more often than not.
            feedback = result.error

        plan.error = plan.result.error if plan.result else "Query failed."
        return plan

    async def run_raw(self, cypher: str) -> CypherResult:
        """Run a query the user edited by hand.

        No planning and no retry: the author is a person who can see
        the error and fix it, and silently rewriting their query would
        be worse than showing them what the database said.
        """
        return await self._service.run(cypher)

    # ── The model call ────────────────────────────────────────────────

    async def _author(self, question: str, *, feedback: str) -> str:
        agent = self._build_agent()
        prompt = self._prompt(question, feedback=feedback)
        response = await agent.arun(prompt)
        return _unfence(str(getattr(response, "content", "") or ""))

    def _build_agent(self) -> Any:
        if self._agent_factory is not None:
            return self._agent_factory()
        # Imported here rather than at module scope: this module is
        # pulled in by the BE's controller registry at startup, and
        # Agno's agent module is heavy enough that importing it for a
        # feature nobody has used yet shows up in launch time.
        from agno.agent import Agent

        return Agent(
            name="_artifact_cypher_author",
            model=self._model_factory(),
            instructions=[ARTIFACT_GRAPH_SCHEMA, CYPHER_AUTHOR_RULES],
            markdown=False,
        )

    @staticmethod
    def _prompt(question: str, *, feedback: str) -> str:
        if not feedback:
            return f"Question: {question}"
        return (
            f"Question: {question}\n\n"
            f"Your previous query was rejected:\n{feedback}\n\n"
            "Write a corrected query. Return named columns, always include "
            "a LIMIT, and filter on `head` unless the question is about "
            "version history."
        )


def _unfence(text: str) -> str:
    """Strip a markdown fence the model added despite being told not to."""
    match = _FENCE_RE.match(text)
    return (match.group(1) if match else text).strip()


def _wants_semantics(question: str) -> bool:
    lowered = question.casefold()
    return any(hint in lowered for hint in _SEMANTIC_HINTS)
