/**
 * Minimal end-to-end demo of the visualizer streaming pipeline.
 *
 * Reachable at ``?demo=viz-stream`` (see main.tsx). This page is
 * the smallest possible harness that proves the FULL client-side
 * streaming path works:
 *
 *   1. Simulate the BE by feeding progressively-larger prefixes of
 *      a target JSON spec into ``applyVisualizationDelta`` on a
 *      timer (~50ms per delta — same rate as the real BE).
 *   2. Reduce each delta into a ``ChatItem`` list.
 *   3. Render each visualization item via ``JsonRenderView``.
 *
 * If this page shows a chart that fills in progressively, the
 * client-side pipeline is correct. Any real-app failure past that
 * point is an integration bug (BE emission, WebSocket transport,
 * App.tsx routing) — NOT a rendering bug.
 *
 * Playwright is pointed at ``tests/e2e/visualizer-stream.spec.ts``
 * to screenshot this page at defined moments and assert the DOM
 * matches expected states.
 */

import { Component, useCallback, useMemo, useRef, useState, type ReactNode } from "react";
import type { ChatItem } from "../chat/model";
import { applyVisualizationDelta } from "../chat/visualizationStream";
import { ChatItemView } from "../components/ChatItems";

// Simple error boundary so a bad partial spec doesn't blank the
// entire demo page — if the Renderer throws mid-stream we render a
// small red-text notice instead, and the next good delta restores
// the chart. In the real app this same protection lives at the
// ChatItemView boundary; the demo mounts items directly so we add
// our own here.
class RenderErrorBoundary extends Component<
  { children: ReactNode },
  { error: Error | null }
> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  componentDidUpdate(_prev: unknown, prevState: { error: Error | null }) {
    // Clear the error on ANY new children arrival so a following
    // safe delta re-renders normally. Without this the boundary
    // latches at the first crash and never recovers.
    if (prevState.error && this.state.error === prevState.error) {
      // Reset via a microtask so we don't setState during render.
      queueMicrotask(() => this.setState({ error: null }));
    }
  }
  render() {
    if (this.state.error) {
      return (
        <div
          data-testid="stream-error"
          style={{
            padding: 10,
            background: "rgba(220,38,38,0.1)",
            border: "1px solid rgba(220,38,38,0.4)",
            borderRadius: 6,
            fontFamily: "ui-monospace, monospace",
            fontSize: 12,
            color: "#dc2626",
          }}
        >
          Render error: {String(this.state.error.message).slice(0, 200)}
        </div>
      );
    }
    return this.props.children;
  }
}

// ── Canonical AAPL LineGraph spec — same shape a real visualizer
//    run would emit. Small dataset so we can fit the whole prefix
//    walk into a few seconds. ──────────────────────────────────────

const AAPL_SPEC = {
  root: "root",
  elements: {
    root: {
      type: "Card",
      props: { title: "AAPL — Monthly Close", subtitle: "2023" },
      children: ["chart"],
    },
    chart: {
      type: "LineGraph",
      props: {
        yPrefix: "$",
        xLabel: "Month",
        yLabel: "Close",
        data: [
          { x: "Jan", y: 143.0 },
          { x: "Feb", y: 147.41 },
          { x: "Mar", y: 164.9 },
          { x: "Apr", y: 169.68 },
          { x: "May", y: 177.25 },
          { x: "Jun", y: 193.97 },
          { x: "Jul", y: 196.45 },
          { x: "Aug", y: 187.87 },
          { x: "Sep", y: 171.21 },
          { x: "Oct", y: 170.77 },
          { x: "Nov", y: 189.95 },
          { x: "Dec", y: 192.53 },
        ],
      },
      children: [],
    },
  },
};

const TARGET_JSON = JSON.stringify(AAPL_SPEC);
const SPEC_ID = "demo-run";

export function VisualizerStreamDemo() {
  const [items, setItems] = useState<ChatItem[]>([]);
  const [status, setStatus] = useState<"idle" | "streaming" | "done">("idle");
  const [cursor, setCursor] = useState(0);
  const timerRef = useRef<number | null>(null);

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

  const start = useCallback(() => {
    reset();
    setStatus("streaming");
    let pos = 0;
    // Chunk size ~2% of the target so we get ~50 deltas total —
    // enough to visibly stream a small spec in a few seconds.
    const CHUNK = Math.max(1, Math.floor(TARGET_JSON.length / 50));
    timerRef.current = window.setInterval(() => {
      pos = Math.min(TARGET_JSON.length, pos + CHUNK);
      setCursor(pos);
      const slice = TARGET_JSON.slice(0, pos);
      setItems((prev) =>
        applyVisualizationDelta(prev, { spec_id: SPEC_ID, json: slice }),
      );
      if (pos >= TARGET_JSON.length) {
        stop();
        setStatus("done");
      }
    }, 50);
  }, [reset, stop]);

  const one = useCallback(() => {
    reset();
    setItems((prev) =>
      applyVisualizationDelta(prev, { spec_id: SPEC_ID, json: TARGET_JSON }),
    );
    setCursor(TARGET_JSON.length);
    setStatus("done");
  }, [reset]);

  const progressPct = useMemo(
    () => Math.round((cursor / TARGET_JSON.length) * 100),
    [cursor],
  );

  return (
    <div className="viz-stream-demo">
      <header>
        <h1>Visualizer streaming — client pipeline sandbox</h1>
        <p className="viz-stream-hint">
          Feeds growing prefixes of a real json-render spec into{" "}
          <code>applyVisualizationDelta</code> and renders each update via{" "}
          <code>JsonRenderView</code>. Prove the client works here; any
          real-app failure past this point is a transport/BE bug.
        </p>
        <div className="viz-stream-controls">
          <button
            type="button"
            data-testid="stream-start"
            onClick={start}
            disabled={status === "streaming"}
          >
            Stream (50ms/delta)
          </button>
          <button type="button" data-testid="stream-one" onClick={one}>
            One-shot (full spec)
          </button>
          <button type="button" data-testid="stream-reset" onClick={reset}>
            Reset
          </button>
        </div>
        <div
          className="viz-stream-status"
          data-testid="stream-status"
          data-status={status}
          data-progress={progressPct}
        >
          {status} · {progressPct}% · {cursor}/{TARGET_JSON.length} chars ·{" "}
          {items.filter((i) => i.kind === "visualization").length} card(s)
        </div>
      </header>

      {/* Debug: expose the current spec as visible text. Off by
          default so the sandbox stays clean; ``?debug=1`` turns it
          back on for future diagnostic runs. */}
      {new URLSearchParams(window.location.search).get("debug") === "1" && (
        <pre
          data-testid="stream-debug"
          style={{
            fontSize: 11,
            fontFamily: "ui-monospace, monospace",
            background: "var(--bg-inset)",
            padding: 8,
            borderRadius: 6,
            overflow: "auto",
            maxHeight: 200,
          }}
        >
          {JSON.stringify(
            items
              .filter((i) => i.kind === "visualization")
              .map((i) => {
                const v = i as Extract<ChatItem, { kind: "visualization" }>;
                const spec = v.spec as {
                  root?: string;
                  elements?: Record<string, { type?: string; children?: string[] }>;
                };
                return {
                  specId: v.specId,
                  root: spec.root ?? "",
                  elementIds: Object.keys(spec.elements ?? {}),
                  rootType: spec.elements?.[spec.root ?? ""]?.type,
                  rootChildren: spec.elements?.[spec.root ?? ""]?.children ?? [],
                };
              }),
            null,
            2,
          )}
        </pre>
      )}

      <main
        className="viz-stream-body"
        data-testid="stream-body"
        data-card-count={items.filter((i) => i.kind === "visualization").length}
      >
        {items.length === 0 ? (
          <div className="viz-stream-empty" data-testid="stream-empty">
            No items yet. Click <strong>Stream</strong> to start.
          </div>
        ) : (
          items.map((item) => (
            <RenderErrorBoundary key={item.id}>
              <ChatItemView item={item} />
            </RenderErrorBoundary>
          ))
        )}
      </main>

      <style>{`
        .viz-stream-demo {
          max-width: 900px;
          margin: 24px auto;
          padding: 24px;
          font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
          color: var(--fg);
        }
        .viz-stream-demo header { margin-bottom: 24px; }
        .viz-stream-demo h1 { font-size: 18px; margin: 0 0 8px; }
        .viz-stream-hint {
          font-size: 13px;
          color: var(--fg-muted);
          margin: 0 0 12px;
        }
        .viz-stream-hint code {
          font-family: var(--font-mono);
          font-size: 12px;
          background: var(--bg-raised);
          padding: 1px 4px;
          border-radius: 4px;
        }
        .viz-stream-controls {
          display: flex;
          gap: 8px;
          margin: 12px 0;
        }
        .viz-stream-controls button {
          padding: 6px 12px;
          font-family: system-ui, sans-serif;
          font-size: 13px;
          border: 1px solid var(--border);
          background: var(--bg-raised);
          color: var(--fg);
          border-radius: 6px;
          cursor: pointer;
        }
        .viz-stream-controls button:disabled {
          opacity: 0.5;
          cursor: not-allowed;
        }
        .viz-stream-status {
          font-family: var(--font-mono);
          font-size: 12px;
          color: var(--fg-muted);
          padding: 6px 10px;
          background: var(--bg-inset);
          border-radius: 6px;
          display: inline-block;
        }
        .viz-stream-body {
          display: flex;
          flex-direction: column;
          gap: 12px;
        }
        .viz-stream-empty {
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
