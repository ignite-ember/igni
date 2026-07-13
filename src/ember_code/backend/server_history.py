"""Chat-history rebuild for session-resume.

Extracted from :mod:`ember_code.backend.server`. One free
function — :func:`get_chat_history` — walks an Agno session's
persisted runs and rebuilds the FE's turn list so ``--continue``
opens a session with the exact same visible state it had before
the process died.

Turn shapes produced:

* ``{role: "user", ...}`` / ``{role: "assistant", ...}``
* ``{role: "thinking", ...}`` — synthesized from either the
  assistant message's ``reasoning_content`` (sidecar reasoning
  from Anthropic-style providers) or from inline ``<think>``
  tags in the content (MiniMax-style).
* ``{role: "tool", ...}`` — rebuild the tool cards emitted live
  as separate ``tool_started`` + ``tool_completed`` events.
* ``{role: "plan", ...}`` — synthesized in place of an
  ``exit_plan_mode`` tool result so the PlanCard renders at the
  chronological position where the agent submitted the plan.
* ``{role: "stats", ...}`` — per-run input/output token badge.
* ``{role: "visualization", ...}`` — spliced from the session
  event log's ``visualization_delta`` entries.

Rule 2 clean — no inline imports.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ember_code.backend.server_helpers import (
    _format_tool_args_for_restore,
    _split_assistant_content_for_restore,
)
from ember_code.protocol.agno_events import TOOL_NAMES

if TYPE_CHECKING:
    from ember_code.backend.server import BackendServer


_SPAWN_TOOLS: frozenset[str] = frozenset({"spawn_agent", "spawn_team"})


async def get_chat_history(backend: "BackendServer", session_id: str) -> list[dict]:
    """Rebuild the FE's turn list for ``session_id``.

    See module docstring for turn shapes. ``run_id`` on every
    turn lets the FE map a user message back to its owning run
    for edit/delete truncation. Skips sub-agent runs
    (``parent_run_id`` set) and ``system`` messages.
    """
    agent = backend._session.main_team
    agno_session = await agent.aget_session(
        session_id=session_id,
        user_id=backend._session.user_id,
    )
    if agno_session is None:
        return []
    runs = getattr(agno_session, "runs", None) or []
    out: list[dict] = []
    # Running char count across the FULL prompt the model sees on
    # each turn: system prompt + tool defs + conversation history
    # (user / assistant / tool results) + this turn's user
    # message. chars/4 is the same coarse estimator the FE uses
    # for ``visibleOutTokens``. Per-turn input is monotonic — it
    # grows as the chat grows, matching the user's intuition.
    history_chars = 0  # accumulated user/assistant/tool content so far
    system_chars = 0  # the constant system + tool-defs overhead, captured once
    for run in runs:
        if getattr(run, "parent_run_id", None):
            continue
        run_id = str(getattr(run, "run_id", "") or "")
        messages = getattr(run, "messages", None) or []
        # Snapshot BEFORE walking this run's messages — that's the
        # context the model saw on its way into this turn (not yet
        # including this turn's user message).
        input_chars = history_chars
        assistant_chars = 0
        # Track exit_plan_mode tool calls within this run so we
        # can render a PlanCard in place of the regular tool turn
        # when the tool result lands. ``tool_call_id`` (set on
        # both the assistant's ``tool_calls`` entry and the
        # subsequent tool message) is the correlation key.
        plan_calls_in_run: dict[str, dict] = {}
        for m in messages:
            if getattr(m, "from_history", False):
                continue
            role = getattr(m, "role", "")
            content = m.content if isinstance(m.content, str) else str(m.content or "")
            created_at = int(getattr(m, "created_at", 0) or 0)
            # System messages are the system-prompt + tool-defs
            # overhead the model receives on every API call.
            # Same content on every run — capture once and add as
            # a constant to every input estimate.
            if role == "system":
                if not system_chars:
                    system_chars = len(content)
                continue
            # Tool result messages — rebuild the live tool card,
            # UNLESS this is the result of an exit_plan_mode call:
            # then emit a PlanCard turn instead so the card
            # appears at the point in the chat where the agent
            # actually submitted the plan, not bolted onto the end.
            if role == "tool":
                tool_name = str(getattr(m, "tool_name", "") or "")
                tool_call_id = str(getattr(m, "tool_call_id", "") or "")
                if tool_call_id and tool_call_id in plan_calls_in_run:
                    plan_args = plan_calls_in_run.pop(tool_call_id)
                    plan_text = str(plan_args.get("plan", "")).strip()
                    if plan_text:
                        out.append(
                            {
                                "role": "plan",
                                "plan": plan_text,
                                "tasks": plan_args.get("tasks") or [],
                                # State is filled in post-walk from
                                # the persisted ``plan_decisions``
                                # map keyed by ``run_id``. Leave
                                # empty here so the post-walk pass
                                # can distinguish "we set it" from
                                # "it was never touched".
                                "state": "",
                                "run_id": run_id,
                                "created_at": created_at,
                            }
                        )
                        history_chars += len(content)
                        continue
                tool_args_raw = getattr(m, "tool_args", None)
                if isinstance(tool_args_raw, (dict, list)):
                    args_summary = _format_tool_args_for_restore(tool_args_raw)
                elif tool_args_raw is None:
                    args_summary = ""
                else:
                    args_summary = str(tool_args_raw)
                out.append(
                    {
                        "role": "tool",
                        "tool_name": tool_name,
                        "friendly_name": TOOL_NAMES.get(tool_name, tool_name),
                        "args": args_summary,
                        "content": content,
                        "is_error": bool(getattr(m, "tool_call_error", False)),
                        "run_id": run_id,
                        "created_at": created_at,
                    }
                )
                history_chars += len(content)
                continue
            # Assistant message: handle two thinking sources and
            # interleave with the visible reply so the restored
            # chat reads in the same order the live stream
            # produced (thinking → reply → maybe more thinking).
            #
            # Source 1: Agno's ``reasoning_content`` field — set
            # by providers that expose reasoning as a sidecar
            # stream (Anthropic-style). One thinking block,
            # logically BEFORE the visible reply.
            #
            # Source 2: inline ``<think>...</think>`` tags inside
            # the content itself (MiniMax-style). Split the
            # content and interleave assistant + thinking turns
            # in occurrence order.
            #
            # Also stash any ``exit_plan_mode`` tool calls keyed
            # by call_id so the later tool result can be rewritten
            # as a PlanCard turn.
            if role == "assistant":
                reasoning = getattr(m, "reasoning_content", None)
                if isinstance(reasoning, str) and reasoning.strip():
                    out.append(
                        {
                            "role": "thinking",
                            "content": reasoning,
                            "run_id": run_id,
                            "created_at": created_at,
                        }
                    )
                for tc in getattr(m, "tool_calls", None) or []:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") or {}
                    if fn.get("name") != "exit_plan_mode":
                        continue
                    args_raw = fn.get("arguments")
                    if isinstance(args_raw, str):
                        try:
                            parsed = json.loads(args_raw)
                        except Exception:
                            continue
                    elif isinstance(args_raw, dict):
                        parsed = args_raw
                    else:
                        continue
                    call_id = str(tc.get("id") or "")
                    if call_id:
                        plan_calls_in_run[call_id] = parsed
                # Source 2: split inline <think> tags out of the
                # content. Each segment becomes its own turn.
                for part_role, part_text in _split_assistant_content_for_restore(content):
                    out.append(
                        {
                            "role": part_role,
                            "content": part_text,
                            "run_id": run_id,
                            "created_at": created_at,
                        }
                    )
                    if part_role == "assistant":
                        assistant_chars += len(part_text)
                # Count the full original content toward history
                # — that's what the model actually saw on the
                # next turn (including the think tags).
                history_chars += len(content)
                continue
            # User turn — display AND count. Carry the
            # message's ``created_at`` (Agno-issued epoch seconds)
            # so the FE can stamp each turn with a real time.
            out.append(
                {
                    "role": role,
                    "content": content,
                    "run_id": run_id,
                    "created_at": created_at,
                }
            )
            history_chars += len(content)
            if role == "user":
                # The user message of this run lands in the model's
                # input but not in the pre-run snapshot.
                input_chars += len(content)
        metrics = getattr(run, "metrics", None)
        # Input / output are ALWAYS chars/4 estimates of the model's
        # actual prompt — NOT Agno's billed numbers. Reason: Agno's
        # ``run.metrics.input_tokens`` sums across model iterations
        # within a turn (agent reasoning loops, tool re-prompts),
        # so the same conversation reads as non-monotonic. The live
        # path corrects this via ``count_context_tokens`` after each
        # run, but historical runs have no corrected number to
        # restore. Estimate = system + history + this-turn user
        # message, all chars/4.
        full_input_chars = system_chars + input_chars
        input_tokens = max(1, full_input_chars // 4) if full_input_chars else 0
        output_tokens = max(1, assistant_chars // 4) if assistant_chars else 0
        # After estimation, an all-zero stats line means the run
        # had no visible content at all (degenerate / empty run);
        # nothing to display.
        if input_tokens or output_tokens:
            out.append(
                {
                    "role": "stats",
                    "run_id": run_id,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "reasoning_tokens": int(getattr(metrics, "reasoning_tokens", 0) or 0)
                    if metrics
                    else 0,
                    "duration": float(getattr(metrics, "duration", 0) or 0) if metrics else 0.0,
                }
            )

    _fill_plan_states(backend, out)
    _splice_visualizations(backend, out)
    return out


def _fill_plan_states(backend: "BackendServer", out: list[dict]) -> None:
    """Post-walk pass that assigns ``state`` to each plan turn.

    Plan-turn state comes from the persisted ``plan_decisions``
    map (run_id → "approved" | "dismissed") that the FE writes
    via the ``approve_plan`` / ``dismiss_plan`` RPCs. Never
    inferred from the current permission mode — a mode flip
    without an explicit user click leaves the plan pending
    (which is the truth: the user never decided).

    Assignment:
      - explicit decision in the map  → use it
      - no decision AND latest plan   → "pending"
        (user still has the chance to act)
      - no decision AND historical    → "dismissed"
        (the user moved on without clicking; card renders as
        dismissed so we don't show stale Approve buttons on
        prior plans)
    """
    store = getattr(backend._session, "plan_store", None)
    decisions = getattr(store, "decisions", {}) if store else {}
    plan_indices = [i for i, t in enumerate(out) if t.get("role") == "plan"]
    if not plan_indices:
        return
    latest_idx = plan_indices[-1]
    for i in plan_indices:
        turn = out[i]
        run_id = str(turn.get("run_id") or "")
        recorded = decisions.get(run_id) if run_id else None
        if recorded in ("approved", "dismissed"):
            turn["state"] = recorded
        elif i == latest_idx:
            turn["state"] = "pending"
        else:
            turn["state"] = "dismissed"


def _splice_visualizations(backend: "BackendServer", out: list[dict]) -> None:
    """Splice visualizer cards from the session event log into ``out``.

    Storage side: ``orchestrate.py`` appends one
    ``visualization_delta`` event (``final=True``, complete
    spec JSON in the payload) to the session event log every
    time the visualizer sub-agent finishes. No FE→BE save RPC
    involved — the BE owns the lifecycle.

    Positioning: within each run, the Nth logged viz pairs with
    the Nth ``spawn_agent`` / ``spawn_team`` tool turn (Agno
    concatenates the assistant's interleaved text blocks per
    run, so per-block splicing is impossible; tool-turn pairing
    is the closest-to-live ordering). Fallback anchors: last
    turn of the matching run, then append-at-tail.
    """
    event_log = getattr(backend._session, "event_log", None) or []
    # ``event_log`` holds :class:`SessionEvent` post-iter-60.
    # Filter by attribute access (Rule 1 — no dict-shaped events).
    viz_events = [e for e in event_log if e.type == "visualization_delta"]
    if not viz_events:
        return

    spawn_indices_by_run: dict[str, list[int]] = {}
    last_by_run: dict[str, int] = {}
    for i, t in enumerate(out):
        rid = str(t.get("run_id") or "")
        if rid:
            last_by_run[rid] = i
        if t.get("role") == "tool" and t.get("tool_name") in _SPAWN_TOOLS and rid:
            spawn_indices_by_run.setdefault(rid, []).append(i)

    # Group by run_id preserving the log's seq order so the Nth
    # viz in a run maps to the Nth spawn call.
    by_run: dict[str, list] = {}
    for ev in viz_events:
        rid = ev.run_id
        by_run.setdefault(rid, []).append(ev)
    for group in by_run.values():
        group.sort(key=lambda e: e.seq)

    insertions: dict[int, list[dict]] = {}
    for rid, group in by_run.items():
        spawn_idxs = spawn_indices_by_run.get(rid, [])
        for n, ev in enumerate(group):
            payload = ev.payload or {}
            json_str = str(payload.get("json") or "")
            if not json_str:
                continue
            try:
                spec = json.loads(json_str)
            except Exception:
                continue
            if not isinstance(spec, dict):
                continue
            turn_out = {
                "role": "visualization",
                "spec_id": str(payload.get("spec_id") or ""),
                "spec": spec,
                "source_agent": "visualizer",
                "run_id": rid,
                "seq": ev.seq,
            }
            if n < len(spawn_idxs):
                target = spawn_idxs[n]
            elif rid in last_by_run:
                target = last_by_run[rid]
            else:
                target = -1
            insertions.setdefault(target, []).append(turn_out)

    # Splice from highest index down so earlier positions remain
    # valid as we mutate the list.
    for target in sorted(insertions.keys(), reverse=True):
        group = sorted(insertions[target], key=lambda t: t["seq"])
        if target < 0:
            out.extend(group)
        else:
            out[target + 1 : target + 1] = group
