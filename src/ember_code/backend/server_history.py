"""Chat-history rebuild for session-resume.

The public entry point is :class:`ChatHistoryRebuilder` — walks an
Agno session's persisted runs, rebuilds the FE's turn list so
``--continue`` opens the session with the exact visible state it
had before the process died. The per-run message walking lives on
:class:`~ember_code.backend.server_history_walker.RunWalker` (kept
in its own module so this file stays a small orchestrator).

Replaces the trio of free functions that used to live here
(``get_chat_history``, ``_fill_plan_states``,
``_splice_visualizations``) — each of which took a
``BackendServer`` as its first arg and reached back into
``backend._session.*`` for state. The class version composes the
:class:`Session` directly at construction time.

Neither class is a Pydantic model — they hold behavior + a
non-DTO ``Session`` reference. Pydantic BaseModel semantics
(forward-ref rebuild requirements, validation on assignment) fit
data classes, not services.

Turn shapes produced (see :mod:`schemas_history` for the Pydantic
models — every turn is a member of the :data:`ChatTurn` tagged
union, dumped at the wire boundary in :class:`BackendServer` to
preserve the existing ``list[dict]`` RPC contract):

* :class:`UserTurn` / :class:`AssistantTurn`
* :class:`ThinkingTurn` — synthesized from either the assistant
  message's ``reasoning_content`` (sidecar reasoning from
  Anthropic-style providers) or from inline ``<think>`` tags in
  the content (MiniMax-style).
* :class:`ToolTurn` — rebuild the tool cards emitted live as
  separate ``tool_started`` + ``tool_completed`` events.
* :class:`PlanTurn` — synthesized in place of an ``exit_plan_mode``
  tool result so the PlanCard renders at the chronological
  position where the agent submitted the plan.
* :class:`StatsTurn` — per-run input/output token badge.
* :class:`VisualizationTurn` — spliced from the session event
  log's ``visualization_delta`` entries.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from ember_code.backend.schemas_history import (
    AgnoRunView,
    ChatTurn,
    PlanTurn,
    ToolTurn,
    VisualizationDeltaPayload,
    VisualizationTurn,
)
from ember_code.backend.server_history_walker import RunWalker

if TYPE_CHECKING:
    from ember_code.core.session.core import Session


logger = logging.getLogger(__name__)

# Tool names that spawn a sub-agent / team — the splicer pairs the
# Nth logged visualization in a run with the Nth of these turns.
_SPAWN_TOOLS: frozenset[str] = frozenset({"spawn_agent", "spawn_team"})


class ChatHistoryRebuilder:
    """Rebuild the FE's turn list from a persisted Agno session.

    Constructor takes the :class:`Session` directly — no
    ``BackendServer`` reach-in. The three responsibilities of the
    old free-function trio are now methods here:

    * :meth:`rebuild` — walk each run's messages via a fresh
      :class:`RunWalker`, then run the two post-walk passes.
    * :meth:`_fill_plan_states` — assign ``pending`` / ``approved``
      / ``dismissed`` to each :class:`PlanTurn` from the persisted
      plan-decisions map on ``session.plan_store``.
    * :meth:`_splice_visualizations` — inject the visualizer cards
      from ``session.event_log`` at the correct chronological
      positions (Nth ``spawn_*`` tool turn in a run → Nth logged
      viz for that run).
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    async def rebuild(self, session_id: str) -> list[ChatTurn]:
        """Return the turn list for ``session_id``.

        Skips sub-agent runs (``parent_run_id`` set) — those are
        already visible via the parent's spawn tool turn. Empty
        list when Agno has no session by that id (fresh start,
        cleared history, wrong user).
        """
        agent = self.session.main_team
        agno_session = await agent.aget_session(
            session_id=session_id,
            user_id=self.session.user_id,
        )
        if agno_session is None:
            return []
        runs_raw = getattr(agno_session, "runs", None) or []
        out: list[ChatTurn] = []
        # Running char count across the FULL prompt the model sees
        # on each turn: system prompt + tool defs + conversation
        # history + this turn's user message. chars/4 is the same
        # coarse estimator the FE uses for ``visibleOutTokens``.
        history_chars = 0
        system_chars = 0
        for run_raw in runs_raw:
            try:
                run = AgnoRunView.model_validate(run_raw, from_attributes=True)
            except Exception as exc:
                logger.debug("Skipping malformed Agno run: %s", exc)
                continue
            if run.parent_run_id:
                continue
            walker = RunWalker(
                run_id=run.run_id,
                history_chars=history_chars,
                system_chars=system_chars,
            )
            out.extend(walker.walk(run.messages))
            stats = walker.finalize(run.metrics)
            if stats is not None:
                out.append(stats)
            history_chars = walker.history_chars
            system_chars = walker.system_chars

        self._fill_plan_states(out)
        self._splice_visualizations(out)
        return out

    def _fill_plan_states(self, out: list[ChatTurn]) -> None:
        """Assign ``state`` to each plan turn from
        ``session.plan_store.decisions``.

        Plan-turn state comes from the persisted decisions map
        (run_id → ``"approved"`` / ``"dismissed"``) that the FE
        writes via the ``approve_plan`` / ``dismiss_plan`` RPCs.
        Never inferred from the current permission mode — a mode
        flip without an explicit user click leaves the plan
        pending (the truth: the user never decided).

        Assignment:
          - explicit decision in the map  → use it
          - no decision AND latest plan   → "pending"
          - no decision AND historical    → "dismissed"
        """
        decisions = self.session.plan_store.decisions
        plan_indices = [i for i, t in enumerate(out) if isinstance(t, PlanTurn)]
        if not plan_indices:
            return
        latest_idx = plan_indices[-1]
        for i in plan_indices:
            turn = out[i]
            if not isinstance(turn, PlanTurn):
                continue
            recorded = decisions.get(turn.run_id) if turn.run_id else None
            if recorded in ("approved", "dismissed"):
                turn.state = recorded
            elif i == latest_idx:
                turn.state = "pending"
            else:
                turn.state = "dismissed"

    def _splice_visualizations(self, out: list[ChatTurn]) -> None:
        """Splice visualizer cards from
        ``session.event_log`` at the correct chronological
        positions.

        Within each run, the Nth logged viz pairs with the Nth
        ``spawn_agent`` / ``spawn_team`` tool turn (Agno
        concatenates the assistant's interleaved text blocks per
        run, so per-block splicing is impossible; tool-turn
        pairing is the closest-to-live ordering). Fallback
        anchors: last turn of the matching run, then
        append-at-tail.
        """
        event_log = self.session.event_log
        viz_events = [e for e in event_log if e.type == "visualization_delta"]
        if not viz_events:
            return

        spawn_indices_by_run: dict[str, list[int]] = {}
        last_by_run: dict[str, int] = {}
        for i, t in enumerate(out):
            rid = t.run_id
            if rid:
                last_by_run[rid] = i
            if isinstance(t, ToolTurn) and t.tool_name in _SPAWN_TOOLS and rid:
                spawn_indices_by_run.setdefault(rid, []).append(i)

        # Group by run_id preserving the log's seq order so the
        # Nth viz in a run maps to the Nth spawn call.
        by_run: dict[str, list] = {}
        for ev in viz_events:
            by_run.setdefault(ev.run_id, []).append(ev)
        for group in by_run.values():
            group.sort(key=lambda e: e.seq)

        insertions: dict[int, list[VisualizationTurn]] = {}
        for rid, group in by_run.items():
            spawn_idxs = spawn_indices_by_run.get(rid, [])
            for n, ev in enumerate(group):
                payload = VisualizationDeltaPayload.model_validate(ev.payload or {})
                if not payload.spec_json:
                    continue
                try:
                    spec = json.loads(payload.spec_json)
                except json.JSONDecodeError:
                    continue
                if not isinstance(spec, dict):
                    continue
                turn_out = VisualizationTurn(
                    spec_id=payload.spec_id,
                    spec=spec,
                    source_agent="visualizer",
                    run_id=rid,
                    seq=ev.seq,
                )
                if n < len(spawn_idxs):
                    target = spawn_idxs[n]
                elif rid in last_by_run:
                    target = last_by_run[rid]
                else:
                    target = -1
                insertions.setdefault(target, []).append(turn_out)

        # Splice from highest index down so earlier positions
        # remain valid as we mutate the list.
        for target in sorted(insertions.keys(), reverse=True):
            group = sorted(insertions[target], key=lambda t: t.seq)
            if target < 0:
                out.extend(group)
            else:
                out[target + 1 : target + 1] = group


__all__ = ["ChatHistoryRebuilder"]
