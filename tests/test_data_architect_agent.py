"""Tests for the ``data-architect`` agent that owns Cypher access.

Three contracts:

  1. The agent file parses into a valid ``AgentDefinition``.
  2. The agent's declared ``tools: [CodeIndex, Bash, ...]``
     resolves to a toolkit list that includes ``CodeIndexTools``
     (so ``codeindex_cypher`` is registered on it). No other
     Cypher / Neo4j route is exposed.
  3. The agent body's safety contract — ``confirm_raw_cypher``
     and the read-only token list — is pinned so a future prompt
     rewrite can't silently drop the guardrails. (Project isolation
     is a PROCESS boundary, not a query predicate — see
     ``neo4j_schema.py`` — so no ``project_hash`` predicate is
     required in the prompt.)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from ember_code.core.agents.markdown import AgentMarkdownFile
from ember_code.core.agents.schemas import AgentDefinition
from ember_code.core.code_index.index import CodeIndex
from ember_code.core.tools.codeindex.tool import CodeIndexTools

AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"
DATA_ARCH = AGENTS_DIR / "data-architect.codeindex.md"


class TestDataArchitectParsing:
    def test_agent_file_exists(self):
        assert DATA_ARCH.exists(), (
            "data-architect.codeindex.md missing — agents using codeindex_cypher "
            "have no curated counterpart to lean on"
        )

    def test_agent_parses_into_agent_definition(self):
        md = AgentMarkdownFile(DATA_ARCH)
        definition: AgentDefinition = md.parse()
        assert isinstance(definition, AgentDefinition)
        assert definition.name == "data-architect"

    def test_agent_metadata_pins_keys(self):
        """Metadata fields are pinned so a future tag rewrite
        forces a deliberate test update instead of silent drift
        (the audit's "missing tests = drift magnet" finding)."""
        md = AgentMarkdownFile(DATA_ARCH)
        definition = md.parse()
        assert "CodeIndex" in definition.tools
        assert "Bash" in definition.tools
        assert "cypher" in definition.tags
        assert "read-only" in definition.tags
        assert "codeindex" in definition.tags
        # Read-only — the agent must not be able to write.
        assert definition.can_orchestrate is True  # can fan out
        # ``body`` carries the system prompt; meta fields stay
        # non-overlapping with body keywords.
        assert "confirm_raw_cypher" in definition.system_prompt

    def test_body_must_mention_safety_contract(self):
        """The body must call out the four invariants so a copy
        edit can't drop them silently. ``codeindex_cypher`` is
        the only CodeIndex tool the agent has — pinned here so
        the typed-tool regressions are caught at test time."""
        md = AgentMarkdownFile(DATA_ARCH)
        body = md.parse().system_prompt
        for required in (
            "codeindex_cypher",  # ONLY agent-facing CodeIndex tool
            "confirm_raw_cypher=True",
            "Read-only",  # the section heading spelling
            "CodeIndex",  # toolkit name in body
        ):
            assert required in body, (
                f"data-architect body missing required safety / capability keyword: {required!r}"
            )

        # Defence against typed-tool regression: the body must
        # NOT advertise the deprecated ``codeindex_query`` /
        # ``codeindex_tree`` surface. Pinning the absence here
        # so a future copy edit that brings them back fails
        # the test rather than silently restoring the dual
        # surface.
        for banned in (
            "Run `codeindex_query`",
            "use codeindex_query",
            "call `codeindex_tree(id=",
        ):
            assert banned not in body, (
                f"data-architect body must not steer the agent to a removed tool — found {banned!r}"
            )

        # Read-only contract keywords. The body of the prompt
        # currently lists "Rejected: CREATE, MERGE, SET, REMOVE,
        # DELETE, DETACH DELETE, DROP, ALTER, RENAME, BEGIN/COMMIT/
        # ROLLBACK, SHOW, PROFILE, CALL dbms.* / CALL db.*". The
        # test asserts the body mentions each individual token
        # so a future prompt rewrite that drops one of them
        # surfaces the gap rather than silently changing the
        # agent's contract.
        for write_token in (
            "CREATE",
            "MERGE",
            "DETACH DELETE",
            "DROP",
            "BEGIN/COMMIT/ROLLBACK",
            "SHOW",
            "PROFILE",
            "CALL dbms.* / CALL db.*",
        ):
            assert write_token in body, (
                f"data-architect body's forbidden-tokens table is missing entry {write_token!r}"
            )


class TestDataArchitectToolSurface:
    def test_codeindex_tools_registers_only_cypher(self):
        """``codeindex_cypher`` is the only CodeIndex tool
        registered with the agno toolkit surface. ``codeindex_query``
        and ``codeindex_tree`` were removed so the typed
        surface can't drift ahead of the raw one. This test
        is a regression guard: if anyone re-adds them, the
        toolkit now exposes the wider surface and this
        test will fail.
        """
        mock_index = MagicMock(spec=CodeIndex)
        mock_index.project_id = "x"
        tools = CodeIndexTools(project_dir=".", index=mock_index)

        # Agno picks up only the public methods registered on
        # ``self`` (via ``Toolkit.register``). Enumerate them.
        registered = sorted(
            name
            for name in (
                "codeindex_query",
                "codeindex_tree",
                "codeindex_cypher",
            )
            if callable(getattr(tools, name, None))
        )
        # Cypher is the only one that should be present.
        assert registered == ["codeindex_cypher"], (
            f"CodeIndexTools surfaced extra CodeIndex tools: "
            f"{registered!r}. Restore cypher-only by deleting "
            "the typed methods or update this test + the agent "
            "body to acknowledge the multi-tool surface."
        )

    def test_no_typed_or_helper_methods_on_toolkit(self):
        """Defence-in-depth: there is **no** shortcut
        ``run_cypher`` / ``execute_neo4j`` / similar method
        that an agent could call bypassing the
        ``codeindex_cypher`` tool surface. The typed
        surface (``codeindex_query`` / ``codeindex_tree``)
        is also gone, so this test covers both axes."""
        mock_index = MagicMock(spec=CodeIndex)
        mock_index.project_id = "x"
        tools = CodeIndexTools(project_dir=".", index=mock_index)
        for forbidden in (
            "codeindex_query",
            "codeindex_tree",
            "run_cypher",
            "execute_neo4j",
            "raw_query",
            "direct_cypher",
        ):
            assert not hasattr(tools, forbidden), (
                f"CodeIndexTools.{forbidden}() appearing on the "
                "instance would be an agent-callable bypass of "
                "codeindex_cypher"
            )

    def test_tools_minimum_invoke_path(self):
        """Smoke: instantiating the toolkit and calling
        ``codeindex_cypher`` with the confirm flag refused path
        doesn't require a real Neo4j (DB seam never reached).
        """
        mock_index = MagicMock(spec=CodeIndex)
        mock_index.project_id = "x"
        mock_index.client_for = AsyncMock(return_value=None)
        tools = CodeIndexTools(project_dir=".", index=mock_index)

        result = asyncio.run(
            tools.codeindex_cypher(
                # Write op — read-only guardrail rejects before the driver seam.
                # (We used to test missing-project_hash here, but project
                # isolation is a PROCESS boundary now, not a query predicate.)
                cypher="CREATE (n:Item) RETURN n",
                confirm_raw_cypher=True,
            )
        )
        # Hard denial from the guardrail before the driver seam
        # is touched — ``client_for`` was never awaited.
        assert '"cypher_guard"' in result
        assert mock_index.client_for.await_count == 0
