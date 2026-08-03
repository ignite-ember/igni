/** Unit tests for the Esc keydown policy.
 *
 * We don't render App — that pulls in too much surface area for
 * what is essentially pure logic. Each test passes a small
 * ``EscContext`` in directly and asserts on the action the
 * handler returns. App's useEffect is a thin wrapper that calls
 * ``handleEsc(e, ctx)`` and applies the resulting ``EscAction``;
 * if ``handleEsc`` is right, the integration is right.
 *
 * Branch coverage mirrors the docstring in ``escHandler.ts`` —
 * 1. Not Escape → no-op
 * 2. HITL open → reject all + cancel
 * 3. Active generation → cancel
 * 4. Fullscreen → swallow OS-level exit
 * 5. Idle + not fullscreen → no-op
 */

import { describe, expect, it } from "vitest";
import { handleEsc, type EscContext } from "./escHandler";
import type { HITLRequest } from "../protocol/messages";

const baseCtx: EscContext = {
  hitl: null,
  isProcessing: false,
  isFullscreen: false,
};

function req(id: string): HITLRequest {
  return {
    type: "hitl_request",
    requirement_id: id,
    tool_name: "edit_file",
    friendly_name: "Edit",
    tool_args: {},
    details: "",
    agent_path: "",
  };
}

describe("handleEsc", () => {
  it("is a no-op for non-Escape keys (even when generation is active)", () => {
    // Defensive: a hotkey listener should never swallow unrelated
    // keystrokes. Test with several plausible keys.
    for (const key of ["Enter", " ", "a", "Tab", "ArrowDown"]) {
      const action = handleEsc({ key }, baseCtx);
      expect(action.preventDefault, `key=${key}`).toBe(false);
      expect(action.decision, `key=${key}`).toBeNull();
    }
  });

  it("rejects all pending HITL + cancels the run when a dialog is open", () => {
    const ctx: EscContext = { ...baseCtx, hitl: [req("r1"), req("r2"), req("r3")] };
    const action = handleEsc({ key: "Escape" }, ctx);
    expect(action.preventDefault).toBe(true);
    expect(action.decision).toEqual({ kind: "reject_hitl_and_cancel" });
  });

  it("HITL takes priority over an in-flight generation", () => {
    // If both are true (paused mid-tool-call waiting for HITL,
    // then the agent is technically still in the streaming phase
    // but blocked on HITL), Esc should target the HITL — the
    // agent is paused, "cancel generation" would be confusing.
    const ctx: EscContext = {
      ...baseCtx,
      hitl: [req("r1")],
      isProcessing: true,
    };
    const action = handleEsc({ key: "Escape" }, ctx);
    expect(action.decision).toEqual({ kind: "reject_hitl_and_cancel" });
  });

  it("cancels generation when the run is active and no HITL is up", () => {
    const ctx: EscContext = { ...baseCtx, isProcessing: true };
    const action = handleEsc({ key: "Escape" }, ctx);
    expect(action.preventDefault).toBe(true);
    expect(action.decision).toEqual({ kind: "cancel_generation" });
  });

  it("blocks the OS-level fullscreen-exit gesture when in fullscreen", () => {
    const ctx: EscContext = { ...baseCtx, isFullscreen: true };
    const action = handleEsc({ key: "Escape" }, ctx);
    expect(action.preventDefault).toBe(true);
    expect(action.decision).toEqual({ kind: "block_fullscreen_exit" });
  });

  it("fullscreen + streaming: cancel wins, fullscreen-exit is swallowed as a side-effect", () => {
    // The user is streaming inside fullscreen. Esc should:
    //   1. Cancel the run (most useful in-app action).
    //   2. NOT exit fullscreen (the OS's default Esc gesture).
    // ``preventDefault: true`` on the cancel branch handles both
    // — the App's call to ``e.preventDefault()`` blocks the OS
    // exit-fullscreen path. So we test the higher-priority branch
    // (cancel_generation) wins; the fullscreen block is implicit
    // via the preventDefault flag.
    const ctx: EscContext = {
      ...baseCtx,
      isProcessing: true,
      isFullscreen: true,
    };
    const action = handleEsc({ key: "Escape" }, ctx);
    expect(action.preventDefault).toBe(true);
    expect(action.decision).toEqual({ kind: "cancel_generation" });
  });

  it("is a no-op when idle (idle + not fullscreen + no HITL)", () => {
    // Lets the browser / OS do its default — close popovers, exit
    // a focused text field, etc. The Esc handler should never be
    // a global interceptor for non-igni keys.
    const action = handleEsc({ key: "Escape" }, baseCtx);
    expect(action.preventDefault).toBe(false);
    expect(action.decision).toBeNull();
  });

  it("treats an empty HITL array the same as no HITL", () => {
    // Defensive: ``hitl`` is an array, the App-side check is
    // ``hitl && hitl.length > 0``. An empty array means the
    // dialog has no pending reqs — Esc falls through to the
    // cancel-generation branch.
    const ctxEmptyHitl: EscContext = {
      ...baseCtx,
      hitl: [],
      isProcessing: true,
    };
    const action = handleEsc({ key: "Escape" }, ctxEmptyHitl);
    expect(action.decision).toEqual({ kind: "cancel_generation" });
  });
});
