import { useContext, useEffect, type ReactNode } from "react";
import { CloseIcon } from "../Icons";
import { PageContext, PageShell } from "../PageShell";

/**
 * The frame a panel renders in — a modal, or a page.
 *
 * Which one is not the panel's business. Nine of the thirteen panels
 * that use this are destinations you browse (plugins, knowledge,
 * codeindex, …) and now render as pages with a breadcrumb trail; the
 * rest are dialogs and still render as this modal. The panels
 * themselves did not change: each one passes the same `title`,
 * `headerExtras` and `toolbar` it always did, and `PageContext` — set
 * by `App.tsx` around whatever the page stack says to render — decides
 * the presentation.
 *
 * Doing it here rather than editing nine components is what kept the
 * conversion honest: `PluginsPanel` is a thousand lines, and a
 * mechanical edit repeated nine times across bodies that all differ
 * slightly is how a wrapper swap turns into a rewrite.
 */
export function Drawer({
  title,
  headerExtras,
  toolbar,
  onClose,
  children,
}: {
  title: ReactNode;
  /** Slot rendered between the title and the close button — sized
   *  to the remaining horizontal space. Used for inline search,
   *  status pills, etc. */
  headerExtras?: ReactNode;
  /** Optional non-scrolling row docked under the header (filters,
   *  status counts). Sits outside the scrollable body so list
   *  items can never peek above it. */
  toolbar?: ReactNode;
  onClose: () => void;
  children: ReactNode;
}) {
  const page = useContext(PageContext);

  // The close button advertises "Close (Esc)" — honor it. Capture
  // phase + stopPropagation so the app-level Esc (cancel run) doesn't
  // also fire while a panel is open.
  //
  // Skipped entirely as a page: `PageShell` owns Esc there, where it
  // pops one level instead of closing outright. Two handlers on the
  // same key would pop and close on a single press.
  useEffect(() => {
    if (page) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      // A file preview (or any overlay marked .modal-overlay) sits
      // ON TOP of the drawer and owns Esc while it's open — don't
      // close the drawer underneath it.
      if (document.querySelector(".file-preview, .modal-overlay")) return;
      e.stopPropagation();
      onClose();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose, page]);

  if (page) {
    return (
      <PageShell
        trail={page.trail}
        title={title}
        headerExtras={headerExtras}
        toolbar={toolbar}
        onCrumb={page.onCrumb}
        onBack={page.onBack}
        onClose={page.onClose}
      >
        {children}
      </PageShell>
    );
  }

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside className="drawer">
        <div className="drawer-head">
          {title}
          {headerExtras ? (
            <div className="drawer-head-extras">{headerExtras}</div>
          ) : (
            <div className="header-spacer" />
          )}
          <button className="icon-btn" onClick={onClose} title="Close (Esc)">
            <CloseIcon />
          </button>
        </div>
        {toolbar && <div className="drawer-toolbar">{toolbar}</div>}
        <div className="drawer-body">{children}</div>
      </aside>
    </>
  );
}
