import { useRef } from "react";
import { ScrollIndicator } from "./ScrollIndicator";
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
  const listRef = useRef<HTMLDivElement>(null);
  return (
    <>
      {open && window.innerWidth <= 700 && (
        <div
          style={{ position: "fixed", inset: 0, zIndex: 55, background: "rgba(0,0,0,0.35)" }}
          onClick={onClose}
        />
      )}
      <nav className={`sidebar ${open ? "" : "closed"}`}>
        {/* Solid strip sitting between the scrollable list (z:1)
            and the "Sessions" cluster (z:10) — hides items
            scrolling into the cluster's 118 px zone. */}
        <div className="sidebar-blur" aria-hidden="true" />
        {/* Floating top — head row, New-chat button, "Sessions" label.
            Pulled out of normal flow so the scrollable session list
            below extends up behind it. Mirrors the ``.app-header`` /
            ``.conversation`` overlap pattern. */}
        <div className="sidebar-top">
          <div className="sidebar-head">
            {/* App identity (flame + "igni") lives in the main
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
        </div>
        {/* Scroll-position overlay — same reason as the conversation:
            native scrollbar is hidden so the cluster blur doesn't
            frost it; this renders the thumb above the blur at z:30. */}
        <ScrollIndicator scrollRef={listRef} />
        <div className="sidebar-list" ref={listRef}>
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
