"""Typed schemas for the chat-history rebuild pipeline.

Extracted out of :mod:`ember_code.backend.server_history` — the old
free-function module used raw dict literals for every turn shape, ~30
loose ``getattr(msg, ..., default)`` calls to read Agno's per-message
struct, and ``payload.get("json")`` to peek into
``visualization_delta`` event payloads. Every dict / loose-attr shape
at a boundary now lives here as a Pydantic model so mypy + Ruff +
Pydantic validation give schema coverage at every seam. Same
rationale as the sibling :mod:`schemas_run` module.

Consumers:

* :class:`ChatTurn` — the tagged union of every wire-shape a
  rebuilt chat can emit. Each subclass has a ``role: Literal[...]``
  discriminator so :meth:`BaseModel.model_dump` round-trips through
  the exact same ``{"role": "...", ...}`` dict the FE and search
  helpers expect.
* :class:`AgnoRunMessageView` — the one-shot typed wrapper the
  walker builds from an Agno persisted-message record at the top
  of each iteration. Replaces the ~30 ``getattr`` reads. Uses
  ``extra='ignore'`` (not ``allow``) so unknown Agno fields drop
  silently without ballooning the model — a schema rename upstream
  is caught by the per-field defaults kicking in.
* :class:`VisualizationDeltaPayload` — cast at the splicer's read
  boundary so ``payload.spec_id`` / ``payload.spec_json`` are real
  attribute reads instead of ``payload.get("json")``. Lives here
  rather than next to :class:`SessionEvent` because the producer
  side is in ``core/tools/orchestrate_streaming.py`` and moving
  the type would be a cross-package change out of scope for this
  refactor.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# :class:`SessionListEntry` is defined in :mod:`ember_code.protocol.messages`
# (the leaf layer — protocol never depends on backend) and re-exported
# here so backend callers can pick it up alongside the other chat-history
# schemas from one place. Kept as an explicit re-export in ``__all__``
# so ``from schemas_history import SessionListEntry`` keeps working.
from ember_code.backend.schemas_plan import PlanState
from ember_code.protocol.messages import SessionListEntry


class _RunOwned(BaseModel):
    """Shared base for turns that carry ``run_id`` on the wire.

    Every rebuilt-history turn carries ``run_id`` so the FE can
    map back to the owning Agno run for edit/delete truncation.
    ``created_at`` is Agno-issued epoch seconds and is set only on
    conversational turns (user/assistant/thinking/tool/plan) — the
    per-run ``stats`` badge and spliced ``visualization`` turns
    intentionally omit ``created_at`` on the wire to match the
    pre-refactor free-function output exactly.
    """

    run_id: str = ""


class _TimedTurn(_RunOwned):
    """Conversational turn with a per-message wall-clock stamp."""

    created_at: int = 0


class UserTurn(_TimedTurn):
    """A user-input turn."""

    role: Literal["user"] = "user"
    content: str = ""


class AssistantTurn(_TimedTurn):
    """An assistant-visible-reply turn (may be one of many per run
    when the original stream interleaved ``<think>`` blocks)."""

    role: Literal["assistant"] = "assistant"
    content: str = ""


class ThinkingTurn(_TimedTurn):
    """A synthesized ``thinking`` card — either from Agno's
    ``reasoning_content`` sidecar or from an inline ``<think>``
    block in the assistant content."""

    role: Literal["thinking"] = "thinking"
    content: str = ""


class ToolTurn(_TimedTurn):
    """A rebuilt tool card (previously emitted live as
    ``tool_started`` + ``tool_completed`` events)."""

    role: Literal["tool"] = "tool"
    tool_name: str = ""
    friendly_name: str = ""
    args: str = ""
    content: str = ""
    is_error: bool = False


class PlanTurn(_TimedTurn):
    """A PlanCard turn synthesized in place of an ``exit_plan_mode``
    tool result. ``state`` uses the empty-string sentinel during
    the walk — the post-walk pass rewrites it to
    ``"pending"``/``"approved"``/``"dismissed"``. The empty-string
    default is preserved (rather than ``None``) so the wire dump
    keeps the same shape the FE reads today."""

    role: Literal["plan"] = "plan"
    plan: str = ""
    tasks: list[Any] = Field(default_factory=list)
    state: PlanState = ""


class StatsTurn(_RunOwned):
    """The per-run input/output token badge. ``duration`` stays a
    float even when zero — the FE consumer treats ``0`` and
    ``0.0`` interchangeably but the persisted wire is float. No
    ``created_at`` on the wire (matches the pre-refactor output)."""

    role: Literal["stats"] = "stats"
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    duration: float = 0.0


class VisualizationTurn(_RunOwned):
    """A visualizer card spliced from the session event log.

    ``spec`` stays a raw dict — the visualizer subagent owns the
    JSON schema and we don't validate it here. ``seq`` is the
    :class:`SessionEvent.seq` for ordering within a run. No
    ``created_at`` on the wire — the FE derives display time from
    the neighboring turns."""

    role: Literal["visualization"] = "visualization"
    spec_id: str = ""
    spec: dict[str, Any] = Field(default_factory=dict)
    source_agent: str = "visualizer"
    seq: int = 0


#: Discriminated union of every turn kind the rebuilder can emit.
#: Serialization uses ``role`` as the discriminator so every
#: ``model_dump()`` at the wire boundary lands on the same key-set
#: the FE reads today.
ChatTurn = Annotated[
    UserTurn | AssistantTurn | ThinkingTurn | ToolTurn | PlanTurn | StatsTurn | VisualizationTurn,
    Field(discriminator="role"),
]


class AgnoRunMessageView(BaseModel):
    """Typed view over one persisted Agno run message.

    Built once at the top of the walk via
    ``AgnoRunMessageView.model_validate(msg, from_attributes=True)``
    so the per-message body reads as attribute access instead of
    thirty ``getattr(msg, ...)`` calls.

    Field types mirror Agno's ``Message`` class
    (``agno/models/message.py``): every attribute except ``role``
    and ``content`` is ``Optional`` and defaults to ``None`` on
    non-tool messages. The ``_coerce_none`` validator collapses
    those ``None`` values back to a usable non-None default (``""``
    / ``False`` / ``[]``) so downstream handlers can treat every
    field as populated — matching the tolerance the pre-refactor
    ``getattr(msg, name, default)`` reads gave us.

    ``extra='ignore'`` (not ``'allow'``) — Agno's message struct
    isn't ours to lock down, but we don't need unknown fields to
    ride along on the model either.
    """

    model_config = ConfigDict(extra="ignore")

    role: str = ""
    content: Any = ""
    created_at: int = 0
    from_history: bool = False
    tool_name: str = ""
    tool_call_id: str = ""
    tool_args: Any = None
    tool_call_error: bool = False
    reasoning_content: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator(
        "role",
        "tool_name",
        "tool_call_id",
        "reasoning_content",
        mode="before",
    )
    @classmethod
    def _coerce_str_none(cls, v: Any) -> str:
        return "" if v is None else v

    @field_validator("tool_call_error", "from_history", mode="before")
    @classmethod
    def _coerce_bool_none(cls, v: Any) -> bool:
        return False if v is None else v

    @field_validator("created_at", mode="before")
    @classmethod
    def _coerce_int_none(cls, v: Any) -> int:
        return 0 if v is None else v

    @field_validator("tool_calls", mode="before")
    @classmethod
    def _coerce_list_none(cls, v: Any) -> list[dict[str, Any]]:
        return [] if v is None else v

    @property
    def content_str(self) -> str:
        """Coerce ``content`` to string with the same guard the old
        code used (``isinstance(content, str)`` else ``str(content or
        "")``). Some Agno providers put a list-of-parts here."""
        c = self.content
        if isinstance(c, str):
            return c
        return str(c or "")


class AgnoRunMetricsView(BaseModel):
    """Typed view over an Agno run's ``metrics`` struct.

    Same tolerance shape as :class:`AgnoRunMessageView` — every
    field defaults to a usable zero so a missing metrics object
    (older Agno versions / test fixtures) can't crash the stats
    line."""

    model_config = ConfigDict(extra="ignore")

    reasoning_tokens: int = 0
    duration: float = 0.0

    @field_validator("reasoning_tokens", mode="before")
    @classmethod
    def _coerce_int_none(cls, v: Any) -> int:
        return 0 if v is None else v

    @field_validator("duration", mode="before")
    @classmethod
    def _coerce_float_none(cls, v: Any) -> float:
        return 0.0 if v is None else v


class AgnoRunView(BaseModel):
    """Typed view over one persisted Agno run.

    Wraps ``run_id`` + ``parent_run_id`` + ``messages`` +
    ``metrics`` access with the same ``getattr(run, name,
    default)`` tolerance the pre-refactor code used. ``messages``
    stays ``list[Any]`` because we validate each entry through
    :class:`AgnoRunMessageView` inside the walker (per-message
    validation gives us a fresh error site if a specific message
    is malformed instead of aborting the whole run at load time).
    """

    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    run_id: str = ""
    parent_run_id: str = ""
    messages: list[Any] = Field(default_factory=list)
    metrics: AgnoRunMetricsView | None = None

    @field_validator("run_id", "parent_run_id", mode="before")
    @classmethod
    def _coerce_str_none(cls, v: Any) -> str:
        return "" if v is None else str(v)

    @field_validator("messages", mode="before")
    @classmethod
    def _coerce_list_none(cls, v: Any) -> list[Any]:
        return [] if v is None else v

    @field_validator("metrics", mode="before")
    @classmethod
    def _coerce_metrics(cls, v: Any) -> Any:
        if v is None:
            return None
        # Accept an Agno metrics struct (attribute access) or a
        # dict — both flow through Pydantic validation.
        if isinstance(v, dict):
            return v
        return AgnoRunMetricsView.model_validate(v, from_attributes=True)


class VisualizationDeltaPayload(BaseModel):
    """Payload cast for ``visualization_delta`` events.

    Persisted wire keys are ``spec_id`` and ``json`` (the raw JSON
    string of the visualizer's spec). ``json`` collides with
    ``BaseModel.json`` (deprecated but still bound on v2), so we
    name the field ``spec_json`` and bind it to the wire key via
    :class:`~pydantic.Field` alias. ``model_validate`` reads the
    ``json`` key from the persisted payload; a hypothetical
    ``model_dump(by_alias=True)`` would emit it back.

    ``extra='ignore'`` — future producers may add fields we don't
    care about at read time.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    spec_id: str = ""
    spec_json: str = Field(default="", alias="json")


class ChatHistoryEntry(BaseModel):
    """Wire-shape entry for one chat-history turn.

    The pipeline is: :meth:`ChatHistoryRebuilder.rebuild` produces a
    list of :class:`ChatTurn` (a discriminated union); each is
    ``model_dump(mode="json")``ed at the RPC boundary in
    :meth:`BackendServer.get_chat_history`; the resulting dicts are
    the input to :meth:`SessionsController.search_chat` (which
    re-validates them into typed :class:`ChatHistoryEntry` instances
    at the seam and forwards to
    :meth:`ChatHistorySearcher.search`).

    This model is that dict shape typed. It is intentionally
    permissive (every field defaults, ``extra='allow'``) so a
    ``ChatTurn.model_dump()`` from ANY of the seven turn subclasses
    validates cleanly — the discriminator ``role`` is preserved and
    downstream code can still branch on it, while turn-specific
    fields (``tool_name``, ``spec``, …) survive via ``extra='allow'``.

    Using this model as the type of the wire boundary gets us schema
    documentation without narrowing the actual payload — the RPC
    contract stays byte-identical.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    role: str = ""
    run_id: str = ""
    content: str = ""
    created_at: int = 0


class ChatSearchHit(BaseModel):
    """One match returned by
    :meth:`SessionsController.search_chat` /
    :meth:`ember_code.backend.chat_history_searcher.ChatHistorySearcher.search`.

    Wire shape: ``{history_index, role, run_id, snippet, match_start,
    match_end, created_at}``. ``history_index`` MUST align with the
    emission order of :meth:`ChatHistoryRebuilder.rebuild` — the FE
    keeps a parallel ``historyIndex → itemIndex`` map built at
    session load, so any drift breaks the "click result → jump to
    chat item" mapping.

    ``match_start`` / ``match_end`` are positions within
    ``snippet`` (not the original content) — keeps the FE highlight
    logic trivial.
    """

    history_index: int = 0
    role: str = ""
    run_id: str = ""
    snippet: str = ""
    match_start: int = 0
    match_end: int = 0
    # Epoch seconds (Agno-issued) — the FE formats it into a
    # relative "2h ago" / locale time string per row.
    created_at: int = 0


__all__ = [
    "UserTurn",
    "AssistantTurn",
    "ThinkingTurn",
    "ToolTurn",
    "PlanTurn",
    "StatsTurn",
    "VisualizationTurn",
    "ChatTurn",
    "AgnoRunMessageView",
    "AgnoRunView",
    "AgnoRunMetricsView",
    "VisualizationDeltaPayload",
    "ChatHistoryEntry",
    "ChatSearchHit",
    "SessionListEntry",
]
