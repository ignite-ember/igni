/**
 * Run lifecycle — single source of truth for what phase the current
 * assistant turn is in. Every UI derivation (spinner visible,
 * composer enabled, "Finalizing…" label) reads from `RunPhase` via
 * the derived getters here.
 *
 * Why this exists: before this file, `App.tsx` had TWO independent
 * booleans (`processing`, `finalizing`) each set from 3+ sites and
 * cleared from 3+ others. The cancel path missed one of them (the
 * `finalizing` flag was only cleared by `run_started` /
 * `run_completed` events, but a cancelled run emits neither). Result:
 * STOP button visually did nothing.
 *
 * The fix is model-first, per CODE_STANDARDS.md Pattern 1: one enum,
 * derived flags, cancel is a single transition. Every subsystem that
 * cares about "is the run active" reads the same state.
 */

/**
 * Every phase a run can be in from the FE's point of view.
 *
 * Ordering is meaningful — later phases dominate on transition
 * conflicts (see `nextPhaseFrom` for the conflict resolution rules).
 */
export type RunPhase =
  | "idle"        // no run active; app just booted, or a run finished cleanly
  | "starting"    // user just submitted; awaiting first run_started event
  | "streaming"   // model is producing content; deltas flowing
  | "finalizing"  // streaming_done fired; BE tail draining (memory / stats)
  | "cancelled"   // user hit STOP; local terminal state, don't wait for BE
  | "errored"     // run_error fired; local terminal state
  | "done";       // run_completed fired for a top-level run

/**
 * Phases that count as "the FE should treat the run as active" —
 * spinner visible, cancel button armed, composer input blocked.
 *
 * Kept as a Set so callers can `activePhases.has(phase)` in reducer
 * tests without invoking a component.
 */
export const ACTIVE_PHASES: ReadonlySet<RunPhase> = new Set<RunPhase>([
  "starting",
  "streaming",
  "finalizing",
]);

/**
 * Phases that are terminal — the run is over, whether cleanly or not.
 * The next `run_started` transitions out of these back to `starting`.
 */
export const TERMINAL_PHASES: ReadonlySet<RunPhase> = new Set<RunPhase>([
  "idle",
  "cancelled",
  "errored",
  "done",
]);

// ── Derived UI flags ─────────────────────────────────────────────────

/**
 * True while the model is actively producing content. Blocks composer
 * submit, shows the "Ember is replying…" label.
 */
export function isProcessing(phase: RunPhase): boolean {
  return phase === "starting" || phase === "streaming";
}

/**
 * True while the BE tail is still draining after content ended.
 * Composer is unblocked (per TUI parity) but the spinner stays
 * visible with a distinct label. Cancel + error don't count — the
 * FE has already resolved the run from its point of view.
 */
export function isFinalizing(phase: RunPhase): boolean {
  return phase === "finalizing";
}

/**
 * True while the spinner should be visible for any reason —
 * processing OR finalizing. Cheaper than computing both.
 */
export function shouldShowSpinner(phase: RunPhase): boolean {
  return ACTIVE_PHASES.has(phase);
}

/**
 * Human-readable label for the spinner. Empty string when hidden.
 */
export function phaseLabel(phase: RunPhase): string {
  switch (phase) {
    case "starting":
    case "streaming":
      return "igni is replying…";
    case "finalizing":
      return "Finalizing…";
    default:
      return "";
  }
}

// ── Legacy adapter — `{proc, finalizing}` ↔ `RunPhase` ───────────────

/**
 * The `nextObserverBusyState` reducer takes and returns
 * `{proc, finalizing}` — the pre-refactor shape. Keep its interface
 * unchanged (it's an A-tier pure reducer with its own tests) and
 * translate at the App boundary.
 */
export interface ProcFinalizing {
  proc: boolean;
  finalizing: boolean;
}

/**
 * Convert current phase into the `{proc, finalizing}` shape the
 * observer reducer expects.
 */
export function phaseToProcFinalizing(phase: RunPhase): ProcFinalizing {
  return {
    proc: isProcessing(phase),
    finalizing: isFinalizing(phase),
  };
}

/**
 * Merge the observer reducer's `{proc, finalizing}` output back into
 * a `RunPhase`. Handles the flag-model → phase-model translation
 * cleanly at the boundary.
 *
 * Rules:
 * - Both flags false when we WERE processing → done.
 * - `finalizing=true` → "finalizing".
 * - `proc=true, finalizing=false` → "streaming" (observer bus never
 *   distinguishes "starting" from "streaming"; safe default).
 * - Both false when we were already terminal → stay in the current
 *   terminal phase (don't downgrade "cancelled"/"errored" to "done").
 */
export function phaseFromProcFinalizing(
  next: ProcFinalizing,
  currentPhase: RunPhase,
): RunPhase {
  if (next.finalizing) return "finalizing";
  if (next.proc) return "streaming";
  // Both false. Preserve terminal phases; otherwise resolve to done.
  if (TERMINAL_PHASES.has(currentPhase)) return currentPhase;
  return "done";
}
