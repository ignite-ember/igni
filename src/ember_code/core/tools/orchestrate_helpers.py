"""Pure helpers for :mod:`ember_code.core.tools.orchestrate`.

Extracted so the god-file's two 400-line streaming generators
have less noise around them. Every helper here is a stateless
utility — no mutable state, no session references, no coupling
to the streaming generators' nonlocal state.

Contents:

* :func:`_finalize_worktree` — restore rebound ``base_dir`` on
  tools + clean up a per-spawn worktree; produces the footer
  the parent agent sees after an isolated spawn.
* :func:`_format_args` / :func:`_preview` / :func:`_build_preview`
  — pretty-print tool args, one-line result previews, and the
  multi-line rolling preview shown under each agent header
  during a streaming run.
* :class:`VisualizationDeltaEvent` — Pydantic wire model for a
  ``visualization_delta`` push event.
* :func:`_extract_spec_from_partial_args` — tolerant partial-
  JSON parse of the visualizer sub-agent's streaming tool-call
  arguments.

``PREVIEW_WINDOW`` / ``PREVIEW_LINE_MAX`` live here too — the FE
mirrors them in ``clients/web/src/chat/model.ts``.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any

import jiter
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from ember_code.core.worktree import WorktreeInfo

logger = logging.getLogger(__name__)

# How many non-empty lines of streamed agent content to keep in the
# rolling "thinking" preview shown under each agent header. Matches
# the FE constant ``PREVIEW_WINDOW`` in
# ``clients/web/src/chat/model.ts`` — the BE is the source of truth
# for the window, the FE just renders it.
PREVIEW_WINDOW = 5
PREVIEW_LINE_MAX = 120


def _finalize_worktree(
    manager: Any,
    info: "WorktreeInfo | None",
    original_base_dirs: dict[Any, Any],
) -> str:
    """Restore tool ``base_dir`` rebinds and clean up the
    worktree. Returns a footer string for the spawn response so
    the parent agent knows whether the worktree was reaped or
    preserved.

    Idempotent and exception-safe — designed to run inside
    ``finally``-ish paths after every spawn, isolated or not.
    Returns ``""`` when there was no worktree (the normal case)
    so callers can append unconditionally.
    """
    # Restore tool base_dirs first — the worktree dir may disappear
    # in the cleanup step below, and a stray reference to it after
    # that would point at a missing path.
    for tool, original in original_base_dirs.items():
        with contextlib.suppress(Exception):
            tool.base_dir = original
    if manager is None or info is None:
        return ""
    try:
        reaped = manager.cleanup()
    except Exception as exc:
        logger.warning("worktree cleanup failed: %s", exc)
        return (
            f"\n\nWorktree: {info.worktree_path} (branch: "
            f"{info.branch_name}) — cleanup failed: {exc}"
        )
    if reaped:
        return f"\n\nWorktree {info.branch_name} (clean) — reaped."
    return (
        f"\n\nWorktree preserved: {info.worktree_path} "
        f"(branch: {info.branch_name}) — has uncommitted changes.\n"
        f"To merge: git merge {info.branch_name}\n"
        f"To remove: git worktree remove {info.worktree_path}"
    )


def _format_args(tool_args: dict | None) -> str:
    """One-line preview of the first two kwargs in a tool call."""
    if not tool_args:
        return ""
    parts = []
    for k, v in list(tool_args.items())[:2]:
        val = str(v).replace("\n", " ")
        if len(val) > 30:
            val = val[:27] + "..."
        parts.append(f"{k}={val}")
    return ", ".join(parts)


def _preview(result: Any, limit: int = 60) -> str:
    """One-line preview of a tool result, capped at ``limit`` chars."""
    if result is None:
        return ""
    s = str(result).replace("\n", " ").strip()
    return s[:limit] + "..." if len(s) > limit else s


def _build_preview(buf: str) -> str:
    """Turn an agent's accumulated streaming text into the multi-line
    preview payload — the last PREVIEW_WINDOW non-empty lines, each
    truncated to PREVIEW_LINE_MAX chars, joined by ``\\n``.

    Returning a multi-line ``text`` is the protocol: the FE splits
    on ``\\n`` and *replaces* its preview window. That keeps the BE
    as the source of truth — Agno deltas are token-sized, so the FE
    used to fill its window with token-per-line garbage when it
    appended each delta as its own preview entry.
    """
    if not buf:
        return ""
    cleaned = buf.replace("<think>", "").replace("</think>", "")
    lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
    if not lines:
        return ""
    tail = lines[-PREVIEW_WINDOW:]
    truncated = [
        (ln[: PREVIEW_LINE_MAX - 1] + "…") if len(ln) > PREVIEW_LINE_MAX else ln for ln in tail
    ]
    return "\n".join(truncated)


class VisualizationDeltaEvent(BaseModel):
    """One ``visualization_delta`` wire event. Sent to the FE either
    as a progressive delta (partial JSON as the visualizer sub-agent
    streams its tool_call arguments) or a final one carrying the
    fully-parsed spec (``final=True``) once ``ToolCallStartedEvent``
    fires.

    The FE partial-parses ``spec_json`` on every delta and swaps in
    the completed spec when ``final`` is true. ``spec_id`` dedupes
    so multiple deltas for the same card update in place instead of
    stacking. The wire uses key name ``json`` (kept via alias) to
    match the existing FE contract without renaming the field there.
    """

    model_config = {"populate_by_name": True}

    type: str = Field(default="visualization_delta", frozen=True)
    agent_path: str
    spec_id: str
    spec_json: str = Field(alias="json")
    final: bool = False


def _extract_spec_from_partial_args(args_partial: str) -> str | None:
    """Given a partial JSON string being streamed as a tool call's
    arguments (e.g. ``'{"spec": {"root": "r", "elem'``), extract the
    ``spec`` sub-object as a JSON string.

    Uses ``jiter.from_json`` with ``partial_mode='trailing-strings'``:
    tolerantly parses the incomplete outer object, salvaging as much
    nested structure as landed. Returns ``None`` when ``spec`` isn't
    a dict yet (first few tokens before the object opens).
    """
    if not args_partial:
        return None
    try:
        parsed = jiter.from_json(args_partial.encode(), partial_mode="trailing-strings")
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    spec = parsed.get("spec")
    if not isinstance(spec, dict):
        return None
    return json.dumps(spec)


def _format_team_result(
    *,
    names: list[str],
    mode: str,
    member_lines: list[str],
    task: str,
    elapsed: float,
    result: str,
    activity: list[str],
) -> str:
    """Format the multi-line response string returned by
    ``OrchestrateTools.spawn_team`` to the parent agent.

    Same shape as :func:`_format_spawn_result` but with a team
    header (agents + mode + per-member description block)
    instead of a single-agent header. Teams don't get a
    worktree footer today — isolation is a per-agent concern.
    """
    activity_log = "\n".join(activity) if activity else "  (no activity)"
    return (
        f"[Team: {', '.join(names)}] (mode: {mode})\n"
        f"[Members:\n" + "\n".join(member_lines) + "]\n"
        f"[Task: {task}]\n"
        f"[Time: {elapsed:.1f}s]\n\n"
        f"Activity:\n{activity_log}\n\n"
        f"Response:\n{result}"
    )


def _format_spawn_result(
    *,
    agent_name: str,
    agent_desc: str,
    agent_tools: str,
    task: str,
    elapsed: float,
    result: str,
    activity: list[str],
    worktree_footer: str,
) -> str:
    """Format the multi-line response string returned by
    ``OrchestrateTools.spawn_agent`` to the parent agent.

    Includes:
    * A four-line header (agent name + desc, tools, task,
      elapsed time).
    * The per-tool-call activity log (or ``"(no tool calls)"``
      when the sub-agent produced no tool events).
    * The sub-agent's final response text.
    * An explicit warning banner when the sub-agent hit a
      run-level error mid-stream (e.g. model API failure) —
      surfaces the "response is partial" fact so the parent
      doesn't guess "looks cut off" and can react (retry,
      switch tactic, etc.).
    * The worktree footer (branch + path) when isolation was
      requested; empty otherwise.
    """
    activity_log = "\n".join(activity) if activity else "  (no tool calls)"
    run_errors = [line for line in activity if "RUN ERROR" in line]
    error_section = ""
    if run_errors:
        error_section = (
            "\n\nWARNING: This sub-agent terminated with a run error — "
            "the response below is partial. Consider retrying, or proceed "
            "with the partial result if it's sufficient.\n" + "\n".join(run_errors)
        )
    return (
        f"[Agent: {agent_name}] {agent_desc}\n"
        f"[Tools: {agent_tools}]\n"
        f"[Task: {task}]\n"
        f"[Time: {elapsed:.1f}s]\n\n"
        f"Activity:\n{activity_log}\n\n"
        f"Response:\n{result}"
        f"{error_section}"
        f"{worktree_footer}"
    )
