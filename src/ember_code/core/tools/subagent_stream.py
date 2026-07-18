"""Sub-agent stream state — Pydantic models that own the mutation
logic for the two streaming handlers in
:mod:`orchestrate_streaming`.

Why this exists: before this file, ``_run_agent_streaming``'s nested
``_handle`` closure declared 11 nonlocals to track content buffers,
run-id capture, visualizer streaming throttle state, and completion
markers. Every new feature added another nonlocal. Adding a nonlocal
is a two-line change (``nonlocal foo; ... foo = ...``) with no schema
enforcement — the ceiling for that pattern is a bug per addition
because typos become new locals silently.

The refactor is model-first, per CODE_STANDARDS.md Pattern 4
(composition over god-classes) + AP2 (>5 nonlocals in a function is
a smell). One Pydantic model holds every piece of per-stream state
AND owns the mutation methods that used to be inline in the handler
closure. Adding a new field is a one-line change on the model + a
call site; adding new behaviour is a new method on the model, not a
new free function taking ``state`` as its first arg.
"""

from __future__ import annotations

import time
import uuid

from pydantic import BaseModel, ConfigDict, Field

from ember_code.core.tools.orchestrate_preview import PREVIEWS


class SubAgentStreamState(BaseModel):
    """Per-invocation state for the sub-agent stream handler.

    One instance is constructed at the top of every sub-agent stream
    and mutated across the event loop. Fields group by concern:

    - **Identity**: what agent this is, how the FE identifies it.
    - **Activity log**: parent-recap lines, not FE-facing.
    - **Content preview**: token-buffered lines for the FE tree view.
    - **Visualizer streaming**: partial-JSON throttle state.
    - **Agno run identity**: run/session/parent-run ids captured from
      the first event that carries them.
    - **Completion tracking**: belt-and-suspenders emit flag,
      backup final-content capture.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # ── Identity ────────────────────────────────────────────────────
    #: Dot-joined agent path, stable across events for one run.
    #: ``"root"`` for top-level runs; ``"architect.editor"`` for
    #: nested sub-agent runs. FE uses this as the tree-node id.
    agent_path_id: str
    #: Sticky card identifier stamped on every emitted event so the
    #: FE routes them all to the same progress card. Empty when the
    #: caller didn't provide one.
    card_id: str = ""

    # ── Activity log ────────────────────────────────────────────────
    #: One line per notable event (tool call, HITL pause, error).
    #: Concatenated into the parent agent's tool-return so the model
    #: can recap what the sub-agent did. NOT sent to the FE.
    log: list[str] = Field(default_factory=list)

    # ── Content preview state ───────────────────────────────────────
    #: Currently-executing tool name, for the "└─ result" pairing.
    current_tool: str | None = None
    #: Last content-preview emission time (``time.monotonic``).
    #: Throttles the ``content_preview`` event to ~2 Hz so the FE
    #: doesn't drown in token-sized deltas.
    last_update: float = 0.0
    #: Last preview text we emitted — dedup so identical previews
    #: don't spam the wire when the tail hasn't advanced.
    last_preview: str = ""
    #: Accumulated content stream. Agno yields deltas that are often
    #: single tokens; we buffer here and slice the tail for the
    #: multi-line preview.
    content_buf: str = ""

    # ── Visualizer streaming state ──────────────────────────────────
    #: Stable spec_id for THIS sub-agent's visualize call — used by
    #: the FE to dedupe multiple progressive deltas onto one card.
    #: Generated fresh per stream so parallel visualizer calls don't
    #: collide. Constructor default assigns a hex UUID prefix.
    vis_spec_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    #: Length of the last-emitted ``arguments_partial`` string, to
    #: dedup emissions when the accumulator advanced by zero bytes.
    vis_last_emitted_len: int = 0
    #: Last visualization-delta emission time (``time.monotonic``).
    #: 50 ms throttle prevents wire spam on fast models.
    vis_last_emit_at: float = 0.0

    # ── Agno run identity ───────────────────────────────────────────
    #: Sub-agent's own run_id (Agno UUID). Latched onto the first
    #: event that carries it — Agno only yields ``RunStartedEvent``
    #: when ``stream_events=True``, and we deliberately don't pass
    #: that (specialist streams shouldn't spam lifecycle), so
    #: RunStarted-only capture would leave this ``None`` and the
    #: ``aget_run_output`` fallback would silently miss.
    current_run_id: str | None = None
    #: Sub-agent's session_id. Same latch-first-non-empty rule.
    #: Needed for the ``aget_run_output(run_id, session_id)`` lookup
    #: that reads the final answer after the stream ends.
    current_session_id: str | None = None
    #: ``RunStartedEvent.parent_run_id`` — the TOP-LEVEL run that
    #: spawned this sub-agent. This is what ``get_chat_history``
    #: splicing keys on when placing visualizer cards next to their
    #: originating ``spawn_agent`` tool call. Using
    #: ``current_run_id`` here would fall through to the tail-append
    #: fallback and lose the correct positioning.
    parent_top_run_id: str | None = None

    # ── Completion tracking ─────────────────────────────────────────
    #: True once we've emitted an ``agent_completed`` event for this
    #: sub-agent. Agno's specialist ``arun`` doesn't yield
    #: ``RunCompletedEvent`` unless ``stream_events=True``, and the
    #: FE relies on ``agent_completed`` to stop the spinning card.
    #: Set when either (a) a real RunCompletedEvent arrives or (b)
    #: the post-loop belt-and-suspenders synthesises one. Guards
    #: against double-emission.
    agent_completed_emitted: bool = False
    #: Backup capture of the final answer from ``RunCompletedEvent``.
    #: ``aget_run_output`` is the canonical source, but the async DB
    #: write that backs it sometimes hasn't flushed by the time the
    #: stream ends — the streamed RunCompletedEvent already carries
    #: the full content, so we hold it as a fallback.
    completed_content: str = ""

    # ── Behaviour ──────────────────────────────────────────────────
    def latch_ids(self, event: object) -> bool:
        """Latch ``run_id`` / ``session_id`` / ``parent_run_id`` from
        the first event that carries each.

        Agno only yields ``RunStartedEvent`` when
        ``stream_events=True``, which we don't pass to keep specialist
        streams quiet, so a ``RunStartedEvent``-only capture would
        leave both fields ``None`` and our
        ``aget_run_output(run_id, session_id)`` lookup would silently
        return ``None``. Every Agno run event carries these fields,
        so latching onto the first non-empty value we see is
        sufficient and stable across pause/resume.

        Returns ``True`` iff this call latched ``current_run_id`` for
        the first time — signal for the caller to register with the
        cancellation registry.
        """
        newly_run = False
        if not self.current_run_id:
            ev_run_id = getattr(event, "run_id", None)
            if ev_run_id:
                self.current_run_id = ev_run_id
                newly_run = True
        if not self.current_session_id:
            ev_session_id = getattr(event, "session_id", None)
            if ev_session_id:
                self.current_session_id = ev_session_id
        if not self.parent_top_run_id:
            ev_parent = getattr(event, "parent_run_id", None)
            if ev_parent:
                self.parent_top_run_id = ev_parent
        return newly_run

    def can_emit_vis_delta(self, now_s: float) -> bool:
        """50ms throttle for visualizer partial-JSON deltas.

        First delta always emits so the FE mounts the card early.
        Subsequent deltas need at least 50ms of wall-clock separation
        from the previous emission — keeps the wire quiet on fast
        models while still feeling live.
        """
        return self.vis_last_emitted_len == 0 or (now_s - self.vis_last_emit_at >= 0.05)

    def record_vis_emission(self, now_s: float, partial_len: int) -> None:
        """Note that we just emitted a delta at ``now_s`` for a
        partial-args string of length ``partial_len``."""
        self.vis_last_emitted_len = partial_len
        self.vis_last_emit_at = now_s

    def record_completion(self, content: str | None, _metrics: object = None) -> None:
        """Keep the streamed ``RunCompletedEvent.content`` as a
        fallback in case the DB-backed lookup comes up empty."""
        if content:
            self.completed_content = str(content)
        self.agent_completed_emitted = True

    def append_content_delta(self, chunk: str) -> str | None:
        """Buffer a streaming content chunk and return a preview
        string when the ~2 Hz throttle fires AND the preview text
        actually changed.

        Returns ``None`` when the throttle window hasn't elapsed or
        the preview text matches the last one we emitted.
        """
        if not chunk:
            return None
        self.content_buf += str(chunk)
        now = time.monotonic()
        if now - self.last_update <= 0.5:
            return None
        self.last_update = now
        preview = PREVIEWS.format_content_buffer(self.content_buf)
        if not preview or preview == self.last_preview:
            return None
        self.last_preview = preview
        return preview


class TeamStreamState(BaseModel):
    """Per-invocation state for the team stream handler.

    Sibling of :class:`SubAgentStreamState` — same "model instead of
    nonlocals" refactor, but shaped for the team case. Two things
    differ from the single-agent state:

    1. Throttle/dedup state is keyed by ``agent_path_id`` (dict per
       agent) because in ``broadcast`` and ``coordinate`` modes
       multiple members emit interleaved deltas; a shared last-
       update would let one chatty specialist starve the others.
    2. There's a ``team_path_id`` — the dot-joined base identity of
       the whole team run, used as the tree-node id for the
       coordinator itself and as a namespace prefix for member
       events.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # ── Identity ────────────────────────────────────────────────────
    #: Dot-joined base identity for this team run. ``"team"`` for
    #: top-level teams; ``"architect.review_team"`` when the team is
    #: itself a member of a parent team.
    team_path_id: str
    #: Sticky card id — same semantic as :attr:`SubAgentStreamState.card_id`.
    card_id: str = ""

    # ── Activity log ────────────────────────────────────────────────
    log: list[str] = Field(default_factory=list)

    # ── Content preview state (per-member keyed) ────────────────────
    #: Currently-executing tool name — shared across members because
    #: only one tool executes at a time within a single specialist,
    #: and interleaved calls belong to different specialists (each
    #: agent's own _handle sees its own current_tool via the event's
    #: agent_name).
    current_tool: str | None = None
    #: Currently-executing agent name, latched onto the first event
    #: from a fresh specialist so tool events without an explicit
    #: agent_name (rare, but Agno's team coordinator occasionally
    #: drops it) route to the right node.
    current_agent: str = ""
    #: Last content-preview emission time per member (throttled to
    #: ~2 Hz per agent). Keyed by agent_path_id so one chatty member
    #: can't starve the others.
    last_update_by_agent: dict[str, float] = Field(default_factory=dict)
    #: Last preview text per member (dedup key).
    last_preview_by_agent: dict[str, str] = Field(default_factory=dict)
    #: Content buffer per member — Agno yields deltas that are often
    #: single tokens; we buffer here and slice the tail for the
    #: multi-line preview.
    content_buf_by_agent: dict[str, str] = Field(default_factory=dict)

    # ── Agno run identity ───────────────────────────────────────────
    #: Team's own run_id (latch-first).
    current_run_id: str | None = None
    #: Team's session_id (latch-first).
    current_session_id: str | None = None

    # ── Completion tracking ─────────────────────────────────────────
    #: Backup capture of the final answer, same rationale as
    #: :attr:`SubAgentStreamState.completed_content`.
    completed_content: str = ""

    # ── Behaviour ──────────────────────────────────────────────────
    def latch_ids(self, event: object) -> bool:
        """Same latch-first-non-empty semantics as
        :meth:`SubAgentStreamState.latch_ids` — team state has no
        ``parent_top_run_id`` to worry about because the team IS the
        top-level run (its members' parent_run_id points back here).

        Returns ``True`` iff ``current_run_id`` was latched by this
        call.
        """
        newly_run = False
        if not self.current_run_id:
            ev_run_id = getattr(event, "run_id", None)
            if ev_run_id:
                self.current_run_id = ev_run_id
                newly_run = True
        if not self.current_session_id:
            ev_session_id = getattr(event, "session_id", None)
            if ev_session_id:
                self.current_session_id = ev_session_id
        return newly_run

    def record_completion(self, content: str | None, _metrics: object = None) -> None:
        if content:
            self.completed_content = str(content)

    def append_content_delta(self, agent_path: str, chunk: str) -> str | None:
        """Per-agent variant of
        :meth:`SubAgentStreamState.append_content_delta`. The throttle
        + dedup are keyed by ``agent_path`` so one chatty member
        can't starve the others."""
        if not chunk:
            return None
        buf = self.content_buf_by_agent.get(agent_path, "") + str(chunk)
        self.content_buf_by_agent[agent_path] = buf
        now = time.monotonic()
        if now - self.last_update_by_agent.get(agent_path, 0.0) <= 0.5:
            return None
        self.last_update_by_agent[agent_path] = now
        preview = PREVIEWS.format_content_buffer(buf)
        if not preview or preview == self.last_preview_by_agent.get(agent_path):
            return None
        self.last_preview_by_agent[agent_path] = preview
        return preview
