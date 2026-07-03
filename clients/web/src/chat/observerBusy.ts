/**
 * Observer-side "is a run in progress" state machine.
 *
 * ``App.tsx`` binds two boolean flags to the composer's UX:
 *
 *  - ``proc``       — a run is actively producing output. Disables
 *                      send, shows the spinner, greys the mic.
 *  - ``finalizing`` — the visible content has ended but the BE tail
 *                      (memory extract, compression, persistence) is
 *                      still running. Composer is re-enabled, but a
 *                      subtle "Finalizing…" indicator shows so the
 *                      user knows things aren't fully idle.
 *
 * The **initiator** path (the view that submitted the message) drives
 * these flags from ``onStreamEvent``, where the stream-demux layer
 * intercepts ``stream_end`` before it reaches this state machine.
 *
 * The **observer** path (any other view attached to the same BE)
 * sees the same events dispatched via the general event listener,
 * *including* ``stream_end`` — because it doesn't own the request
 * stream those events belong to.
 *
 * The tricky one: ``stream_end`` arrives AFTER ``run_completed``. If
 * we treated it like ``streaming_done`` (which used to be the case),
 * the sequence
 *   streaming_done  → finalizing=true
 *   run_completed   → finalizing=false
 *   stream_end      → finalizing=true   ← bug
 * left the observer's UI pinned at "Finalizing…" while the
 * initiator's chat was long since idle. This module makes the rules
 * explicit and testable so that regression can't sneak back in.
 */

import type { ServerMessage } from "../protocol/messages";

export interface ObserverBusyState {
  proc: boolean;
  finalizing: boolean;
}

export const IDLE: ObserverBusyState = { proc: false, finalizing: false };

/**
 * Compute the next ``ObserverBusyState`` given the previous state
 * and a streamed protocol message. Pure — no side effects, no React,
 * so it's cheap to test with a scripted event tape.
 *
 * Events not listed here (content deltas, tool cards, reasoning,
 * push notifications, …) never change busy state; the caller passes
 * ``prev`` through unchanged.
 */
export function nextObserverBusyState(
  prev: ObserverBusyState,
  msg: ServerMessage,
): ObserverBusyState {
  switch (msg.type) {
    case "run_started":
      // Fresh run beginning — reset both flags. A new run always
      // wipes any leftover ``finalizing`` from the previous run's
      // tail; without this a fast follow-up would carry the stale
      // indicator into the new turn.
      return { proc: true, finalizing: false };

    case "streaming_done":
      // Content has ended; tail still running.
      return { proc: false, finalizing: true };

    case "run_completed":
      // Tail done — release the indicator. ``proc`` was already
      // cleared by ``streaming_done``; preserve it just in case a
      // run_completed slips through without a prior streaming_done
      // (shouldn't happen, but the wire doesn't guarantee it).
      return { ...prev, finalizing: false };

    case "stream_end":
      // The BE's per-request terminator, fired AFTER
      // ``run_completed``. For a cross-view observer this reaches
      // the app-level handler (the initiator's stream demuxer
      // intercepts it in ``protocol/client.ts``). Treat it as a
      // hard reset — never re-arm ``finalizing`` here, and go
      // fully idle in case the observer missed an earlier event.
      return IDLE;

    default:
      return prev;
  }
}
