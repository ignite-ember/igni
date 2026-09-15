import { useCallback, useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import type { IgniClient } from "../../protocol/client";
import { Drawer } from "./Drawer";
import { ChevronIcon } from "../Icons";

interface ScheduledTask {
  id?: string;
  description?: string;
  scheduled_at?: string;
  status?: string;
  recurrence?: string;
  result?: string;
  error?: string;
  created_at?: string;
}

const STATUS_TONE: Record<string, "ok" | "warn" | "bad" | "muted"> = {
  pending: "warn",
  running: "warn",
  completed: "ok",
  failed: "bad",
  cancelled: "muted",
};

function StatusPill({ status }: { status: string }) {
  const tone = STATUS_TONE[status] || "muted";
  return <span className={`task-status tone-${tone}`}>{status}</span>;
}

function TaskRow({
  task,
  onCancel,
}: {
  task: ScheduledTask;
  onCancel: () => void;
}) {
  const [open, setOpen] = useState(false);
  const hasDetails = Boolean(task.result || task.error);
  const status = task.status || "pending";
  const cancellable = status === "pending" || status === "running";
  return (
    <div className="task-entry">
      <div className="task-head" onClick={() => setOpen(!open)}>
        <span className="tool-chevron" style={{ display: "inline-flex" }}>
          <ChevronIcon size={10} down={open} />
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="name">
            {task.description || "(no description)"}
            <StatusPill status={status} />
            {task.recurrence && <span className="mini-tag">{task.recurrence}</span>}
          </div>
          <div className="meta">
            {task.scheduled_at &&
              `next: ${new Date(task.scheduled_at).toLocaleString([], {
                month: "short",
                day: "numeric",
                hour: "2-digit",
                minute: "2-digit",
              })}`}
            {task.id && <code className="task-id">{task.id}</code>}
          </div>
        </div>
        {cancellable && (
          <button
            className="btn btn-sm btn-danger"
            onClick={(e) => {
              e.stopPropagation();
              onCancel();
            }}
          >
            Cancel
          </button>
        )}
      </div>
      {open && (
        <div className="task-body">
          {task.error && (
            <div className="task-block task-block-error">
              <div className="task-block-label">Error</div>
              <pre>{task.error}</pre>
            </div>
          )}
          {task.result && (
            <div className="task-block">
              <div className="task-block-label">Result</div>
              <div className="agent-prompt-md">
                <ReactMarkdown>{task.result}</ReactMarkdown>
              </div>
            </div>
          )}
          {!hasDetails && (
            <div className="msg-info" style={{ padding: 0 }}>
              No output yet — this task hasn't run.
            </div>
          )}
        </div>
      )}
    </div>
  );
}

type FilterKey = "active" | "all" | "completed" | "failed" | "cancelled";

const FILTERS: { key: FilterKey; label: string; match: (s: string) => boolean }[] = [
  { key: "active", label: "Active", match: (s) => s === "pending" || s === "running" },
  { key: "all", label: "All", match: () => true },
  { key: "completed", label: "Done", match: (s) => s === "completed" },
  { key: "failed", label: "Failed", match: (s) => s === "failed" },
  { key: "cancelled", label: "Cancelled", match: (s) => s === "cancelled" },
];

export function SchedulePanel({
  client,
  onClose,
}: {
  client: IgniClient;
  onClose: () => void;
}) {
  const [tasks, setTasks] = useState<ScheduledTask[] | null>(null);
  const [filter, setFilter] = useState<FilterKey>("active");
  const [query, setQuery] = useState("");

  const refresh = useCallback(async () => {
    try {
      // include_done so users can read past results (completed,
      // failed, cancelled).
      setTasks(await client.rpc<ScheduledTask[]>("get_scheduled_tasks", { include_done: true }));
    } catch (e) {
      console.error(e);
      setTasks([]);
    }
  }, [client]);

  useEffect(() => {
    void refresh();
    const t = setInterval(refresh, 3_000);
    return () => clearInterval(t);
  }, [refresh]);

  const cancel = async (t: ScheduledTask) => {
    if (!t.id) return;
    await client.rpc("cancel_scheduled_task", { task_id: t.id });
    void refresh();
  };

  const counts: Record<FilterKey, number> = {
    active: 0,
    all: tasks?.length || 0,
    completed: 0,
    failed: 0,
    cancelled: 0,
  };
  for (const t of tasks || []) {
    const s = t.status || "";
    if (s === "pending" || s === "running") counts.active++;
    else if (s === "completed") counts.completed++;
    else if (s === "failed") counts.failed++;
    else if (s === "cancelled") counts.cancelled++;
  }

  const activeFilter = FILTERS.find((f) => f.key === filter) || FILTERS[0];
  const q = query.trim().toLowerCase();
  const visible = (tasks || [])
    .filter((t) => activeFilter.match(t.status || ""))
    .filter(
      (t) =>
        !q ||
        (t.description || "").toLowerCase().includes(q) ||
        (t.id || "").toLowerCase().includes(q),
    )
    .sort((a, b) => {
      // pending/running first, then by scheduled_at desc within each band.
      const live = (s?: string) => (s === "pending" || s === "running" ? 0 : 1);
      const liveDelta = live(a.status) - live(b.status);
      if (liveDelta) return liveDelta;
      return (b.scheduled_at || "").localeCompare(a.scheduled_at || "");
    });

  const hasTasks = !!tasks && tasks.length > 0;
  const toolbar = hasTasks && (
    <div className="task-filters">
      {FILTERS.map((f) => (
        <button
          key={f.key}
          className={`task-filter ${filter === f.key ? "active" : ""}`}
          onClick={() => setFilter(f.key)}
        >
          {f.label}
          <span className="task-filter-count">{counts[f.key]}</span>
        </button>
      ))}
    </div>
  );
  const headerSearch = hasTasks ? (
    <input
      className="task-search"
      type="search"
      placeholder="Search description or id…"
      value={query}
      onChange={(e) => setQuery(e.target.value)}
    />
  ) : undefined;

  return (
    <Drawer
      title="Scheduled tasks"
      headerExtras={headerSearch}
      toolbar={toolbar}
      onClose={onClose}
    >
      {tasks === null && <div className="msg-info">Loading…</div>}
      {tasks?.length === 0 && (
        <div className="msg-info">
          Nothing scheduled. Use <code>/schedule &lt;description&gt; every &lt;time&gt;</code>.
        </div>
      )}
      {tasks && tasks.length > 0 && visible.length === 0 && (
        <div className="msg-info">No tasks match.</div>
      )}
      {visible.map((t, i) => (
        <TaskRow key={t.id || i} task={t} onCancel={() => void cancel(t)} />
      ))}
    </Drawer>
  );
}
