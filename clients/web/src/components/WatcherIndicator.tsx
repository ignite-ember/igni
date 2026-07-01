import { useEffect, useState } from "react";
import type { EmberClient } from "../protocol/client";
import type { ServerMessage } from "../protocol/messages";

/** Footer pill that mirrors the running-background-process
 *  count. Click → opens the watcher panel. Hidden when nothing
 *  is running so the footer doesn't clutter — the indicator's
 *  whole job is "you have invisible work happening; here's the
 *  surface to see it."
 *
 *  State management: a single integer (running count), updated
 *  three ways:
 *  - Mount: ``list_background_processes`` RPC seeds the count
 *    so processes that started before the FE attached show up.
 *    Catches the orphan-rehydration case — a BE restart's
 *    rehydrate path means there might be N orphans the FE has
 *    never been told about via push.
 *  - ``process_started`` push: increment.
 *  - ``process_exited`` push: decrement.
 *
 *  No polling. The two push channels are authoritative once
 *  the seed lands.
 */
export function WatcherIndicator({
  client,
  onOpen,
}: {
  client: EmberClient;
  onOpen: () => void;
}) {
  // We track live pids (not just a counter) so increment/decrement
  // are idempotent — a duplicate push doesn't drift the count, and
  // a "start" for a pid we already know (the seed RPC raced ahead
  // of the push) is a no-op.
  const [pids, setPids] = useState<Set<number>>(new Set());

  // ── Seed once on mount ───────────────────────────────────
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const initial = (await client.rpc("list_background_processes")) as Array<{
          pid: number;
        }>;
        if (cancelled) return;
        setPids((prev) => {
          // Merge with anything that already arrived via push
          // between mount and this RPC's response.
          const next = new Set(prev);
          for (const p of initial) {
            if (Number.isFinite(p.pid)) next.add(p.pid);
          }
          return next;
        });
      } catch {
        // Older BE without the RPC, or RPC errored — keep the
        // empty state. The push channels will populate as
        // processes spawn.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client]);

  // ── Push subscription ────────────────────────────────────
  useEffect(() => {
    return client.onEvent((m: ServerMessage) => {
      if (m.type !== "push_notification") return;
      if (m.channel === "process_started") {
        const pid = Number((m.payload as { pid?: number }).pid);
        if (!Number.isFinite(pid)) return;
        setPids((prev) => {
          if (prev.has(pid)) return prev;
          const next = new Set(prev);
          next.add(pid);
          return next;
        });
      } else if (m.channel === "process_exited") {
        const pid = Number((m.payload as { pid?: number }).pid);
        if (!Number.isFinite(pid)) return;
        setPids((prev) => {
          if (!prev.has(pid)) return prev;
          const next = new Set(prev);
          next.delete(pid);
          return next;
        });
      }
    });
  }, [client]);

  const count = pids.size;
  if (count === 0) return null;

  return (
    <button
      className="watcher-pill"
      title={`${count} background process${count === 1 ? "" : "es"} running — click to open the watcher`}
      onClick={onOpen}
      aria-label="Open watcher panel"
    >
      <span className="watcher-pill-dot" />
      {count} running
    </button>
  );
}
