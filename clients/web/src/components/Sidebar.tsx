import { useEffect, useRef } from "react";
import { ScrollIndicator } from "./ScrollIndicator";
import { ThemeToggle } from "./ThemeToggle";
import { host } from "../lib/host";

export interface SessionEntry {
  session_id: string;
  name: string;
  detail?: string;
}

export interface SidebarLink {
  kind: string;
  label: string;
}

export function Sidebar({
  open,
  sessions,
  currentId,
  links,
  activeLink,
  onLink,
  onNewChat,
  onPick,
  onClose,
}: {
  open: boolean;
  sessions: SessionEntry[];
  currentId: string;
  /** Destinations that sit above the chats — plugins, knowledge,
   *  agents. Above the list because that is what they are: places,
   *  not conversations. */
  links: SidebarLink[];
  /** Which one is open, if any. */
  activeLink?: string;
  onLink: (kind: string) => void;
  onNewChat: () => void;
  onPick: (id: string) => void;
  onClose: () => void;
}) {
  const listRef = useRef<HTMLDivElement>(null);
  const topRef = useRef<HTMLDivElement>(null);

  // The floating cluster's height is also the list's top padding and
  // the blur strip's height — one measurement, three uses. It used to
  // be the literal 118 in two CSS rules that had to be changed
  // together; adding these links made it 230 and would have left both
  // stale, tucking the first session row up under the label.
  //
  // Measured rather than written down, so whatever the cluster grows
  // or loses next is accounted for without anyone remembering to.
  useEffect(() => {
    const el = topRef.current;
    if (!el) return;
    const apply = () =>
      el.parentElement?.style.setProperty("--sidebar-top-h", `${el.offsetHeight}px`);
    apply();
    const ro = new ResizeObserver(apply);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

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
        {/* Floating top — head row and the New-chat button. The
            section links and "Sessions" label sit in the scrollable
            list below instead. Pulled out of normal flow so the list
            extends up behind it. Mirrors the ``.app-header`` /
            ``.conversation`` overlap pattern. */}
        <div className="sidebar-top" ref={topRef}>
          <div className="sidebar-head">
            {/* App identity (flame + "igni") lives in the main
                column's ``.app-header`` — repeating it in the sidebar
                header makes the brand appear twice when the sidebar
                is open. Keep this row for the controls only (theme
                toggle today, settings later). The spacer pushes them
                to the right so the row stays visually anchored.
                JB owns the theme via ``LafManagerListener`` →
                ``ember:theme``, so we skip our own picker there —
                users switch light/dark through PyCharm/IntelliJ. */}
            <div className="sidebar-head-spacer" />
            {host.kind !== "jetbrains" && host.kind !== "vscode" && <ThemeToggle />}
          </div>
          <div style={{ padding: "6px 12px" }}>
            <button className="btn" style={{ width: "100%" }} onClick={onNewChat}>
              + New chat
            </button>
          </div>
        </div>
        {/* Scroll-position overlay — same reason as the conversation:
            native scrollbar is hidden so the cluster blur doesn't
            frost it; this renders the thumb above the blur at z:30. */}
        <ScrollIndicator scrollRef={listRef} />
        <div className="sidebar-list" ref={listRef}>
          {/* Links and the "Sessions" label scroll with the list
              rather than riding in the pinned cluster above. They are
              the head of one surface — destinations, then the chats
              under them — and pinning the label while the links
              scrolled past it would have read as two separate things. */}
          {links.length > 0 && (
            <nav className="sidebar-links" aria-label="Sections">
              {links.map((link) => (
                <button
                  key={link.kind}
                  className={`sidebar-link${activeLink === link.kind ? " active" : ""}`}
                  aria-current={activeLink === link.kind ? "page" : undefined}
                  onClick={() => onLink(link.kind)}
                >
                  {link.label}
                </button>
              ))}
            </nav>
          )}
          <div className="sidebar-section">Sessions</div>
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
