/**
 * Minimal end-to-end demo of the workflow live-progress card.
 *
 * Reachable at ``?demo=workflow`` (see main.tsx). Feeds the same
 * event tape the BE's workflow runner emits on the
 * ``workflow_event`` push channel into the real
 * ``reduceWorkflowEvent`` reducer, then renders the result via
 * the real :class:`WorkflowRun` component. So this page is a
 * fully client-side sandbox — no BE, no Node bridge, no
 * refactor-to-standards.mjs — that proves the FULL rendering
 * pipeline works:
 *
 *   1. Replay the canned event tape (5 phases + 9 agent calls)
 *      on a timer at the same ~50ms cadence the BE uses.
 *   2. Reduce each event into the ``WorkflowRunState`` typed
 *      structure.
 *   3. Render each phase card + agent card via
 *      ``<WorkflowRun />``.
 *
 * If this page shows a phase tree unfolding progressively and
 * landing on a clean ``✓ completed`` final state, the client
 * rendering path is correct. Any real-app failure past that
 * point is a transport/BE/integration bug.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  reduceWorkflowEvent,
  workflowItem,
  type ChatItem,
  type WorkflowEvent,
} from "../chat/model";
import { WorkflowRun } from "../components/WorkflowRun";

// ── Canned event tape (mirrors what the BE pushes for
//    refactor-to-standards.mjs). Each entry is the JSON shape
//    the BE's runner drains from the Node subprocess's stdout
//    and re-emits on the ``workflow_event`` push channel. ──────

const RUN_ID = "wf_demo";
// Demo tape: timestamps 1 hour ago + 50ms apart. Use a real
// "now - 1h" anchor so the displayed elapsed time is sensible
// when the demo runs (the canned ts values would otherwise be
// pinned to 2023-11-14, making every card show -3 years).
const START_MS = Date.now() - 60 * 60 * 1000;

function ts(seq: number): number {
  return START_MS + seq * 50;
}

const EVENTS: WorkflowEvent[] = [
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(0), seq: 0, type: "workflow_started",
    payload: { name: "refactor-to-standards", args: { file: "src/example.py" } },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(1), seq: 1, type: "phase_started",
    payload: { phase_id: "phase_1_demo", title: "Assess" },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(2), seq: 2, type: "agent_started",
    payload: { id: "req_assess", label: "assess", phase: "Assess" },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(3), seq: 3, type: "agent_completed",
    payload: {
      id: "req_assess", label: "assess", status: "completed",
      result: { file_summary: "Module-level functions, no classes.", oop_offenders: 3 },
    },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(4), seq: 4, type: "log",
    payload: { text: "Audit complete: 3 OOP offenders found." },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(5), seq: 5, type: "phase_completed",
    payload: { phase_id: "phase_1_demo", status: "completed" },
  },

  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(6), seq: 6, type: "phase_started",
    payload: { phase_id: "phase_2_demo", title: "Design" },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(7), seq: 7, type: "parallel_started",
    payload: { group_id: "par_design", lane_count: 3 },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(8), seq: 8, type: "agent_started",
    payload: { id: "req_d_minimal", label: "design:oop-minimal", phase: "Design" },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(8), seq: 9, type: "agent_started",
    payload: { id: "req_d_arch", label: "design:oop-architectural", phase: "Design" },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(8), seq: 10, type: "agent_started",
    payload: { id: "req_d_cons", label: "design:oop-consistency", phase: "Design" },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(9), seq: 11, type: "agent_completed",
    payload: { id: "req_d_minimal", label: "design:oop-minimal", status: "completed",
      result: { approach_name: "minimal-OOP", target_grade: "B+" } },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(9), seq: 12, type: "agent_completed",
    payload: { id: "req_d_arch", label: "design:oop-architectural", status: "completed",
      result: { approach_name: "architectural-OOP", target_grade: "A-" } },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(10), seq: 13, type: "agent_completed",
    payload: { id: "req_d_cons", label: "design:oop-consistency", status: "completed",
      result: { approach_name: "schema-consistent", target_grade: "B" } },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(11), seq: 14, type: "parallel_completed",
    payload: { group_id: "par_design", status: "completed" },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(12), seq: 15, type: "agent_started",
    payload: { id: "req_judge", label: "judge", phase: "Design" },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(13), seq: 16, type: "agent_completed",
    payload: { id: "req_judge", label: "judge", status: "completed",
      result: { winner: "minimal-OOP", rationale: "minimum diff that still enforces OOP." } },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(14), seq: 17, type: "log",
    payload: { text: 'Chosen: "minimal-OOP"' },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(15), seq: 18, type: "phase_completed",
    payload: { phase_id: "phase_2_demo", status: "completed" },
  },

  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(16), seq: 19, type: "phase_started",
    payload: { phase_id: "phase_3_demo", title: "Implement" },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(17), seq: 20, type: "agent_started",
    payload: { id: "req_implement", label: "implement", phase: "Implement" },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(18), seq: 21, type: "agent_completed",
    payload: { id: "req_implement", label: "implement", status: "completed",
      result: { files_edited: ["src/example.py"], lines_changed: 42 } },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(19), seq: 22, type: "log",
    payload: { text: "Refactor applied." },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(20), seq: 23, type: "phase_completed",
    payload: { phase_id: "phase_3_demo", status: "completed" },
  },

  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(21), seq: 24, type: "phase_started",
    payload: { phase_id: "phase_4_demo", title: "Review" },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(22), seq: 25, type: "parallel_started",
    payload: { group_id: "par_review", lane_count: 2 },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(23), seq: 26, type: "agent_started",
    payload: { id: "req_r1", label: "review:local-optimum-and-oop", phase: "Review" },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(23), seq: 27, type: "agent_started",
    payload: { id: "req_r2", label: "review:grade-and-oop-purity", phase: "Review" },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(24), seq: 28, type: "agent_completed",
    payload: { id: "req_r1", label: "review:local-optimum-and-oop", status: "completed",
      result: { greenlight: true, grade_estimate: "A-" } },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(25), seq: 29, type: "agent_completed",
    payload: { id: "req_r2", label: "review:grade-and-oop-purity", status: "completed",
      result: { greenlight: true, grade_estimate: "A-" } },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(26), seq: 30, type: "parallel_completed",
    payload: { group_id: "par_review", status: "completed" },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(27), seq: 31, type: "log",
    payload: { text: "All reviewers greenlit." },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(28), seq: 32, type: "phase_completed",
    payload: { phase_id: "phase_4_demo", status: "completed" },
  },

  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(29), seq: 33, type: "phase_started",
    payload: { phase_id: "phase_5_demo", title: "Verify" },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(30), seq: 34, type: "agent_started",
    payload: { id: "req_verify", label: "verify", phase: "Verify" },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(31), seq: 35, type: "agent_completed",
    payload: { id: "req_verify", label: "verify", status: "completed",
      result: { tests_passed: 47, tests_failed: 0, command_run: "pytest tests/test_src_example_py.py" } },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(32), seq: 36, type: "phase_completed",
    payload: { phase_id: "phase_5_demo", status: "completed" },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(33), seq: 37, type: "workflow_completed",
    payload: {
      status: "completed",
      result: {
        target: "src/example.py",
        status: "clean",
        chosen_design: "minimal-OOP",
        verification: {
          tests_passed: 47,
          tests_failed: 0,
          command_run: "pytest tests/test_src_example_py.py",
        },
        reviews: [
          { greenlight: true, grade_estimate: "A-" },
          { greenlight: true, grade_estimate: "A-" },
        ],
      },
    },
  },
];

// ── Failed-run tape (the same workflow, but with one failed
//    reviewer + a failed phase + a run-level error). Same shape
//    as EVENTS so the same reducer + component render the whole
//    failure UX (banner, per-phase rollup, Rerun/Cancel buttons).
const FAILED_TAPE: WorkflowEvent[] = [
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(0), seq: 0, type: "workflow_started",
    payload: { name: "refactor-to-standards", args: { file: "src/example.py" } },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(1), seq: 1, type: "phase_started",
    payload: { phase_id: "phase_1_demo", title: "Assess" },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(2), seq: 2, type: "agent_started",
    payload: { id: "req_assess", label: "assess", phase: "Assess" },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(3), seq: 3, type: "agent_completed",
    payload: {
      id: "req_assess", label: "assess", status: "completed",
      result: { file_summary: "Module-level functions, no classes.", oop_offenders: 3 },
    },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(4), seq: 4, type: "log",
    payload: { text: "Audit complete: 3 OOP offenders found." },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(5), seq: 5, type: "phase_completed",
    payload: { phase_id: "phase_1_demo", status: "completed" },
  },

  // Design: 3 parallel designers, 1 succeeds, 2 fail.
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(6), seq: 6, type: "phase_started",
    payload: { phase_id: "phase_2_demo", title: "Design" },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(7), seq: 7, type: "parallel_started",
    payload: { group_id: "par_design", lane_count: 3 },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(8), seq: 8, type: "agent_started",
    payload: { id: "req_d_minimal", label: "design:oop-minimal", phase: "Design" },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(8), seq: 9, type: "agent_started",
    payload: { id: "req_d_arch", label: "design:oop-architectural", phase: "Design" },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(8), seq: 10, type: "agent_started",
    payload: { id: "req_d_cons", label: "design:oop-consistency", phase: "Design" },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(9), seq: 11, type: "agent_completed",
    payload: {
      id: "req_d_minimal", label: "design:oop-minimal", status: "completed",
      result: { approach_name: "minimal-OOP", target_grade: "B+" },
    },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(10), seq: 12, type: "agent_completed",
    payload: {
      id: "req_d_arch", label: "design:oop-architectural", status: "failed",
      error: "free-function-with-state-arg still present in d-architectural",
    },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(10), seq: 13, type: "agent_completed",
    payload: {
      id: "req_d_cons", label: "design:oop-consistency", status: "timeout",
      error: "agent timed out after 600s",
    },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(11), seq: 14, type: "parallel_completed",
    payload: { group_id: "par_design", status: "failed" },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(12), seq: 15, type: "log",
    payload: { text: "Design phase failed — 2 of 3 lanes broken." },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(13), seq: 16, type: "phase_completed",
    payload: { phase_id: "phase_2_demo", status: "failed" },
  },

  // Run-level failure.
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(14), seq: 17, type: "workflow_failed",
    payload: {
      error: "Design phase failed: 2 of 3 design lanes reported blockers",
    },
  },
  {
    workflow_run_id: RUN_ID, name: "refactor-to-standards",
    ts: ts(15), seq: 18, type: "workflow_completed",
    payload: {
      status: "failed",
      result: {
        target: "src/example.py",
        status: "needs-attention",
        chosen_design: "minimal-OOP",
        verification: { tests_passed: 0, tests_failed: 0 },
      },
    },
  },
];

// ── Replay loop ──────────────────────────────────────────────────

type Status = "idle" | "streaming" | "done";

const STEP_MS = 150; // one event every 150ms — slow enough to see unfold

export function WorkflowDemo() {
  const [items, setItems] = useState<ChatItem[]>([]);
  const [status, setStatus] = useState<Status>("idle");
  const [cursor, setCursor] = useState(0);
  const timerRef = useRef<number | null>(null);
  // The most-recent tape the user started or jumped to. The
  // ``onRerun`` callback on each rendered workflow card reads
  // this to know which tape to replay when the user clicks the
  // Rerun button. Defaults to EVENTS (success) so the first
  // render has a valid tape to point at.
  const lastTapeRef = useRef<WorkflowEvent[]>(EVENTS);

  const stop = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const reset = useCallback(() => {
    stop();
    setItems([]);
    setCursor(0);
    setStatus("idle");
  }, [stop]);

  const start = useCallback(
    (tape: WorkflowEvent[] = EVENTS) => {
      reset();
      setStatus("streaming");
      lastTapeRef.current = tape;
      // Optimistic card so the user sees something before the first
      // event lands.
      setItems([workflowItem("refactor-to-standards", RUN_ID)]);
      let pos = 0;
      timerRef.current = window.setInterval(() => {
        pos = Math.min(tape.length, pos + 1);
        setCursor(pos);
        setItems((prev) => reduceWorkflowEvent(prev, tape[pos - 1]));
        if (pos >= tape.length) {
          stop();
          setStatus("done");
        }
      }, STEP_MS);
    },
    [reset, stop],
  );

  const jumpToEnd = useCallback(
    (tape: WorkflowEvent[] = EVENTS) => {
      reset();
      lastTapeRef.current = tape;
      let next: ChatItem[] = [workflowItem("refactor-to-standards", RUN_ID)];
      for (const ev of tape) {
        next = reduceWorkflowEvent(next, ev);
      }
      setItems(next);
      setCursor(tape.length);
      setStatus("done");
    },
    [reset],
  );

  // Free the timer on unmount.
  useEffect(() => () => stop(), [stop]);

  const progressPct = useMemo(
    () => Math.round((cursor / EVENTS.length) * 100),
    [cursor],
  );

  return (
    <div className="wf-demo">
      <header>
        <h1>Workflow live-progress — client pipeline sandbox</h1>
        <p className="wf-demo-hint">
          Replays the same event tape the BE pushes on the{" "}
          <code>workflow_event</code> channel (a canned version of{" "}
          what <code>refactor-to-standards.mjs</code> emits when the
          BE's Node bridge runs it). Feeds each event into{" "}
          <code>reduceWorkflowEvent</code>, renders the resulting
          <code>WorkflowRunState</code> via the same{" "}
          <code>&lt;WorkflowRun /&gt;</code> component the chat uses.
        </p>
        <div className="wf-demo-controls">
          <button
            type="button"
            data-testid="wf-stream"
            onClick={() => start(EVENTS)}
            disabled={status === "streaming"}
          >
            Stream success (150ms/event)
          </button>
          <button
            type="button"
            data-testid="wf-stream-failed"
            onClick={() => start(FAILED_TAPE)}
            disabled={status === "streaming"}
            title="Replay a failure scenario: 2 of 3 design lanes fail,
              the phase fails, the run ends with a Rerun button visible."
          >
            Stream failure
          </button>
          <button
            type="button"
            data-testid="wf-jump"
            onClick={() => jumpToEnd(EVENTS)}
            disabled={status === "streaming"}
          >
            Jump to success
          </button>
          <button
            type="button"
            data-testid="wf-jump-failed"
            onClick={() => jumpToEnd(FAILED_TAPE)}
            disabled={status === "streaming"}
          >
            Jump to failure
          </button>
          <button
            type="button"
            data-testid="wf-reset"
            onClick={reset}
          >
            Reset
          </button>
        </div>
        <div
          className="wf-demo-status"
          data-testid="wf-status"
          data-status={status}
          data-progress={progressPct}
        >
          {status} · {progressPct}% · {cursor}/{EVENTS.length} events ·{" "}
          {items.filter((i) => i.kind === "workflow").length} run(s)
        </div>
      </header>

      <main
        className="wf-demo-body"
        data-testid="wf-body"
        data-run-count={items.filter((i) => i.kind === "workflow").length}
      >
        {items.length === 0 ? (
          <div className="wf-demo-empty" data-testid="wf-empty">
            No items yet. Click <strong>Stream</strong> to start.
          </div>
        ) : (
          items.map((item) => (
            <div key={item.id} className="wf-demo-item">
              <WorkflowRun
                run={item.kind === "workflow" ? item.run : (null as never)}
                onRerun={() => {
                  // Demo: clicking the in-card Rerun button
                  // replays the same tape the user originally
                  // ran (success → success, failure → failure).
                  // In a real session this would re-fire the
                  // run_workflow RPC with the same name + args.
                  jumpToEnd(lastTapeRef.current);
                }}
                onCancel={() => {
                  // Demo: clicking the in-card Cancel button
                  // stops any in-flight timer (cancels the stream
                  // mid-run). For runs that are already done
                  // (jumped to end), this is a no-op. In a real
                  // session this would call the cancel_workflow
                  // RPC, which writes {"type":"cancel"} to the
                  // subprocess stdin and escalates to SIGTERM
                  // after a grace period.
                  stop();
                }}
              />
            </div>
          ))
        )}
      </main>

      <style>{`
        .wf-demo {
          max-width: 900px;
          margin: 0 auto;
          padding: 24px;
          font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
          color: var(--fg);
          /* Constrain the whole demo to the viewport so the
             body becomes scrollable rather than the page
             itself — the user never has to scroll the demo
             shell, only the workflow card inside it. */
          height: 100vh;
          display: flex;
          flex-direction: column;
        }
        .wf-demo > header { flex: 0 0 auto; }
        .wf-demo > main { flex: 1 1 auto; min-height: 0; }
        .wf-demo header { margin-bottom: 24px; }
        .wf-demo h1 { font-size: 18px; margin: 0 0 8px; }
        .wf-demo-hint {
          font-size: 13px;
          color: var(--fg-muted);
          margin: 0 0 12px;
          line-height: 1.5;
        }
        .wf-demo-hint code {
          font-family: var(--font-mono);
          font-size: 12px;
          background: var(--bg-raised);
          padding: 1px 4px;
          border-radius: 4px;
        }
        .wf-demo-controls {
          display: flex;
          gap: 8px;
          margin: 12px 0;
        }
        .wf-demo-controls button {
          padding: 6px 12px;
          font-family: system-ui, sans-serif;
          font-size: 13px;
          border: 1px solid var(--border);
          background: var(--bg-raised);
          color: var(--fg);
          border-radius: 6px;
          cursor: pointer;
        }
        .wf-demo-controls button:disabled {
          opacity: 0.5;
          cursor: not-allowed;
        }
        .wf-demo-status {
          font-family: var(--font-mono);
          font-size: 12px;
          color: var(--fg-muted);
          padding: 6px 10px;
          background: var(--bg-inset);
          border-radius: 6px;
          display: inline-block;
        }
        .wf-demo-body {
          display: flex;
          flex-direction: column;
          gap: 16px;
          /* The full 5-phase + 9-agent run card is ~900 px tall
             on its own — too big to fit on one screen next to the
             page header + controls. Constrain the body to a
             viewport-sized scrollable region so the user can pan
             the full card without leaving the page. */
          max-height: min(70vh, 720px);
          overflow-y: auto;
          padding: 4px 4px 12px;
          /* Fade out the top + bottom of the scroll region so it's
             obvious the content continues. */
          mask-image: linear-gradient(
            to bottom,
            transparent 0,
            black 12px,
            black calc(100% - 12px),
            transparent 100%
          );
          -webkit-mask-image: linear-gradient(
            to bottom,
            transparent 0,
            black 12px,
            black calc(100% - 12px),
            transparent 100%
          );
        }

        /* Custom thin scrollbar so the fade-out masks don't fight
           with a chunky default one. */
        .wf-demo-body::-webkit-scrollbar {
          width: 6px;
        }
        .wf-demo-body::-webkit-scrollbar-thumb {
          background: var(--border);
          border-radius: 3px;
        }
        .wf-demo-body::-webkit-scrollbar-track {
          background: transparent;
        }
        .wf-demo-empty {
          padding: 40px;
          text-align: center;
          color: var(--fg-faint);
          border: 1px dashed var(--border-soft);
          border-radius: 8px;
        }
      `}</style>
    </div>
  );
}
