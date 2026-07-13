"""The main run engine — user-message dispatch + streaming.

Extracted from :mod:`ember_code.backend.server`. Three free
functions taking ``BackendServer`` as arg:

* :func:`run_message` — the streaming entry point. Serialises
  concurrent submits via ``backend._run_lock`` and tracks the
  running task on ``backend._current_run_task`` so
  ``cancel_run`` can ``task.cancel()`` it.
* :func:`run_message_locked` — the actual body. Owns the whole
  pre-run pipeline (mentions, media, learnings, interrupted-
  summary, hook fire, pending-message pre-persist) and the
  post-run tail (pending mark-completed, checkpoint task
  cancel, http-client close, Stop hook).
* :func:`close_model_http_client` — force-close the model's
  httpx client after a run so the API TCP connection is torn
  down promptly. A fresh client is assigned so the model
  stays usable for subsequent runs.

Rule 2 clean — all inline imports hoisted to module top.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, AsyncIterator

import httpx

from ember_code.core.hooks.events import HookEvent
from ember_code.core.utils.media import (
    attach_resolved_files,
    extract_media_urls,
    resolve_file_references,
)
from ember_code.core.utils.mentions import process_file_mentions
from ember_code.protocol import messages as msg

if TYPE_CHECKING:
    from ember_code.backend.server import BackendServer

logger = logging.getLogger(__name__)


# Fresh httpx client params post-``close_model_http_client``.
# Modest keepalive footprint so a run's post-tail teardown
# doesn't hold a big connection pool open across sessions.
_HTTP_CLIENT_LIMITS = httpx.Limits(
    max_connections=10,
    max_keepalive_connections=5,
    keepalive_expiry=30,
)


async def run_message(
    backend: "BackendServer",
    text: str,
    media: dict[str, Any] | None = None,
) -> AsyncIterator[msg.Message]:
    """Execute a user message and yield protocol messages.

    This is the main streaming entry point. The FE iterates over
    the yielded messages and renders them.

    Serialised by ``backend._run_lock``: when the FE submits a
    new message before the previous run's Agno tail has
    finished (compression, memory extraction, final
    persistence), the new call waits silently on the lock. The
    FE has already cleared its "processing" state on
    ``StreamingDone`` so the user can type, but two concurrent
    ``team.arun()`` calls on the same Agno team would race on
    session/memory state. The lock makes the second turn appear
    as a normal beat of "Thinking" from the user's POV; the
    wait is invisible apart from that.
    """
    async with backend._run_lock:
        # Track the task so cancel_run can ``task.cancel()`` it.
        # ``current_task()`` returns the task running this async
        # generator (the one consuming ``run_message_locked``).
        backend._current_run_task = asyncio.current_task()
        try:
            # Route through the instance method so per-class
            # patches (used by ``test_streaming_done_unblock``
            # etc.) still intercept — the free function is the
            # canonical body but callers may swap the method for
            # a spy.
            async for proto in backend._run_message_locked(text, media):
                yield proto
        except asyncio.CancelledError:
            # User-initiated cancel — emit a soft notice and
            # return gracefully so the FE clears its "Thinking"
            # state.
            yield msg.Info(text="Run cancelled by user.")
        finally:
            backend._current_run_task = None


async def run_message_locked(
    backend: "BackendServer",
    text: str,
    media: dict[str, Any] | None,
) -> AsyncIterator[msg.Message]:
    """Body of ``run_message`` — runs only when the serial lock is held."""
    backend._processing = True
    team = backend._session.main_team

    # Process @file mentions.
    text, mentioned_files = process_file_mentions(text)
    if mentioned_files:
        yield msg.Info(text=f"Referenced: {', '.join(mentioned_files)}")

    # Resolve bare filenames and attach media for vision-capable
    # models.
    model_name = backend._session.settings.models.default
    model_cfg = backend._session.settings.models.registry.get(model_name, {})
    is_vision = model_cfg.get("vision", False)

    text, resolved_files = resolve_file_references(text, project_dir=backend._session.project_dir)
    if resolved_files:
        if is_vision:
            parsed_media = attach_resolved_files(resolved_files)
            if parsed_media:
                media = parsed_media
                yield msg.Info(text=f"Attached: {len(resolved_files)} file(s)")
            else:
                yield msg.Info(text=f"Resolved: {', '.join(resolved_files)}")
        else:
            yield msg.Info(text=f"Resolved: {', '.join(resolved_files)}")

    # Attach media URLs (images, etc.) for vision models.
    if is_vision:
        url_media = extract_media_urls(text)
        if url_media:
            if media:
                for k, v in url_media.items():
                    media.setdefault(k, []).extend(v)
            else:
                media = url_media
            count = sum(len(v) for v in url_media.values())
            yield msg.Info(text=f"Attached {count} URL(s)")

    # Inject learnings.
    await backend._session._inject_learnings()

    # If the previous process died mid-chain, surface the
    # incomplete-run summary built during ``startup`` so the
    # agent knows it was interrupted. One-shot per launch — the
    # next iteration of this turn will see ``None``. Pending
    # rows surfaced by ``get_pending_messages`` to the FE on
    # resume are discarded here too, after the agent has been
    # nudged about them — that way a SECOND restart before the
    # user actually responds doesn't surface them again.
    interrupted_summary = backend._interrupted_run_summary
    backend._interrupted_run_summary = None
    for pending_id_to_drop in backend._pending_message_ids_to_drop:
        await backend._pending_store.adiscard(pending_id_to_drop)
    backend._pending_message_ids_to_drop = []

    # Add timestamp (and the interrupted-run note, if any).
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    ctx_parts = [f"Current datetime: {timestamp}"]
    if interrupted_summary:
        ctx_parts.append(interrupted_summary)
        yield msg.Info(text="(continuing from an interrupted previous run)")
    message = f"<system-context>{' '.join(ctx_parts)}</system-context>\n{text}"

    # Fire UserPromptSubmit hook.
    hook_result = await backend._session.hook_executor.execute(
        event=HookEvent.USER_PROMPT_SUBMIT.value,
        payload={"message": text, "session_id": backend._session.session_id},
    )
    if not hook_result.should_continue:
        yield msg.Error(text=hook_result.message or "Message blocked by hook.")
        backend._processing = False
        return
    if hook_result.message:
        # Queue hook context for injection.
        message = f"{message}\n<hook-context>{hook_result.message}</hook-context>"

    # Stream events from Agno. We multiplex the team's stream with
    # the sub-agent HITL coordinator — see
    # ``_stream_with_subagent_hitl`` for the full rationale. The
    # same multiplexer is also used by ``resolve_hitl`` so a
    # parent that pauses (top-level Bash) and then spawns a
    # sub-agent on resume still gets the sub-agent's pauses
    # surfaced — an earlier version only multiplexed inside
    # ``run_message`` and the sub-agent's pauses silently sat in
    # the coordinator forever.
    # Pre-persist the user message so a kill mid-stream doesn't
    # lose it (Agno doesn't write to disk until end-of-run). The
    # id is opaque; we use it on the success path to mark the
    # row completed. On a crash the row stays ``pending`` and
    # ``_detect_interrupted_run`` surfaces it on the next
    # ``--continue`` boot.
    pending_id = await backend._pending_store.arecord_received(
        backend._session.session_id, text
    )

    # Periodic checkpoint task — fires ``asave_session`` every
    # few seconds during the run so streaming responses (which
    # otherwise see zero disk writes between RunStarted and
    # RunCompleted) survive a crash within ~3 seconds of
    # whatever Agno had assembled by then. Cancelled in the
    # finally below regardless of how the run exits.
    checkpoint_task = asyncio.create_task(backend._periodic_checkpoint(team))

    media_kwargs = media or {}
    try:
        async for proto in backend._stream_with_subagent_hitl(
            team.arun(message, stream=True, **media_kwargs)
        ):
            # Latch the top-level run's input-token count as
            # the current context size. ``input_tokens`` is the
            # prompt Agno sent to the model — which IS the live
            # context. Computing it lazily from ``aget_session``
            # (the old path) hung after a run while Agno's
            # post-stream tail held session state; this is
            # O(1) and never blocks.
            if (
                isinstance(proto, msg.RunCompleted)
                and not proto.parent_run_id
                and proto.input_tokens
            ):
                backend._session._last_input_tokens = proto.input_tokens
            yield proto
            # Checkpoint after each tool completion. Agno's
            # default persistence model is end-of-run only — if
            # the process crashes mid-chain, the in-flight
            # ``RunOutput`` is lost and ``--continue`` can't
            # surface the partial work to the agent. Forcing
            # ``asave_session`` after every tool-completed
            # write means a crash leaves a session with
            # ``status=running`` containing every tool call
            # that finished. On a successful run the natural
            # end-of-run save overwrites the last partial
            # snapshot via Agno's upsert semantics, so no
            # separate "drop partial" cleanup is needed. The
            # cost is one ~1-5ms SQLite upsert per tool call —
            # negligible compared to the model latency, and
            # only fires on actual tool completion events (not
            # every ContentDelta).
            if isinstance(proto, (msg.ToolCompleted, msg.ToolError)):
                await backend._checkpoint_session(team)
        # Run reached natural end → mark the pre-persisted user
        # message as completed so it doesn't get surfaced as
        # "interrupted" on the next boot.
        await backend._pending_store.amark_completed(pending_id)
    finally:
        backend._processing = False
        checkpoint_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await checkpoint_task
        await close_model_http_client(team)

    # Fire Stop hook.
    stop_result = await backend._session.hook_executor.execute(
        event=HookEvent.STOP.value,
        payload={"session_id": backend._session.session_id},
    )
    if stop_result.message and not stop_result.should_continue:
        yield msg.Info(text=stop_result.message)


async def close_model_http_client(team: Any) -> None:
    """Close the httpx client on the model to release open HTTP streams.

    When an Agno run finishes or is cancelled mid-stream, the
    underlying httpx connection to the API may stay open
    indefinitely. Closing the client ensures the TCP connection
    is torn down promptly so the server can release concurrency
    slots. A fresh client is assigned so the model remains
    usable for subsequent runs.
    """
    model = None
    try:
        model = getattr(team, "model", None)
        client = getattr(model, "http_client", None) if model else None
        if isinstance(client, httpx.AsyncClient):
            await asyncio.wait_for(client.aclose(), timeout=3)
    except Exception as exc:
        logger.debug("Failed to close model HTTP client: %s", exc)

    # Always ensure a fresh client, even if close failed. The
    # old client's connections will eventually timeout.
    if model is not None:
        model.http_client = httpx.AsyncClient(limits=_HTTP_CLIENT_LIMITS)
