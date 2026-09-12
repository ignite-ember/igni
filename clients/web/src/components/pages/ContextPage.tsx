import { useCallback, useEffect, useState } from "react";

import { Drawer } from "../panels/Drawer";
import type { EmberClient } from "../../protocol/client";

export interface ContextBreakdown {
  total: number;
  runs: number;
  floor: number;
}

/**
 * What the two numbers mean as proportions.
 *
 * Pure and exported so the arithmetic is testable without a backend:
 * the interesting cases are all degenerate ones — an empty session,
 * and a tokenizer disagreeing with itself hard enough that the parts
 * exceed the whole.
 */
export function shares(
  b: ContextBreakdown,
  maxContext: number,
): { runsPct: number; floorPct: number; usedPct: number } {
  const total = Math.max(b.total, 0);
  const pct = (n: number, of: number) => (of > 0 ? Math.min(100, (n / of) * 100) : 0);
  return {
    runsPct: pct(b.runs, total),
    floorPct: pct(b.floor, total),
    // Against the window, not the total — this is the one that says
    // how close the session is to needing a compact.
    usedPct: pct(total, maxContext),
  };
}

const fmt = (n: number) => n.toLocaleString();

/**
 * The `/ctx` page — where the context went.
 *
 * This used to print seven lines of markdown into the transcript,
 * which scrolled away and could not be returned to. It is a reading
 * people take repeatedly, usually while deciding whether to compact,
 * so it wants a place rather than a message.
 *
 * The numbers come from `get_context_breakdown` rather than from the
 * command's text: a page that re-reads on demand is the point, and
 * parsing a sentence back into integers to get there would be absurd.
 */
export function ContextPage({
  client,
  maxContext,
  onCompact,
  onClose,
}: {
  client: EmberClient;
  /** The model's window, from the status bar. */
  maxContext: number;
  /** Runs `/compact` and returns to the chat to watch it. */
  onCompact: () => void;
  onClose: () => void;
}) {
  const [breakdown, setBreakdown] = useState<ContextBreakdown | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setBreakdown(await client.rpc<ContextBreakdown>("get_context_breakdown"));
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [client]);

  useEffect(() => {
    void load();
  }, [load]);

  const s = breakdown ? shares(breakdown, maxContext) : null;

  return (
    <Drawer
      title="Context"
      onClose={onClose}
      headerExtras={
        <button className="btn btn-sm" onClick={() => void load()} disabled={loading}>
          {loading ? "Reading…" : "Refresh"}
        </button>
      }
    >
      {error && <div className="ctx-error">Could not read the context: {error}</div>}

      {breakdown && s && (
        <>
          <div className="ctx-total">
            <span className="ctx-total-n">{fmt(breakdown.total)}</span>
            <span className="ctx-total-unit">tokens in context</span>
            {maxContext > 0 && (
              <span className="ctx-total-of">
                {s.usedPct.toFixed(1)}% of a {fmt(maxContext)} window
              </span>
            )}
          </div>

          <div
            className="ctx-bar"
            role="img"
            aria-label={`Conversation ${s.runsPct.toFixed(0)}%, floor ${s.floorPct.toFixed(0)}%`}
          >
            <span className="ctx-bar-runs" style={{ width: `${s.runsPct}%` }} />
            <span className="ctx-bar-floor" style={{ width: `${s.floorPct}%` }} />
          </div>

          <div className="ctx-rows">
            <div className="row">
              <span className="name">
                <span className="ctx-key ctx-key-runs" aria-hidden="true" /> Conversation
              </span>
              <span className="meta">
                {fmt(breakdown.runs)} · {s.runsPct.toFixed(1)}%
              </span>
            </div>
            <div className="row">
              <span className="name">
                <span className="ctx-key ctx-key-floor" aria-hidden="true" /> Floor
              </span>
              <span className="meta">
                {fmt(breakdown.floor)} · {s.floorPct.toFixed(1)}%
              </span>
            </div>
          </div>

          <p className="ctx-note">
            The floor is the system prompt, tool schemas, project rules, memories and the
            injected session summary. It is rebaked into every prompt, so{" "}
            <code>/compact</code> only clears the conversation — which is why the meter does
            not drop to zero after compacting.
          </p>

          <button className="btn btn-primary" onClick={onCompact}>
            Compact the conversation
          </button>
        </>
      )}

      {!breakdown && !error && <div className="ctx-empty">Reading the context…</div>}
    </Drawer>
  );
}
