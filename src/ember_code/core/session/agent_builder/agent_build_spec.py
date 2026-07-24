"""Typed spec for the ``Agent(...)`` constructor call.

The original ``build_main_agent`` ended with a 25-kwarg call to
``Agent(...)``. That's:

* Impossible to grep for individual fields (each kwarg is a
  local-variable spaghetti away from its origin).
* Impossible to mock in unit tests without stubbing every dep or
  monkey-patching the module symbol.
* Fragile against Agno upgrades — a renamed kwarg silently drops
  its value if callers pass the old name.

Wrapping the payload in a Pydantic model gives us:

* IDE completion and one mock seam (``spec.instantiate(mock)``).
* A single source of truth for the field set so an Agno upgrade
  surfaces as a diff on this file.
* Clearer semantics — the field list documents the wire.

The tradeoff is that Agno's ``Model`` / ``Db`` / ``Learning`` /
toolkit types aren't Pydantic-friendly, so
``arbitrary_types_allowed=True`` disables runtime validation on
those fields. We accept that: the spec's value is IDE support and
diff clarity, not runtime type-checking (Agno validates its own
kwargs at ``Agent.__init__``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class AgentBuildSpec(BaseModel):
    """All 25 kwargs the main ``Agent(...)`` call receives.

    Keep the field order aligned with the Agno constructor's
    signature so diff review flags upstream reorderings quickly.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # ── Identity / execution ──
    name: str
    model: Any
    tools: list[Any]
    # ``list[Any]`` (not ``list[str]``) because entries are
    # normally strings but tests / advanced callers may plug in
    # objects with a ``__str__`` (e.g. lazy templates). Agno
    # normalises the list at ``Agent.__init__`` — validating
    # element type here would false-alarm for those callers
    # without catching any real bug.
    instructions: list[Any]
    markdown: bool
    retries: int

    # ── Session persistence ──
    db: Any
    session_id: str
    user_id: str

    # ── History window ──
    add_history_to_context: bool
    num_history_runs: int

    # ── Memory ──
    enable_agentic_memory: bool
    add_memories_to_context: bool

    # ── Compression ──
    compress_tool_results: bool
    compression_manager: Any

    # ── Session summaries ──
    enable_session_summaries: bool
    add_session_summary_to_context: bool

    # ── Streaming ──
    stream: bool
    stream_events: bool

    # ── Knowledge ──
    knowledge: Any | None
    search_knowledge: bool

    # ── Guardrails ──
    pre_hooks: Any | None

    # ── Learning ──
    learning: Any | None
    add_learnings_to_context: bool

    # ── Tool event hooks ──
    tool_hooks: list[Any]

    def instantiate(self, agent_cls: Any) -> Any:
        """Construct the Agno agent from this spec.

        ``agent_cls`` is passed in explicitly (rather than
        importing ``Agent`` here) so tests can inject a stub and
        the module-alias test-patch seam
        (``ember_code.core.session.core.Agent``) keeps working.

        NOTE — ``arbitrary_types_allowed=True`` on this model
        disables Pydantic runtime validation for the Agno-side
        fields (``model``, ``db``, ``compression_manager``,
        ``learning``, ``tools``, ``pre_hooks``, ``tool_hooks``,
        ``knowledge``). The spec's value is IDE completion, one
        mock seam, and diff clarity — not runtime type-checking.
        """
        # Expanded explicitly (rather than ``**self.model_dump()``)
        # so Agno kwarg renames surface as a diff on this call and
        # so kwarg-order changes don't silently reshape the call.
        return agent_cls(
            name=self.name,
            model=self.model,
            tools=self.tools,
            instructions=self.instructions,
            markdown=self.markdown,
            retries=self.retries,
            db=self.db,
            session_id=self.session_id,
            user_id=self.user_id,
            add_history_to_context=self.add_history_to_context,
            num_history_runs=self.num_history_runs,
            enable_agentic_memory=self.enable_agentic_memory,
            add_memories_to_context=self.add_memories_to_context,
            compress_tool_results=self.compress_tool_results,
            compression_manager=self.compression_manager,
            enable_session_summaries=self.enable_session_summaries,
            add_session_summary_to_context=self.add_session_summary_to_context,
            stream=self.stream,
            stream_events=self.stream_events,
            knowledge=self.knowledge,
            search_knowledge=self.search_knowledge,
            pre_hooks=self.pre_hooks,
            learning=self.learning,
            add_learnings_to_context=self.add_learnings_to_context,
            tool_hooks=self.tool_hooks,
        )
