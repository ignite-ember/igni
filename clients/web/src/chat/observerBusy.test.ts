import { describe, it, expect } from "vitest";
import { IDLE, nextObserverBusyState } from "./observerBusy";
import type { ServerMessage } from "../protocol/messages";

/**
 * The observer-side finalizing indicator has one specific footgun:
 * the BE emits ``stream_end`` *after* ``run_completed`` as a
 * per-request terminator. If ``stream_end`` re-arms ``finalizing``
 * (as it used to, when both events shared the same branch in the
 * cross-view handler), a second client stays pinned at
 * "Finalizing…" forever while the originating client sees the run
 * as fully done. These tests lock down the state machine so that
 * regression can't come back.
 */

// Minimal helper — we don't care about payload fields for these
// state transitions, only ``type``. Cast through ``unknown`` so the
// discriminated union check doesn't reject our narrow objects.
function ev(type: ServerMessage["type"]): ServerMessage {
  return { type } as unknown as ServerMessage;
}

// Fold a sequence of events onto the initial state, matching how
// App.tsx runs the observer path.
function run(events: ServerMessage["type"][]): { proc: boolean; finalizing: boolean } {
  return events.reduce((s, t) => nextObserverBusyState(s, ev(t)), IDLE);
}

describe("nextObserverBusyState", () => {
  it("IDLE has both flags false", () => {
    expect(IDLE).toEqual({ proc: false, finalizing: false });
  });

  describe("individual transitions", () => {
    it("run_started arms proc, clears finalizing", () => {
      expect(nextObserverBusyState({ proc: false, finalizing: true }, ev("run_started"))).toEqual({
        proc: true,
        finalizing: false,
      });
    });

    it("streaming_done clears proc, arms finalizing", () => {
      expect(nextObserverBusyState({ proc: true, finalizing: false }, ev("streaming_done"))).toEqual({
        proc: false,
        finalizing: true,
      });
    });

    it("run_completed only clears finalizing", () => {
      expect(nextObserverBusyState({ proc: false, finalizing: true }, ev("run_completed"))).toEqual({
        proc: false,
        finalizing: false,
      });
    });

    it("run_completed preserves proc if it were still set", () => {
      // Guard against a wire ordering where run_completed arrives
      // without a preceding streaming_done. Shouldn't happen but
      // the reducer must degrade gracefully.
      expect(nextObserverBusyState({ proc: true, finalizing: false }, ev("run_completed"))).toEqual({
        proc: true,
        finalizing: false,
      });
    });

    it("stream_end resets both flags", () => {
      expect(nextObserverBusyState({ proc: true, finalizing: true }, ev("stream_end"))).toEqual({
        proc: false,
        finalizing: false,
      });
    });
  });

  describe("unrelated events", () => {
    // Any event not in the state machine passes prev through — the
    // caller applies the message to its own item list separately.
    const cases: ServerMessage["type"][] = [
      "content_delta",
      "tool_started",
      "tool_completed",
      "status_update",
      "welcome",
      "run_paused",
    ];
    for (const t of cases) {
      it(`${t} does not change busy state`, () => {
        const start = { proc: true, finalizing: true };
        expect(nextObserverBusyState(start, ev(t))).toEqual(start);
      });
    }
  });

  describe("full-run sequences", () => {
    it("nominal run ends fully idle", () => {
      // The BE's actual emit order: RunStarted → deltas →
      // StreamingDone → tail → RunCompleted → StreamEnd.
      expect(
        run([
          "run_started",
          "content_delta",
          "content_delta",
          "streaming_done",
          "run_completed",
          "stream_end",
        ]),
      ).toEqual(IDLE);
    });

    // THE regression — this exact sequence used to leave the
    // observer stuck at { proc: false, finalizing: true } because
    // ``stream_end`` was handled in the same branch as
    // ``streaming_done`` and re-armed finalizing after
    // ``run_completed`` had cleared it.
    it("regression: stream_end after run_completed does NOT re-arm finalizing", () => {
      const state = run(["streaming_done", "run_completed", "stream_end"]);
      expect(state.finalizing).toBe(false);
      expect(state.proc).toBe(false);
    });

    it("two runs in a row: fresh run_started wipes leftover finalizing", () => {
      // If the second run_started fired before the first run's
      // stream_end delivered, an old-style observer would still
      // be in finalizing=true. run_started must reset it.
      expect(
        run([
          "run_started",
          "streaming_done",
          "run_started", // observer's leftover tail wiped by the new turn
          "content_delta",
        ]),
      ).toEqual({ proc: true, finalizing: false });
    });

    it("cancelled run: streaming_done then run_completed still lands idle", () => {
      // A user-cancelled turn skips content deltas but the BE still
      // emits the terminators.
      expect(run(["run_started", "streaming_done", "run_completed", "stream_end"])).toEqual(IDLE);
    });

    it("out-of-order run_completed before streaming_done: finalizing stays cleared", () => {
      // Defensive: if the wire ever reordered these two (e.g. an
      // intermediate proxy), the observer should still not lock up.
      const state = run(["run_started", "run_completed", "streaming_done", "stream_end"]);
      // After the whole tape, stream_end forces idle regardless.
      expect(state).toEqual(IDLE);
    });
  });
});
