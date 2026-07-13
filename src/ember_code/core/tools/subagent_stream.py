"""Sub-agent stream state — Pydantic model that replaces the 11
nonlocals in :func:`_run_agent_streaming`.

Why this exists: before this file, ``_run_agent_streaming``'s nested
``_handle`` closure declared 11 nonlocals to track content buffers,
run-id capture, visualizer streaming throttle state, and completion
markers. Every new feature added another nonlocal. Adding a nonlocal
is a two-line change (`nonlocal foo; ... foo = ...`) with no schema
enforcement — the ceiling for that pattern is a bug per addition
because typos become new locals silently.

The refactor is model-first, per CODE_STANDARDS.md Pattern 4
(composition over god-classes) + AP2 (>5 nonlocals in a function is
a smell). One Pydantic model holds every piece of per-stream state.
The handler mutates it in place, then passes it forward. Adding a
new field is a one-line change on the model + explicit usage — no
silent typos, autocomplete works, and every field is discoverable
via ``model_fields``.

Behaviour-preserving: field-for-field replacement of the pre-existing
nonlocals. No new behaviour; the file exists purely to give the
state a schema.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field


class SubAgentStreamState(BaseModel):
    """Per-invocation state for :func:`_run_agent_streaming`.

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


class TeamStreamState(BaseModel):
    """Per-invocation state for :func:`run_team_streaming`.

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

    Field-for-field replacement of the pre-existing 9 nonlocals
    (`current_tool`, `current_agent`, `last_update_by_agent`,
    `last_preview_by_agent`, `content_buf_by_agent`,
    `current_run_id`, `current_session_id`, `completed_content`,
    `team_path_id`). Behaviour-preserving.
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
