/**
 * Tests for the RunPhase state model — the fix for the STOP-button
 * bug (spinner stuck at "Finalizing…" after cancel).
 *
 * The pure module has zero DOM / React coupling, so all invariants
 * can be tested without a component harness.
 */

import { describe, it, expect } from "vitest";

import {
  ACTIVE_PHASES,
  TERMINAL_PHASES,
  isProcessing,
  isFinalizing,
  shouldShowSpinner,
  phaseLabel,
  phaseFromProcFinalizing,
  phaseToProcFinalizing,
  type RunPhase,
} from "./runPhase";

describe("RunPhase — derived flags", () => {
  it("processing covers starting + streaming, nothing else", () => {
    const phases: RunPhase[] = [
      "idle",
      "starting",
      "streaming",
      "finalizing",
      "cancelled",
      "errored",
      "done",
    ];
    const processing = phases.filter(isProcessing);
    expect(processing.sort()).toEqual(["starting", "streaming"]);
  });

  it("finalizing is exclusive to the finalizing phase", () => {
    expect(isFinalizing("finalizing")).toBe(true);
    for (const p of ["idle", "starting", "streaming", "cancelled", "errored", "done"] as const) {
      expect(isFinalizing(p)).toBe(false);
    }
  });

  it("shouldShowSpinner covers every ACTIVE phase and no terminal one", () => {
    for (const p of ACTIVE_PHASES) {
      expect(shouldShowSpinner(p)).toBe(true);
    }
    for (const p of TERMINAL_PHASES) {
      expect(shouldShowSpinner(p)).toBe(false);
    }
  });

  it("ACTIVE_PHASES and TERMINAL_PHASES partition the RunPhase set", () => {
    const all: RunPhase[] = [
      "idle",
      "starting",
      "streaming",
      "finalizing",
      "cancelled",
      "errored",
      "done",
    ];
    for (const p of all) {
      // Exactly one of the two sets contains p.
      const inActive = ACTIVE_PHASES.has(p);
      const inTerminal = TERMINAL_PHASES.has(p);
      expect(inActive !== inTerminal).toBe(true);
    }
  });
});

describe("RunPhase — labels", () => {
  it("streaming + starting share the 'replying' label", () => {
    expect(phaseLabel("streaming")).toBe("igni is replying…");
    expect(phaseLabel("starting")).toBe("igni is replying…");
  });

  it("finalizing has its own label", () => {
    expect(phaseLabel("finalizing")).toBe("Finalizing…");
  });

  it("terminal phases produce empty labels", () => {
    for (const p of TERMINAL_PHASES) {
      expect(phaseLabel(p)).toBe("");
    }
  });
});

describe("RunPhase — legacy adapter (phaseToProcFinalizing)", () => {
  it("streaming ↔ {proc: true, finalizing: false}", () => {
    expect(phaseToProcFinalizing("streaming")).toEqual({
      proc: true,
      finalizing: false,
    });
  });

  it("starting ↔ {proc: true, finalizing: false}", () => {
    expect(phaseToProcFinalizing("starting")).toEqual({
      proc: true,
      finalizing: false,
    });
  });

  it("finalizing ↔ {proc: false, finalizing: true}", () => {
    expect(phaseToProcFinalizing("finalizing")).toEqual({
      proc: false,
      finalizing: true,
    });
  });

  it("idle / done / cancelled / errored ↔ {proc: false, finalizing: false}", () => {
    for (const p of ["idle", "done", "cancelled", "errored"] as const) {
      expect(phaseToProcFinalizing(p)).toEqual({
        proc: false,
        finalizing: false,
      });
    }
  });
});

describe("RunPhase — legacy adapter (phaseFromProcFinalizing)", () => {
  it("finalizing flag wins over proc flag", () => {
    expect(
      phaseFromProcFinalizing({ proc: true, finalizing: true }, "streaming"),
    ).toBe("finalizing");
  });

  it("proc-only yields streaming", () => {
    expect(
      phaseFromProcFinalizing({ proc: true, finalizing: false }, "starting"),
    ).toBe("streaming");
  });

  it("finalizing-only yields finalizing", () => {
    expect(
      phaseFromProcFinalizing({ proc: false, finalizing: true }, "streaming"),
    ).toBe("finalizing");
  });

  it("both false transitions non-terminal → done", () => {
    expect(
      phaseFromProcFinalizing({ proc: false, finalizing: false }, "streaming"),
    ).toBe("done");
    expect(
      phaseFromProcFinalizing({ proc: false, finalizing: false }, "finalizing"),
    ).toBe("done");
  });

  it("both false preserves terminal phases — don't downgrade cancelled to done", () => {
    // This is the CRITICAL invariant for the STOP-button fix. Cancel
    // sets phase=cancelled, then the observer bus reducer eventually
    // reports {proc: false, finalizing: false}. If we naively resolve
    // to "done" we'd lose the cancellation signal that other UI
    // subsystems might key on.
    expect(
      phaseFromProcFinalizing({ proc: false, finalizing: false }, "cancelled"),
    ).toBe("cancelled");
    expect(
      phaseFromProcFinalizing({ proc: false, finalizing: false }, "errored"),
    ).toBe("errored");
    expect(
      phaseFromProcFinalizing({ proc: false, finalizing: false }, "idle"),
    ).toBe("idle");
    expect(
      phaseFromProcFinalizing({ proc: false, finalizing: false }, "done"),
    ).toBe("done");
  });
});

describe("RunPhase — cancel is a single transition", () => {
  it("cancelling from any active phase clears the spinner immediately", () => {
    // The user's bug: from finalizing, cancel used to leave spinner
    // stuck. Now transition to "cancelled" makes shouldShowSpinner
    // false on the very next render.
    const activeStates: RunPhase[] = ["starting", "streaming", "finalizing"];
    for (const from of activeStates) {
      // Simulate the transition — App.tsx's setRunPhase("cancelled")
      // is the actual code path; here we assert the invariant that
      // the destination phase has spinner off.
      expect(shouldShowSpinner("cancelled")).toBe(false);
      // Also — cancel MUST clear the finalizing flag specifically
      // (that was the bug).
      expect(isFinalizing("cancelled")).toBe(false);
      // And processing.
      expect(isProcessing("cancelled")).toBe(false);
      // Sanity — the source phase we're transitioning from was
      // active, otherwise there's nothing to cancel.
      expect(shouldShowSpinner(from)).toBe(true);
    }
  });
});
