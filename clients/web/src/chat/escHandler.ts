/** Esc keydown policy — pure logic, no React.
 *
 * The App-level ``keydown`` listener dispatches to ``handleEsc``.
 * Splitting the decision from the useEffect lets us unit-test
 * every branch without rendering the full App tree (which
 * pulls in the WS client, the backend, the BE transport mock,
 * the composer + hitl + conversation refs — way too much
 * surface area for one branch).
 *
 * Returns an :class:`EscAction` describing what to do; the App
 * side-effect layer applies it.
 *
 * Branch order — matters, the first match wins:
 *
 * 1. **Not Escape** → no-op.
 * 2. **HITL dialog open** (with at least one pending req) →
 *    reject all pending. The agent is paused; dismissing the
 *    dialog also cancels the run.
 * 3. **Active generation** (``shouldShowSpinner`` true) →
 *    cancel the run.
 * 4. **Fullscreen** (``<html data-fullscreen="true">``) →
 *    swallow the event so macOS doesn't exit fullscreen. The
 *    user is in fullscreen because they wanted immersion;
 *    pressing Esc shouldn't kick them out.
 * 5. **Idle + not fullscreen** → no-op (let the OS / browser
 *    handle its default — close menus, etc.).
 *
 * ``preventDefault`` semantics: we call it whenever we're
 * consuming the event for an in-app action OR swallowing the
 * OS-level fullscreen exit. We do NOT call it on the
 * "not Escape" branch (irrelevant) or the "idle + not fullscreen"
 * branch (the OS / browser should do its default).
 */

import type { HITLRequest } from "../protocol/messages";

export interface EscAction {
  /** True when the App side-effect layer should call
   * ``event.preventDefault()`` to suppress the OS-level default
   * (fullscreen exit, menu close, etc.). */
  preventDefault: boolean;
  /** Decision the listener should apply. ``null`` means "do
   * nothing in-app" — the OS still gets the event. */
  decision:
    | null
    | { kind: "reject_hitl_and_cancel" }
    | { kind: "cancel_generation" }
    | { kind: "block_fullscreen_exit" };
}

/** Inputs the App hands the handler on each keydown. */
export interface EscContext {
  /** Pending HITL requirements (``null`` when no dialog is
   * showing). Pulled from App's ``hitl`` state. */
  hitl: HITLRequest[] | null;
  /** True when the run is in ``starting`` / ``streaming`` /
   * ``finalizing``. Pulled from
   * ``shouldShowSpinner(runPhaseRef.current)``. */
  isProcessing: boolean;
  /** True when ``document.documentElement.dataset.fullscreen``
   * is ``"true"`` (set by the Tauri shell's fullscreen detector
   * in ``lib.rs``). False on the web / VSCode / JetBrains hosts
   * (where the attribute is never set). */
  isFullscreen: boolean;
}

export function handleEsc(event: { key: string }, ctx: EscContext): EscAction {
  if (event.key !== "Escape") {
    return { preventDefault: false, decision: null };
  }

  // 1. HITL dialog open — reject everything in the batch.
  if (ctx.hitl && ctx.hitl.length > 0) {
    return {
      preventDefault: true,
      decision: { kind: "reject_hitl_and_cancel" },
    };
  }

  // 2. Active generation — cancel.
  if (ctx.isProcessing) {
    return {
      preventDefault: true,
      decision: { kind: "cancel_generation" },
    };
  }

  // 3. Fullscreen — swallow the OS-level "exit fullscreen on
  // Esc" gesture. WebKit fires the keydown to JS before the OS
  // intercepts it; cancelling here stops the OS from receiving.
  if (ctx.isFullscreen) {
    return {
      preventDefault: true,
      decision: { kind: "block_fullscreen_exit" },
    };
  }

  // 4. Idle + not fullscreen — let the OS / browser handle its
  // default.
  return { preventDefault: false, decision: null };
}
