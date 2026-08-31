"""CodeIndexTools — agent-facing toolkit.

``codeindex_cypher`` is the only registered agent-facing method.
The typed surface (``codeindex_query`` / ``codeindex_tree``) has
been intentionally removed so the typed kwargs can't drift
ahead of the raw Cypher escape hatch — agents go through
``codeindex_cypher`` exclusively, and the toolkit surface is
the single seam to the Neo4j driver.

Responsibilities:

  - register the agent-facing ``codeindex_cypher`` method with
    the agno toolkit machinery,
  - run the read-only guardrail BEFORE the driver seam is
    touched (``assert_read_only_cypher``),
  - hand the typed ``CypherInput`` to :class:`ToolInvocationRecorder`,
    which owns timing + serialization + telemetry + error-wrap.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agno.tools import Toolkit

from ember_code.core.code_index.index import CodeIndex
from ember_code.core.paths import DEFAULT_DATA_DIR
from ember_code.core.tools.codeindex.cypher_guard import (
    CypherGuardError,
    assert_read_only_cypher,
)
from ember_code.core.tools.codeindex.invocation import ToolInvocationRecorder
from ember_code.core.tools.codeindex.schemas import CypherInput
from ember_code.core.tools.codeindex.serializer import JsonSerializer
from ember_code.core.tools.codeindex.services import CodeIndexServices
from ember_code.core.tools.codeindex.telemetry import TelemetryLog

logger = logging.getLogger(__name__)


class CodeIndexTools(Toolkit):
    """Single-tool Cypher surface over the per-commit code index.

    Args:
        project_dir: project root used to derive the on-disk path.
            Defaults to ``cwd``.
        data_dir: ember root, defaults to ``~/.ember``.
        index: pre-built :class:`CodeIndex` (used by tests / advanced
            callers). When provided, ``project_dir`` and ``data_dir``
            are ignored.
    """

    def __init__(
        self,
        *,
        project_dir: str | Path | None = None,
        data_dir: str | Path = DEFAULT_DATA_DIR,
        index: CodeIndex | None = None,
        **kwargs: Any,
    ):
        super().__init__(name="codeindex", **kwargs)
        self._services = CodeIndexServices(
            project_dir=Path(str(project_dir)) if project_dir else Path.cwd(),
            data_dir=data_dir,
            explicit_index=index,
        )
        self._serializer = JsonSerializer()
        self._recorder = ToolInvocationRecorder(
            serializer=self._serializer,
            telemetry=TelemetryLog(),
        )
        # Cypher is the only registered agent-facing tool. The
        # typed surface was deliberately removed to keep the
        # toolbox single-seam (tests pin this invariant).
        self.register(self.codeindex_cypher)

    @property
    def _explicit_index(self) -> CodeIndex:
        return self._services.index

    async def close(self) -> None:
        """Close the underlying :class:`CodeIndex` if one was opened.

        Matches the historical semantics: whichever :class:`CodeIndex`
        the services hold (whether injected or self-built) is
        closed, regardless of who built it.
        """
        await self._services.close()

    # ── codeindex_cypher — only registered agent-facing tool ───────────

    async def codeindex_cypher(
        self,
        cypher: str,
        params: dict[str, str | int | list[str] | None] | None = None,
        limit: int = 50,
        confirm_raw_cypher: bool = False,
        commit: str | None = None,
    ) -> str:
        """Run a **read-only** raw Cypher query against the CodeIndex.

        This is the only agent-facing path to the indexed data
        store. Specialist agents (the ``data-architect`` agent,
        primarily) author Cypher against the schema documented at
        ``core/code_index/neo4j_schema.GRAPH_SCHEMA_DESCRIPTION``.

        Mandatory safety contract — **any violation is a hard
        refusal, not a soft warning**:

        1. ``confirm_raw_cypher=True`` is required. The kit
           treats ``False`` (or absent) as an explicit deny.
        2. The Cypher must be read-only — no ``CREATE``, ``MERGE``,
           ``SET``, ``DELETE``, ``DETACH DELETE``, ``REMOVE``,
           ``DROP``, ``ALTER``, ``BEGIN`` / ``COMMIT`` / ``ROLLBACK``,
           ``SHOW``, ``PROFILE``, ``CALL dbms.*``. See
           :func:`cypher_guard.assert_read_only_cypher`.

           **Two procedures are allowed**, and both are read-only
           searches you are expected to use:
           ``CALL db.index.vector.queryNodes`` (find code by
           describing what it does) and
           ``CALL db.index.fulltext.queryNodes`` (find a literal
           string in the stored source — the only search that
           returns nothing when the term is absent). This
           docstring previously said ``CALL db.*`` was refused
           outright, which is why neither was ever attempted.
        3. ``$param`` placeholders must name a key on the
           allowlist (``proj``, ``commit_sha``, ``ids``,
           ``limit_n``, ``skip_n``, ``kind``, ``type``,
           ``quality``, ``semantic_query``, ``query_vector``).
           The toolkit injects ``proj`` from
           ``CodeIndex.project_id`` and passes the rest
           through verbatim. ``semantic_query`` is the ergonomic
           one: pass a sentence and the service embeds it with the
           same model the chunks were written with, binding the
           result as ``$query_vector``.

        Project isolation is enforced by the PROCESS boundary:
        each ``(project, commit)`` pair runs in its own Neo4j
        process (see ``neo4j_schema.py``), so no query-time
        ``project_hash`` predicate is needed — the driver simply
        can't reach another project's data.

        The toolkit runs both checks BEFORE the query
        reaches the driver, so a rejection is a
        :class:`CypherGuardError` subclass — no DB round
        trip happens.

        Args:
            cypher: a single read-only Cypher statement.
            params: typed parameter dict (only allowlisted
                names will be forwarded as Cypher ``$name``
                replacements).
            limit: max rows returned (default 50, hard cap
                500). Set to a smaller value when querying
                dense parts of the graph.
            confirm_raw_cypher: must be True. Set False
                explicitly to assert "I have not authorized
                this" — useful when the agent intends to
                reject its own first draft.
            commit: commit SHA; defaults to head.

        Returns: ``CypherResponse`` JSON on success, or
            ``ErrorResponse`` JSON with a stable ``error`` category
            on rejection / failure.
        """
        # Hard-deny first, before the typed-input build, so a
        # caller that forgot the flag doesn't get a graceful
        # error — they get a refusal they have to address.
        if confirm_raw_cypher is not True:
            from ember_code.core.tools.codeindex.schemas import ErrorResponse

            return ErrorResponse(
                error="confirm_required",
                message=(
                    "codeindex_cypher refuses to run without confirm_raw_cypher=True. "
                    "Re-call with the flag explicitly set to acknowledge the read-only "
                    "Cypher is intentional."
                ),
            ).model_dump_json()

        # Typed input catches param shape / limit bounds before
        # the cypher is parsed.
        try:
            params_dict = dict(params or {})
            params_dict_clean = {k: v for k, v in params_dict.items() if v is not None}
            params_obj: dict[str, Any] = params_dict_clean
            cypher_input = CypherInput(
                cypher=cypher,
                params=params_obj,
                limit=limit,
                confirm_raw_cypher=confirm_raw_cypher,
                commit=commit,
            )
        except Exception as exc:
            from ember_code.core.tools.codeindex.schemas import ErrorResponse

            return ErrorResponse(error="bad_input", message=str(exc)).model_dump_json()

        # Validate Cypher — rejection here means the query
        # never leaves this method.
        try:
            validated_cypher = assert_read_only_cypher(cypher_input.cypher)
        except CypherGuardError as exc:
            from ember_code.core.tools.codeindex.schemas import ErrorResponse

            return ErrorResponse(error="cypher_guard", message=str(exc)).model_dump_json()

        # Mirror the typed-input validation that was applied
        # above — preserve the validated string for telemetry.
        cypher_input_dict = cypher_input.model_dump()
        cypher_input_dict["cypher"] = validated_cypher
        runtime_input = CypherInput(**cypher_input_dict)

        return await self._recorder.invoke(
            tool_name="codeindex_cypher",
            telemetry_args=runtime_input.telemetry_dict(),
            coro=self._services.cypher().run(runtime_input),
        )
