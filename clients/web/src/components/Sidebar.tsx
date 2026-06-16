import { ThemeToggle } from "./ThemeToggle";

export interface SessionEntry {
  session_id: string;
  name: string;
  detail?: string;
}

export function Sidebar({
  open,
  sessions,
  currentId,
  onNewChat,
  onPick,
  onClose,
}: {
  open: boolean;
  sessions: SessionEntry[];
  currentId: string;
  onNewChat: () => void;
  onPick: (id: string) => void;
  onClose: () => void;
}) {
  return (
    <>
      {open && window.innerWidth <= 700 && (
        <div
          style={{ position: "fixed", inset: 0, zIndex: 55, background: "rgba(0,0,0,0.35)" }}
          onClick={onClose}
        />
      )}
      <nav className={`sidebar ${open ? "" : "closed"}`}>
        <div className="sidebar-head">
          {/* App identity (flame + "Ember Code") lives in the main
              column's ``.app-header`` — repeating it in the sidebar
              header makes the brand appear twice when the sidebar
              is open. Keep this row for the controls only (theme
              toggle today, settings later). The spacer pushes them
              to the right so the row stays visually anchored. */}
          <div className="sidebar-head-spacer" />
          <ThemeToggle />
        </div>
        <div style={{ padding: "6px 12px" }}>
          <button className="btn" style={{ width: "100%" }} onClick={onNewChat}>
            + New chat
          </button>
        </div>
        <div className="sidebar-section">Sessions</div>
        <div className="sidebar-list">
          {sessions.length === 0 && (
            <div className="session-item" style={{ cursor: "default" }}>
              No past sessions
            </div>
          )}
          {sessions.map((s) => (
            <div
              key={s.session_id}
              className={`session-item ${s.session_id === currentId ? "current" : ""}`}
              title={s.detail || s.name}
              onClick={() => onPick(s.session_id)}
            >
              <span className="session-name">{s.name || s.session_id}</span>
              <span className="session-id">{s.session_id.slice(0, 8)}</span>
            </div>
          ))}
        </div>
      </nav>
    </>
  );
}
