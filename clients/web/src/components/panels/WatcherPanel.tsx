/**
 * Right-side panel that surfaces every running backgrounded
 * shell process the agent has spawned via
 * ``run_shell_command(background=True)``. View-only tail with a
 * kill button per process.
 *
 * Real-time without polling. Three push channels feed it:
 *
 *   - ``process_started`` — new row appears.
 *   - ``process_line``    — appended to the selected pid's tail.
 *   - ``process_exited``  — row flips to "stopped" with exit code.
 *
 * One initial RPC (``list_background_processes``) seeds the
 * panel on open so processes that started BEFORE the panel was
 * mounted still show up. Selecting a row fires a single
 * ``read_process_tail`` to bootstrap the log pane; thereafter
 * the push channel keeps it current.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { IgniClient } from "../../protocol/client";
import type { ServerMessage } from "../../protocol/messages";
import { Drawer } from "./Drawer";

interface ProcessRow {
  pid: number;
  cmd: string;
  startedAt: number;      // epoch seconds when added
  isRunning: boolean;
  exitCode: number | null;
  /** Sticky last line for the summary list — saves a render
   *  for the full log pane when the user just wants an
   *  at-a-glance peek. */
  lastLine: string;
}

/** Per-process log buffer cap. The reader's own buffer trims to
 *  half when it grows past ~64KB; the FE matches that bound. A
 *  long-running `tail -f` over hours could otherwise eat the
 *  page's memory. */
const LOG_LINE_CAP = 2000;

export function WatcherPanel({
  client,
  onClose,
}: {
  client: IgniClient;
  onClose: () => void;
}) {
  const [rows, setRows] = useState<Map<number, ProcessRow>>(new Map());
  const [selectedPid, setSelectedPid] = useState<number | null>(null);
  const [logsByPid, setLogsByPid] = useState<Map<number, string[]>>(new Map());
  const [filter, setFilter] = useState("");
  const [autoScroll, setAutoScroll] = useState(true);
  const [now, setNow] = useState(() => Date.now() / 1000);
  // Mirror of ``rows`` for reads that must not re-run an effect.
  const rowsRef = useRef(rows);
  rowsRef.current = rows;

  // ── Seed on open ─────────────────────────────────────────
  // ``list_background_processes`` covers the gap between "BE
  // spawned a process before this panel mounted" and "panel
  // missed the start push". After this initial sync, the push
  // channel is sufficient.
  useEffect(() => {
    let cancelled = false;
    // Captured before the await: which pids this panel already knew
    // about when it asked. Anything that appears afterwards came from
    // a push and is newer than the answer.
    const knownAtFetch = new Set(rowsRef.current.keys());
    void (async () => {
      try {
        const initial = (await client.rpc("list_background_processes")) as Array<{
          pid: number;
          cmd: string;
          elapsed_seconds: number;
          // Added when the list learned to include finished processes.
          // Optional so an older BE, which only ever listed running
          // ones, still reads as "running" rather than as "stopped".
          is_running?: boolean;
          exit_code?: number | null;
        }>;
        if (cancelled) return;
        const nowTs = Date.now() / 1000;
        setRows((prev) => {
          const next = new Map(prev);
          for (const p of initial) {
            // Merge, don't skip. This used to be
            // ``if (!next.has(p.pid))``, so a row the panel already
            // held could never be corrected — and the case where it is
            // wrong is exactly the case where it matters: the BE that
            // would have sent ``process_exited`` was killed, so a
            // window open across a restart showed processes as running
            // forever, with a Kill button for something already dead.
            //
            // The BE is authoritative for the state. ``lastLine`` is
            // not — it accumulates here from the push channel and the
            // list RPC knows nothing about it.
            const existing = next.get(p.pid);
            next.set(p.pid, {
              pid: p.pid,
              cmd: p.cmd,
              startedAt: nowTs - p.elapsed_seconds,
              isRunning: p.is_running ?? true,
              exitCode: p.exit_code ?? null,
              lastLine: existing?.lastLine ?? "",
            });
          }
          // A row the panel held that the BE no longer knows about at
          // all — evicted past its retention, or spawned by a backend
          // from before any of this was recorded. It is certainly not
          // running: the process it described belonged to a BE that is
          // gone. Marked stopped with an unknown code rather than
          // deleted, so the window does not silently lose a line it
          // has been showing.
          //
          // Only pids the panel already held when this fetch started
          // are reconciled. One that arrived from a push while the RPC
          // was in flight is newer than the snapshot, not missing from
          // it.
          for (const pid of knownAtFetch) {
            const row = next.get(pid);
            if (!row || !row.isRunning) continue;
            if (initial.some((p) => p.pid === pid)) continue;
            next.set(pid, { ...row, isRunning: false, exitCode: null });
          }
          return next;
        });
      } catch (e) {
        console.warn("list_background_processes failed", e);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client]);

  // ── Push subscriptions ───────────────────────────────────
  useEffect(() => {
    const handler = (m: ServerMessage) => {
      if (m.type !== "push_notification") return;
      if (m.channel === "process_started") {
        const p = m.payload as { pid?: number; cmd?: string; started_at?: number };
        const pid = Number(p.pid);
        if (!Number.isFinite(pid)) return;
        const startedAt = Number(p.started_at) || Date.now() / 1000;
        setRows((prev) => {
          if (prev.has(pid)) return prev;
          const next = new Map(prev);
          next.set(pid, {
            pid,
            cmd: String(p.cmd ?? ""),
            startedAt,
            isRunning: true,
            exitCode: null,
            lastLine: "",
          });
          return next;
        });
      } else if (m.channel === "process_line") {
        const p = m.payload as { pid?: number; line?: string };
        const pid = Number(p.pid);
        const line = String(p.line ?? "");
        if (!Number.isFinite(pid)) return;
        // Buffer the line — even for un-selected pids, so
        // opening the row later shows recent activity
        // without an RPC call.
        setLogsByPid((prev) => {
          const next = new Map(prev);
          const lines = next.get(pid) ?? [];
          const appended = lines.length >= LOG_LINE_CAP
            ? [...lines.slice(LOG_LINE_CAP / 2), line]
            : [...lines, line];
          next.set(pid, appended);
          return next;
        });
        setRows((prev) => {
          const row = prev.get(pid);
          if (!row) return prev;
          const next = new Map(prev);
          next.set(pid, { ...row, lastLine: line });
          return next;
        });
      } else if (m.channel === "process_exited") {
        const p = m.payload as { pid?: number; exit_code?: number };
        const pid = Number(p.pid);
        if (!Number.isFinite(pid)) return;
        setRows((prev) => {
          const row = prev.get(pid);
          if (!row) return prev;
          const next = new Map(prev);
          next.set(pid, {
            ...row,
            isRunning: false,
            exitCode: typeof p.exit_code === "number" ? p.exit_code : null,
          });
          return next;
        });
      }
    };
    return client.onEvent(handler);
  }, [client]);

  // ── Elapsed-time ticker ──────────────────────────────────
  // Refresh ``now`` once a second so the per-row elapsed
  // counter advances. Cheap; only re-renders the (small) list.
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(t);
  }, []);

  // ── Selection / tail bootstrap ───────────────────────────
  // Click row → expand its tail. Click the SAME row again →
  // collapse back to the list-only view. The user asked for
  // this: once you're staring at a tail there's no way back
  // without an affordance, the watcher feels like a one-way
  // trip.
  //
  // On first select of a pid, fetch the existing tail so we
  // see history that landed before we subscribed. Subsequent
  // lines arrive via the push channel.
  const selectPid = useCallback(
    async (pid: number) => {
      if (pid === selectedPid) {
        setSelectedPid(null);
        return;
      }
      setSelectedPid(pid);
      setAutoScroll(true);
      // Skip the RPC if we already have a buffer (the push
      // channel was already feeding us).
      const existing = logsByPid.get(pid);
      if (existing && existing.length > 0) return;
      try {
        const tail = (await client.rpc("read_process_tail", { pid, tail: 500 })) as {
          output: string;
          is_running: boolean;
          exit_code: number | null;
        };
        const lines = tail.output ? tail.output.split("\n") : [];
        setLogsByPid((prev) => {
          // The push channel might have already started
          // delivering for this pid between our RPC and
          // the response — preserve any newer lines by
          // appending them AFTER the seed tail.
          const post = prev.get(pid) ?? [];
          const next = new Map(prev);
          next.set(pid, [...lines, ...post]);
          return next;
        });
        setRows((prev) => {
          const row = prev.get(pid);
          if (!row) return prev;
          const next = new Map(prev);
          next.set(pid, {
            ...row,
            isRunning: tail.is_running,
            exitCode: tail.exit_code,
          });
          return next;
        });
      } catch (e) {
        console.warn("read_process_tail failed", e);
      }
    },
    [client, logsByPid, selectedPid],
  );

  const killPid = useCallback(
    async (pid: number) => {
      try {
        await client.rpc("stop_background_process", { pid });
        // The BE will fire ``process_exited`` once the reader
        // task flushes; the push handler above flips the row.
      } catch (e) {
        console.warn("stop_background_process failed", e);
      }
    },
    [client],
  );

  // ── Derived: filtered + sorted rows ──────────────────────
  const rowsList = useMemo(() => {
    return Array.from(rows.values()).sort((a, b) => {
      // Running first, then most-recent start.
      if (a.isRunning !== b.isRunning) return a.isRunning ? -1 : 1;
      return b.startedAt - a.startedAt;
    });
  }, [rows]);

  const selectedRow = selectedPid !== null ? rows.get(selectedPid) ?? null : null;
  const selectedLogs = selectedPid !== null ? logsByPid.get(selectedPid) ?? [] : [];
  const filteredLogs = useMemo(() => {
    if (!filter.trim()) return selectedLogs;
    const f = filter.toLowerCase();
    return selectedLogs.filter((l) => l.toLowerCase().includes(f));
  }, [selectedLogs, filter]);

  // ── Auto-scroll on new lines ─────────────────────────────
  const logRef = useRef<HTMLPreElement>(null);
  useEffect(() => {
    if (!autoScroll) return;
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [filteredLogs, autoScroll]);

  // Detect manual scroll-up → pause auto-scroll; scrolling back
  // to within ~40px of bottom resumes it.
  const onLogScroll = useCallback(() => {
    const el = logRef.current;
    if (!el) return;
    const atBottom =
      el.scrollHeight - el.clientHeight - el.scrollTop < 40;
    setAutoScroll(atBottom);
  }, []);

  const runningCount = rowsList.filter((r) => r.isRunning).length;

  return (
    <Drawer
      // The selected pid is a level on the page's own trail, not a
      // second trail of its own. It used to render a private
      // ``.breadcrumb`` in the title, which put `Chat / Watcher` above
      // `Watcher › PID 8201` — the word "Watcher" twice, on two lines,
      // describing one position. `levels` is the seam the page shell
      // already exposes for exactly this; Plugins and Knowledge use it.
      levels={{
        labels: selectedRow ? [`PID ${selectedRow.pid}`] : [],
        // One level deep, so any truncation returns to the list.
        onTruncate: () => setSelectedPid(null),
      }}
      title={
        selectedRow ? (
          // Constant while drilled in: the trail carries the position,
          // so repeating it here would be the same duplication in a
          // different place.
          <span>Watcher</span>
        ) : (
          // The pill in the footer can show a count that the panel
          // doesn't agree with — ``list_background_processes`` is
          // sampled once on open, and pushes that landed between
          // the pill render and the panel render (process exited
          // mid-click) can leave us with "1 running" outside and
          // "0 running" inside. ``Watcher (0 running)`` reads as a
          // contradiction the user can't act on, so when nothing
          // is actually running we drop the count entirely.
          <span>
            Watcher{" "}
            <span className="watcher-count">
              {/* "No processes" has to mean an empty list. Once the
                  panel started keeping finished ones, zero *running*
                  stopped meaning zero rows, and the title sat above a
                  list of them saying there were none. */}
              {rowsList.length === 0
                ? "(no processes)"
                : runningCount === 0
                  ? `(${rowsList.length} stopped)`
                  : `(${runningCount} running${
                      rowsList.length > runningCount
                        ? `, ${rowsList.length - runningCount} stopped`
                        : ""
                    })`}
            </span>
          </span>
        )
      }
      onClose={onClose}
    >
      {rowsList.length === 0 ? (
        <div className="msg-info">
          Nothing has run here yet. Anything you start with{" "}
          <code>$ </code> — or that the agent backgrounds itself — shows up
          here while it runs, and stays afterwards with its exit code.
        </div>
      ) : (
        <div
          className={`watcher-layout${
            selectedRow ? " watcher-layout--split" : " watcher-layout--list-only"
          }`}
        >
          {/* Watching one process shows that process. The other rows
              are a way of getting here, not something to keep looking
              at while you read a log — and the trail above already
              says where "here" is and how to get back. */}
          <ul className="watcher-list">
            {(selectedRow ? [selectedRow] : rowsList).map((row) => (
              <li
                key={row.pid}
                className={`watcher-row${
                  row.pid === selectedPid ? " watcher-row--selected" : ""
                }${row.isRunning ? "" : " watcher-row--stopped"}`}
                onClick={() => void selectPid(row.pid)}
              >
                <div className="watcher-row-head">
                  <span
                    className={`watcher-status watcher-status--${
                      row.isRunning ? "running" : "stopped"
                    }`}
                    aria-hidden="true"
                  />
                  <span className="watcher-pid">PID {row.pid}</span>
                  <span className="watcher-elapsed">
                    {row.isRunning
                      ? formatElapsed(now - row.startedAt)
                      : `exit ${row.exitCode ?? "?"}`}
                  </span>
                  {row.isRunning ? (
                    <button
                      type="button"
                      className="btn btn-sm btn-danger"
                      onClick={(e) => {
                        e.stopPropagation();
                        void killPid(row.pid);
                      }}
                      title="Send SIGTERM"
                    >
                      Kill
                    </button>
                  ) : null}
                </div>
                <div className="watcher-cmd" title={row.cmd}>
                  {row.cmd}
                </div>
                {row.lastLine ? (
                  <div className="watcher-lastline" title={row.lastLine}>
                    {row.lastLine}
                  </div>
                ) : null}
              </li>
            ))}
          </ul>
          {selectedRow && (
          <div className="watcher-tail">
              <>
                <div className="watcher-tail-toolbar">
                  <input
                    type="text"
                    className="watcher-filter"
                    placeholder="Filter lines…"
                    value={filter}
                    onChange={(e) => setFilter(e.target.value)}
                  />
                  {!autoScroll && (
                    <button
                      type="button"
                      className="btn btn-sm"
                      onClick={() => {
                        setAutoScroll(true);
                        const el = logRef.current;
                        if (el) el.scrollTop = el.scrollHeight;
                      }}
                      title="Resume auto-scroll"
                    >
                      ↓ Resume
                    </button>
                  )}
                </div>
                <pre
                  ref={logRef}
                  className="watcher-log"
                  onScroll={onLogScroll}
                >
                  {filteredLogs.length === 0 ? (
                    <span className="watcher-log-empty">
                      {filter ? "No lines match the filter." : "No output yet."}
                    </span>
                  ) : (
                    filteredLogs.join("\n")
                  )}
                </pre>
              </>
          </div>
          )}
        </div>
      )}
    </Drawer>
  );
}

function formatElapsed(secs: number): string {
  if (!Number.isFinite(secs) || secs < 0) return "—";
  if (secs < 60) return `${Math.floor(secs)}s`;
  if (secs < 3600) {
    const m = Math.floor(secs / 60);
    const s = Math.floor(secs % 60);
    return `${m}m${s.toString().padStart(2, "0")}s`;
  }
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  return `${h}h${m.toString().padStart(2, "0")}m`;
}
