"""Session-package Pydantic schemas — the single home for
typed request/response shapes shared across the session
sub-modules.

Mirrors the codebase convention (``core/evals/schemas.py``,
``core/hooks/schemas.py``, ``core/agents/schemas.py``): every
raw ``dict``-shaped boundary in the session package is
promoted to a class here so callers cross the API surface
with a validated model (Rule 1).

Housed here:

* :class:`SessionTitle` — value-object for the raw ↔ cleaned
  session-title pair. Owns the model-produced-name
  sanitisation rule via a ``@model_validator`` that populates
  :attr:`cleaned` at construction time. Promoted from a nested
  class on :class:`SessionPersistence` so the shape lives with
  its siblings instead of buried behind a facade.
* :class:`SessionListRow` — typed row for the session-list wire
  shape (``{session_id, name, created_at, updated_at,
  run_count, summary, agent_name}``) with a
  :meth:`from_agno` classmethod that replaces the inline dict
  literal previously in ``SessionPersistence._session_to_wire``.
* :class:`LoadResult` / :class:`PersistResult` /
  :class:`ForkResult` — Pattern-3 envelopes returned by the
  persistence stores.  Callers that adopt them key on
  ``.ok`` / ``.value`` / ``.error`` instead of the historic
  ``except Exception → return default`` swallow.
* :class:`PluginReloadCounts` — return shape for
  :meth:`Session.reload_plugins` (previously inline in
  ``core.py``).
* :class:`McpServerStatus` — typed row for
  :meth:`Session.get_mcp_status` (replaces raw
  ``list[tuple[str, bool]]``).
* :class:`StopHookPayload` / :class:`StopFailureHookPayload` /
  :class:`UserPromptSubmitHookPayload` /
  :class:`PreCompactHookPayload` /
  :class:`PostCompactHookPayload` /
  :class:`SessionLifecyclePayload` — typed builders for the
  hook events emitted by Session's message / compaction /
  interactive paths. The wider :class:`HookExecutor` still
  accepts ``dict``; callers ``.model_dump()`` at emit time
  so the boundary is a partial win until the executor
  adopts the union too.
* :class:`ContextBreakdown` — token accounting for ``/ctx``
  (moved out of the old ``compact_ops`` module so it lives
  next to its sibling hook payloads).
* :class:`CompactResult` — Pattern-3 envelope returned by
  :meth:`CompactionCoordinator.compact` and
  :meth:`CompactionCoordinator.force_compact` so callers stop
  tuple-unpacking / ``str | None``-checking at every call site.
* :class:`InteractiveBanner` — the typed view-model rendered
  at the top of every ``run_session_interactive`` REPL. Owns
  its own ``render(display)`` method so the five-line banner
  emission lives next to the shape it depends on.
* :class:`LoopPhase` / :class:`LoopAdvance` — ``/loop`` state
  machine phase enum + the tri-shaped advance descriptor
  emitted by :meth:`LoopController.advance_loop`. Housed here
  (not on ``loop_ops.py``) because they are the wire shapes
  crossed by every ``/loop`` RPC and the schemas-home rule
  applies (Rule 1).
* :class:`PlanDecisionResult` — Pattern-3 envelope returned by
  :meth:`PlanCoordinator.approve` /
  :meth:`PlanCoordinator.dismiss`. Housed here (not on
  ``plan_ops.py``) for the same reason as the loop schemas —
  it's the wire shape crossed by every plan-decision RPC and
  the schemas-home rule applies (Rule 1).
* :class:`PlanDecidedBroadcast` — typed payload for the
  ``plan_decided`` broadcast channel emitted after a plan is
  approved / dismissed. Kept alongside
  :class:`PlanDecisionResult` because both cross the same RPC
  surface with the same run_id / decision pair.
* :class:`OutputStyleChangedBroadcast` — typed payload for the
  ``output_style_changed`` broadcast channel emitted by
  :meth:`RuntimeModeCoordinator.set_output_style` when the user
  flips the active output style at runtime. Rule 1 / Pattern 2:
  emit sites call :meth:`model_dump` instead of hand-rolling
  the payload dict.
* :class:`PermissionModeChangedBroadcast` — typed payload for
  the ``permission_mode_changed`` broadcast channel emitted by
  :meth:`RuntimeModeCoordinator.set_permission_mode`. Fields
  carry :class:`PermissionMode` enums; emit sites dump with
  ``mode="json"`` so the wire keeps its string shape.
* :class:`McpClientBundle` — typed wrapper around the
  ``name → client`` mapping crossing into
  :meth:`AgentPool.build_agents`. Replaces the raw
  ``dict[str, Any]`` for the Rule 1 boundary.
* :class:`McpInitResult` — Pattern-3 envelope returned by
  :meth:`McpInitPhase.ensure` (and forwarded by
  :meth:`SessionStartupCoordinator.ensure_mcp`) so callers can
  branch on connected/failed servers without grep-parsing logs.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ember_code.core.config.permission_eval import PermissionMode
from ember_code.core.tools.plan import PlanDecision

if TYPE_CHECKING:
    from ember_code.core.utils.display import DisplayManager


# ── Persistence-layer wire + envelope schemas ────────────────
#
# Promoted here from the pre-refactor ``persistence.py`` god
# class so the persistence sub-package (listing / naming /
# forking / plan-decisions / todos / event-log stores) has a
# stable schemas-home for every shape it crosses at the wire /
# return boundary.

_T = TypeVar("_T")


class SessionTitle(BaseModel):
    """Value-object for a raw ↔ cleaned session-title pair.

    Titles from the auto-namer occasionally come back wrapped in
    markdown decoration (``**Title**`` / ``# Title`` / ``"Title"``);
    the class-level :attr:`_TRIM_RE` strips the leading/trailing
    runs before persisting so the session list doesn't read like a
    raw model response.

    Populated via a ``@model_validator(mode="after")`` so
    :attr:`cleaned` is set exactly once at construction. The
    validator MUST NOT rewrite :attr:`raw` — callers rely on the
    raw ↔ cleaned pair to decide whether the cleaned form differs
    from what Agno persisted (see :meth:`SessionNamer.auto_name`).
    """

    model_config = ConfigDict(frozen=False)

    # Compiled once at class-definition time; every construction
    # reuses the same pattern so repeat cleans stay allocation-free.
    _TRIM_RE: ClassVar[re.Pattern[str]] = re.compile(r"^[\s*_`'\"#]+|[\s*_`'\"]+$")

    raw: str = ""
    cleaned: str = ""

    @model_validator(mode="after")
    def _populate_cleaned(self) -> SessionTitle:
        """Compute :attr:`cleaned` from :attr:`raw` at construction.

        Deliberately does NOT touch :attr:`raw` — the pair is
        load-bearing at the auto-name callsite (compares the freshly
        written raw string against the cleaned form and re-persists
        only when the two differ).
        """
        # Coerce non-string ``raw`` to an empty string at the
        # boundary — Agno's session_data dict is ``dict[str, Any]``
        # and a stray non-string would otherwise crash the regex.
        raw_str = self.raw if isinstance(self.raw, str) else ""
        object.__setattr__(self, "raw", raw_str)
        object.__setattr__(self, "cleaned", self._TRIM_RE.sub("", raw_str))
        return self

    @classmethod
    def clean(cls, raw: str) -> str:
        """Convenience: strip wrappers from ``raw`` in one call."""
        return cls(raw=raw).cleaned


class SessionListRow(BaseModel):
    """Typed wire row for :meth:`SessionListing.list_sessions`.

    Replaces the pre-refactor inline dict literal built by
    ``SessionPersistence._session_to_wire``. The FE-facing shape
    stays the same (``session_id`` / ``name`` / ``created_at`` /
    ``updated_at`` / ``run_count`` / ``summary`` / ``agent_name``);
    consumers that adopt the model reach for named attributes
    instead of dict-key access.
    """

    session_id: str = ""
    name: str = ""
    created_at: int = 0
    updated_at: int = 0
    run_count: int = 0
    summary: str = ""
    agent_name: str = ""

    @classmethod
    def from_agno(cls, s: Any) -> SessionListRow:
        """Build one row from an Agno ``AgentSession``-like object.

        Defensive against the historic shape variance (rows written
        by earlier BE versions may have ``session_data``,
        ``agent_data``, or ``summary`` set to ``None``).
        """
        run_count = len(s.runs) if s.runs else 0
        summary = ""
        if s.summary and hasattr(s.summary, "summary"):
            summary = s.summary.summary or ""
        agent_name = ""
        if s.agent_data and isinstance(s.agent_data, dict):
            agent_name = s.agent_data.get("name", "")
        name = ""
        if s.session_data and isinstance(s.session_data, dict):
            name = s.session_data.get("session_name", "")
        return cls(
            session_id=s.session_id,
            name=name,
            created_at=s.created_at or 0,
            updated_at=s.updated_at or 0,
            run_count=run_count,
            summary=summary,
            agent_name=agent_name,
        )


class LoadResult(BaseModel, Generic[_T]):
    """Pattern-3 envelope for the persistence stores' load path.

    Fields:

    * ``ok`` — ``True`` when the load ran to completion (even if
      the on-disk value was absent — ``value`` is ``None`` in that
      case). ``False`` only when the DB layer raised.
    * ``value`` — the parsed value on hit; ``None`` on miss or
      failure.
    * ``error`` — human-readable error string when ``ok=False``;
      ``None`` on success or empty-value branches.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    ok: bool = True
    value: _T | None = None
    error: str | None = None


class PersistResult(BaseModel):
    """Pattern-3 envelope for the persistence stores' save path.

    Fields:

    * ``ok`` — ``True`` when the DB write completed; ``False`` on
      any DB-layer failure.
    * ``error`` — human-readable error string when ``ok=False``.

    Consumed by :class:`PlanCoordinator` on the persist-then-flip
    invariant path: an ``ok=False`` result on approve aborts the
    mode flip so a restart doesn't surface ``mode=default`` with no
    recorded approval (the original bug that motivated persisting
    plan decisions in the first place).
    """

    ok: bool = True
    error: str | None = None


class ForkResult(BaseModel):
    """Pattern-3 envelope for :meth:`SessionForker.fork`.

    Fields:

    * ``ok`` — ``True`` when the new row was upserted.
    * ``new_session_id`` — the freshly-minted 8-char id on success;
      empty string on failure.
    * ``error`` — human-readable error string when ``ok=False``.
    """

    ok: bool = True
    new_session_id: str = ""
    error: str | None = None


class PluginReloadCounts(BaseModel):
    """Return shape for :meth:`Session.reload_plugins` — a summary
    of how many items were re-wired after the disk scan.

    Callers surface these to the user in a hot-reload confirmation
    ("Active now — N skill(s), M agent(s), K hook(s)"). Modelling
    the shape once here keeps every consumer type-safe (Rule 1).
    """

    plugins: int
    skills: int
    agents: int
    hooks: int


class McpServerStatus(BaseModel):
    """Typed row for :meth:`Session.get_mcp_status`.

    Replaces the raw ``(name, connected)`` tuple so consumers
    (backend RPC, TUI panel) reach for named attributes instead
    of positional indexing.
    """

    name: str
    connected: bool


# ── Hook payloads ────────────────────────────────────────────
#
# The five hook events emitted by Session's message /
# compaction paths. Each carries the minimum context a
# subscriber needs to react (``session_id`` + the event-
# specific fields). Callers ``.model_dump()`` at the executor
# boundary; the executor accepts ``dict``.


class UserPromptSubmitHookPayload(BaseModel):
    """Payload for the ``UserPromptSubmit`` hook — fired once
    per user turn before the model sees the message.

    Blocking subscribers can veto the turn by returning
    ``should_continue=False`` from the executor result.
    """

    message: str
    session_id: str


class StopHookPayload(BaseModel):
    """Payload for the ``Stop`` hook — fired after the model
    produces a response. Non-blocking observers; blocking
    subscribers can force a re-generate by returning
    ``should_continue=False`` with a critique message."""

    session_id: str
    response: str


class StopFailureHookPayload(BaseModel):
    """Payload for the ``StopFailure`` hook — mirror of ``Stop``
    on the failure path. Observation-only (crash reporters,
    alerting)."""

    session_id: str
    error: str
    error_type: str


class PreCompactHookPayload(BaseModel):
    """Payload for the ``PreCompact`` hook — fired BEFORE the
    summariser runs and history is dropped. Subscribers can
    veto the compaction by returning
    ``should_continue=False`` (auto path respects it; the
    user can always retry manually).

    ``scope`` is ``"auto"`` (80% threshold) or ``"manual"``
    (``/compact``). ``tokens_before`` is the input-token
    count that triggered the auto path; ``0`` on the manual
    path where we don't have a recent metric.
    """

    session_id: str
    scope: str
    tokens_before: int


class PostCompactHookPayload(BaseModel):
    """Payload for the ``PostCompact`` hook — observation-only
    fire after the summariser has run and history has been
    cleared. Cannot undo at this stage.

    ``summary_chars`` is only populated on the manual path
    (0 otherwise). Extra fields are allowed so subscribers
    can enrich the payload without a schema change here.
    """

    model_config = ConfigDict(extra="allow")

    session_id: str
    scope: str
    tokens_before: int
    summary_chars: int = 0


# ── Compaction domain models ─────────────────────────────────
#
# ``ContextBreakdown`` reports per-component token accounting
# for the ``/ctx`` slash command; ``CompactResult`` is the
# Pattern-3 envelope both the auto and manual compaction paths
# return. Both live here (not on the coordinator) so any
# consumer — including the view layer in
# ``backend/schemas_context.py`` — sees a stable
# ``session.schemas.*`` import path.


class ContextBreakdown(BaseModel):
    """Per-component token breakdown of the current context.

    ``total = runs + floor`` (``floor`` clamped to ``0`` in case
    of tokenizer inconsistency where ``runs`` > ``total``).
    Consumed by the ``/ctx`` slash command to explain the
    irreducible portion ``/compact`` cannot shrink.

    Construct via :meth:`from_totals` so the floor-clamp
    invariant lives on the model, not scattered at every call
    site.
    """

    total: int
    runs: int
    floor: int

    @classmethod
    def from_totals(cls, total: int, runs: int) -> ContextBreakdown:
        """Build a breakdown from ``total`` + ``runs`` token counts,
        clamping ``floor`` to ``0`` (never negative).

        Owns the ``total == runs + floor`` invariant: if a tokenizer
        bug reports ``runs > total``, the floor still lands at ``0``
        so the ``/ctx`` panel doesn't render a negative pill.
        """
        return cls(total=total, runs=runs, floor=max(0, total - runs))


class CompactResult(BaseModel):
    """Pattern-3 envelope returned by the compaction paths.

    Replaces the pre-refactor ``str | None`` return of
    ``compact()`` and the ad-hoc ``(status, summary)`` tuple of
    ``force_compact()`` with a single typed shape.

    Fields:

    * ``ok`` — ``True`` when the compaction pass completed and
      a non-empty summary was produced. ``False`` means either
      the summariser errored, the summariser returned an empty
      string, or a PreCompact hook vetoed the pass.
    * ``status`` — human-readable status line surfaced by the
      ``/compact`` slash command (e.g. ``"Context compacted."``,
      ``"Nothing to compact — no conversation history."``, or
      the specific error text).
    * ``summary`` — the generated summary text (empty when
      ``ok=False`` or on the auto path where the summary isn't
      surfaced back to the user).
    * ``error`` — the underlying error string when the
      summariser raised; ``None`` on success or empty-summary
      branches.
    """

    ok: bool = True
    status: str = ""
    summary: str = ""
    error: str | None = None


# ── Interactive session payloads ─────────────────────────────
#
# ``SessionLifecyclePayload`` and ``InteractiveBanner`` sit
# next to the hook-payload family above. They are consumed by
# :class:`InteractiveSessionLoop` in ``interactive_loop.py`` —
# the SessionStart / SessionEnd emit sites use
# :meth:`SessionLifecyclePayload.model_dump` at the executor
# boundary (mirroring :class:`StopHookPayload`), while the
# banner owns its own ``render`` method so the five welcome
# lines live in one place.


class SessionLifecyclePayload(BaseModel):
    """Payload for the ``SessionStart`` and ``SessionEnd`` hooks —
    fired once at REPL startup and once at REPL shutdown.

    Follows the same shape convention as :class:`StopHookPayload`
    et al.: the minimum context a subscriber needs to react
    (``session_id`` plus the ``resumed`` flag so analytics /
    hook subscribers can distinguish fresh from resumed
    sessions). Callers ``.model_dump()`` at the executor
    boundary, which still accepts ``dict``.
    """

    session_id: str
    resumed: bool = False


class InteractiveBanner(BaseModel):
    """View-model for the REPL welcome banner printed at the top
    of every :class:`InteractiveSessionLoop` run.

    Built once in ``InteractiveSessionLoop._print_banner`` from
    the settings + pool + skill_pool + hooks_map snapshot, and
    then rendered by :meth:`render` — replacing the five ad-hoc
    ``print_info`` calls previously inlined in the interactive
    loop.

    The ``render`` method deliberately lives on the model
    (co-locating the view-side sequencing with the shape it
    depends on) rather than as a free function in the loop
    module. This is a considered exception to the "schemas are
    pure data" convention in this file — the rendering logic
    is small (five sequential display calls) and has no other
    plausible home.
    """

    version: str
    tip: str
    update_message: str | None = None
    agent_names: list[str] = []
    skill_names: list[str] = []
    hook_count: int = 0
    session_id: str
    resumed: bool = False

    def render(self, display: DisplayManager) -> None:
        """Emit the banner lines to ``display`` in a fixed order.

        Order: welcome panel → contextual tip → optional update
        warning → agent/skill/hook load summaries → session id
        line (annotated ``(resumed)`` when the loop was launched
        via ``--resume``).
        """
        display.print_welcome(self.version)
        display.print_info(f"Tip: {self.tip}")
        if self.update_message:
            display.print_warning(self.update_message)
        if self.agent_names:
            display.print_info(f"Loaded agents: {', '.join(self.agent_names)}")
        if self.skill_names:
            display.print_info(f"Loaded skills: {', '.join('/' + n for n in self.skill_names)}")
        if self.hook_count:
            display.print_info(f"Loaded hooks: {self.hook_count}")
        if self.resumed:
            display.print_info(f"Session: {self.session_id} (resumed)")
        else:
            display.print_info(f"Session: {self.session_id}")


# ── ``/loop`` state machine schemas ──────────────────────────
#
# The two wire shapes for the ``/loop`` controller: the phase
# enum (readers consult :attr:`LoopController.phase` for the
# derived lifecycle) and the tri-shaped advance descriptor
# emitted by :meth:`LoopController.advance_loop`. Housed here
# so ``loop_ops.py`` stays a pure controller module.


class LoopPhase(str, Enum):
    """High-level lifecycle of the ``/loop`` state machine.

    Derived from ``pending_loop_prompt`` + ``paused``: readers
    consult :attr:`LoopController.phase`, never assign it. The
    :class:`LoopController` uses ``_transition`` internally to
    enforce that phase transitions go IDLE → RUNNING → PAUSED
    → RUNNING → IDLE (illegal jumps raise ``ValueError``).
    """

    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"


class LoopAdvance(BaseModel):
    """Wire shape for :meth:`LoopController.advance_loop`.

    Three effective variants, discriminated by ``kind``:

    * ``kind="completed"`` — explicit cap hit; ``total_iterations``
      carries the final count.
    * ``kind="safety_paused"`` — implicit loop hit ``LOOP_HARD_CAP``;
      ``iteration`` carries the count reached.
    * ``kind="step"`` — normal advance: ``prompt`` /
      ``display_prompt`` / ``iteration`` / ``remaining`` /
      ``cap_explicit`` populated. ``auto_extended`` flags the
      one-shot iteration right after the implicit safety net rolled
      over.

    Flat shape (rather than a tagged union) so wire consumers keep
    the same JSON payload; a model-level validator enforces that
    each ``kind`` only carries its own fields. Build via the three
    named classmethods — they are the only spellings
    :class:`LoopController` uses.
    """

    kind: Literal["completed", "safety_paused", "step"] = "step"
    # Completion fields.
    completed: bool = False
    total_iterations: int = 0
    # Safety-cap fields.
    safety_cap_paused: bool = False
    # Step fields.
    prompt: str = ""
    display_prompt: str = ""
    iteration: int = 0
    remaining: int = 0
    cap_explicit: bool = False
    auto_extended: bool = False

    @model_validator(mode="after")
    def _enforce_kind_invariants(self) -> LoopAdvance:
        """Reject shapes that mix fields across variants.

        Pattern-matching callers may safely key on ``kind`` because
        the fields of the other two variants are guaranteed empty.
        """
        if self.kind == "completed":
            if self.safety_cap_paused or self.prompt or self.display_prompt:
                raise ValueError("LoopAdvance(kind='completed') cannot carry step / safety fields")
        elif self.kind == "safety_paused":
            if self.completed or self.total_iterations or self.prompt or self.display_prompt:
                raise ValueError(
                    "LoopAdvance(kind='safety_paused') cannot carry step / completed fields"
                )
        else:  # kind == "step"
            if self.completed or self.total_iterations or self.safety_cap_paused:
                raise ValueError("LoopAdvance(kind='step') cannot carry completed / safety fields")
        return self

    @classmethod
    def completed_at(cls, total: int) -> LoopAdvance:
        """Build the ``kind="completed"`` descriptor for an
        explicit-cap loop that just terminated at its user-declared
        N. ``total`` is that N. Legacy ``.completed`` /
        ``.total_iterations`` accessors return the same values.
        """
        return cls(kind="completed", completed=True, total_iterations=total)

    @classmethod
    def safety_paused(cls, iteration: int) -> LoopAdvance:
        """Build the ``kind="safety_paused"`` descriptor for an
        implicit-cap loop that just hit ``LOOP_HARD_CAP`` and paused
        (rather than terminating). ``iteration`` is the count at the
        pause. Legacy ``.safety_cap_paused`` returns ``True``.
        """
        return cls(
            kind="safety_paused",
            safety_cap_paused=True,
            iteration=iteration,
        )

    @classmethod
    def step(
        cls,
        *,
        prompt: str,
        display_prompt: str,
        iteration: int,
        remaining: int,
        cap_explicit: bool,
        auto_extended: bool,
    ) -> LoopAdvance:
        """Build the ``kind="step"`` descriptor for a normal advance.

        ``prompt`` is the wrapper-decorated iteration text the agent
        sees; ``display_prompt`` is the bare user text for chat
        rendering. ``auto_extended`` is a one-shot flag surfaced on
        the *next* iteration after the implicit safety net rolled
        over.
        """
        return cls(
            kind="step",
            prompt=prompt,
            display_prompt=display_prompt,
            iteration=iteration,
            remaining=remaining,
            cap_explicit=cap_explicit,
            auto_extended=auto_extended,
        )


# ── Plan-decision wire schemas ───────────────────────────────
#
# Return envelope + broadcast payload for the plan-decision
# coordinator. Housed here (not on ``plan_ops.py``) so every
# RPC / broadcast consumer sees a stable ``session.schemas.*``
# import path.


class PlanDecisionResult(BaseModel):
    """Pattern-3 envelope for :meth:`PlanCoordinator.approve` /
    :meth:`PlanCoordinator.dismiss`.

    Surfaced to the FE via the ``APPROVE_PLAN`` / ``DISMISS_PLAN``
    RPCs; the transport serializer auto-converts via
    :meth:`model_dump` so the wire keeps its dict shape.

    Fields:

    * ``run_id`` — the run in which ``exit_plan_mode`` was
      called; identifies the specific plan the user decided on.
    * ``decision`` — the :class:`PlanDecision` StrEnum value the
      user chose (``"approved"`` / ``"dismissed"``). StrEnum
      dumps as its ``.value`` on the wire so existing consumers
      keying on ``"approved"`` / ``"dismissed"`` are unaffected.
    * ``mode_status`` — human-readable one-line summary of any
      permission-mode change triggered by the decision (empty
      for dismiss; ``"mode → default"``-shaped for approve).
    * ``ok`` — ``True`` on success; ``False`` when the
      coordinator refused the record (empty run_id, missing
      plan_store, missing persistence, or DB failure on the
      flip path). Callers key on this to surface an inline
      error to the FE.
    * ``error`` — human-readable error string when ``ok=False``;
      ``None`` on success.
    """

    run_id: str
    decision: PlanDecision
    mode_status: str = ""
    ok: bool = True
    error: str | None = None


class PlanDecidedBroadcast(BaseModel):
    """Typed payload for the ``plan_decided`` broadcast channel.

    The FE listens on this channel to flip the plan card from
    pending to approved/dismissed. Modelled as a Pydantic
    schema so the emit site calls :meth:`model_dump` instead of
    hand-rolling the payload dict (Rule 1 / Pattern 2).
    """

    run_id: str
    decision: PlanDecision


class OutputStyleChangedBroadcast(BaseModel):
    """Typed payload for the ``output_style_changed`` broadcast
    channel.

    Emitted by :meth:`RuntimeModeCoordinator.set_output_style`
    after the active style has been swapped and the main team's
    ``instructions`` list has been hot-patched. The FE consumes
    it to refresh the status chip that shows the active style
    without polling. Modelled as a Pydantic schema so the emit
    site calls :meth:`model_dump` instead of hand-rolling the
    payload dict (Rule 1 / Pattern 2).
    """

    style: str
    previous: str


class PermissionModeChangedBroadcast(BaseModel):
    """Typed payload for the ``permission_mode_changed`` broadcast
    channel.

    Emitted by :meth:`RuntimeModeCoordinator.set_permission_mode`
    after the live :class:`PermissionEvaluator` mode flip. Fields
    carry :class:`PermissionMode` enums; emit sites dump with
    ``mode="json"`` so the wire keeps its historic string shape
    (mirrors the :class:`PlanDecidedBroadcast` / :class:`PlanDecision`
    precedent). Rule 1 / Pattern 2: the emit site calls
    :meth:`model_dump` instead of hand-rolling the payload dict.
    """

    mode: PermissionMode
    previous: PermissionMode


# ── MCP startup schemas ─────────────────────────────────────
#
# Typed wrapper for the ``name → client`` mapping and the
# Pattern-3 envelope returned by :meth:`McpInitPhase.ensure`.
# Kept alongside the other startup wire shapes so the whole
# session-boot API surface is discoverable in one place.


class McpClientBundle(BaseModel):
    """Typed wrapper around the ``name → live MCP client`` mapping
    that crosses into :meth:`AgentPool.build_agents`.

    ``clients`` is a plain ``dict`` (client instances are Agno
    runtime objects, not Pydantic models), but the wrapper
    replaces bare ``dict[str, Any]`` across the boundary so
    downstream code reaches for ``bundle.clients`` / iterates
    ``bundle.names`` instead of hand-rolling the mapping.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    clients: dict[str, Any] = Field(default_factory=dict)

    @property
    def names(self) -> list[str]:
        """Sorted list of connected server names in the bundle."""
        return sorted(self.clients.keys())

    def __bool__(self) -> bool:
        return bool(self.clients)

    def __len__(self) -> int:
        return len(self.clients)


class MessageMedia(BaseModel):
    """Typed media payload for :meth:`Session.handle_message`.

    Replaces the pre-refactor ``**media_kwargs`` blob at the
    session-message boundary (Rule 1 / AP5). Each field is a list
    forwarded verbatim to :meth:`agno.Agent.arun`; the wire shape
    is compatible with Agno's own multimodal contract.

    ``arbitrary_types_allowed`` because Agno's media objects are
    runtime instances rather than Pydantic models. Callers pass
    typed objects OR raw dicts (Agno's ``Image`` / ``Audio`` /
    ``Video`` / ``File`` deserialize both).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    images: list[Any] | None = None
    audio: list[Any] | None = None
    videos: list[Any] | None = None
    files: list[Any] | None = None

    def to_kwargs(self) -> dict[str, Any]:
        """Return the non-``None`` fields as an ``arun``-compatible
        kwargs dict. Empty when no media was supplied — the caller
        can splat the result into ``team.arun(**media.to_kwargs())``
        and get the same shape as the legacy ``**media_kwargs``
        idiom without the untyped boundary.
        """
        out: dict[str, Any] = {}
        if self.images is not None:
            out["images"] = self.images
        if self.audio is not None:
            out["audio"] = self.audio
        if self.videos is not None:
            out["videos"] = self.videos
        if self.files is not None:
            out["files"] = self.files
        return out


class McpInitResult(BaseModel):
    """Pattern-3 envelope returned by :meth:`McpInitPhase.ensure`
    (forwarded by :meth:`SessionStartupCoordinator.ensure_mcp`).

    Callers used to grep the log stream to know how many servers
    connected on first-message init. The envelope surfaces the
    same information at the return-value boundary so RPC clients
    (``BackendServer.ensure_mcp`` / ``McpController.ensure``) can
    project it back to the FE without side-channel scraping.

    Fields:

    * ``connected`` — sorted names of servers that came online.
    * ``failed`` — mapping ``name → error string`` for each server
      whose ``connect`` returned ``None``.
    * ``rebuilt`` — ``True`` iff the agent pool + main team were
      rebuilt with at least one live client (matches the pre-
      refactor log line "agents + main team rebuilt").
    * ``skipped_reason`` — set when :meth:`ensure` short-circuited
      before reaching the connect loop. ``None`` when a connect
      pass was actually executed.
    """

    connected: list[str] = Field(default_factory=list)
    failed: dict[str, str] = Field(default_factory=dict)
    rebuilt: bool = False
    skipped_reason: (
        Literal["already_initialized", "no_configured_servers", "no_clients_connected"] | None
    ) = None
