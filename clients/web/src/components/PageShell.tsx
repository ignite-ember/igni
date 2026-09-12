import { createContext, useEffect, type ReactNode } from "react";

import type { PageRoute } from "../chat/pageStack";

export interface PageContextValue {
  trail: readonly PageRoute[];
  onCrumb: (index: number) => void;
  onBack: () => void;
  onClose: () => void;
}

/**
 * Set when the subtree is being rendered as a page rather than a modal.
 *
 * `panels/Drawer` reads it and delegates here. That indirection is what
 * kept the conversion to one edit instead of nine: every panel keeps
 * its own `title`, `headerExtras` and `toolbar` — a thousand lines of
 * `PluginsPanel` included — and only the frame around them changes.
 */
export const PageContext = createContext<PageContextValue | null>(null);

/**
 * Depth the panel owns, handed up so the trail can show it.
 *
 * `labels` are the crumbs below the destination — `["acme-tools"]`
 * under Plugins. `onTruncate(i)` asks the panel to make level `i` the
 * deepest; `-1` means back to its own root.
 *
 * A prop rather than a context, because the panel renders the
 * `Drawer` that renders this shell — it is the shell's parent, so a
 * provider down here would be invisible to it. (It was, and driving
 * the real thing is what showed it: the title changed and the trail
 * did not.)
 */
export interface PanelLevels {
  labels: string[];
  onTruncate: (index: number) => void;
}

export function PageShell({
  trail,
  levels,
  title,
  headerExtras,
  toolbar,
  onCrumb,
  onBack,
  onClose,
  children,
}: {
  /** Breadcrumbs, root first. The last entry is this page. */
  trail: readonly PageRoute[];
  /** Falls back to the last crumb's label when not given. */
  title?: ReactNode;
  /** Between the title and the close button — inline search, status
   *  pills. Same slot `Drawer` offers. */
  headerExtras?: ReactNode;
  /** Non-scrolling row under the header, outside the scroll area so
   *  list items cannot peek above it. Same slot `Drawer` offers. */
  toolbar?: ReactNode;
  /** Depth inside the rendered panel, if it has any. */
  levels?: PanelLevels;
  /** A crumb was clicked: truncate to `index`. */
  onCrumb: (index: number) => void;
  /** Back one level. At the root this is the same as `onClose`. */
  onBack: () => void;
  /** Leave the pages and return to the chat. */
  onClose: () => void;
  children: ReactNode;
}) {
  // Depth the rendered panel owns — `KnowledgePanel` selecting a
  // document, `PluginsPanel` opening a plugin — so there is one trail
  // rather than the chrome's and the panel's stacked on top of it.
  const panelCrumbs = levels?.labels ?? [];

  // Back goes up one *crumb*, wherever that crumb came from: out of a
  // document before out of Knowledge. Without this, Esc inside a
  // document would leave the destination entirely and skip the level
  // the user was actually in.
  const back = () => {
    if (panelCrumbs.length) levels?.onTruncate(panelCrumbs.length - 2);
    else onBack();
  };

  // Esc pops one level rather than dumping you all the way out — with
  // a trail, "back" and "close" stopped being the same thing.
  //
  // Capture phase and `stopPropagation` for the reason `Drawer`
  // documents: the app-level Esc cancels the running turn, and it must
  // not also fire while a page is open. The overlay check is the same
  // deal — a file preview sits on top and owns Esc while it is there.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (document.querySelector(".file-preview, .modal-overlay")) return;
      e.stopPropagation();
      back();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
    // `back` closes over the current levels; re-bind when they change.
  }, [onBack, levels, panelCrumbs.length]);

  const here = trail[trail.length - 1];
  const leaf = panelCrumbs.length ? panelCrumbs[panelCrumbs.length - 1] : here?.label;

  return (
    <section className="page" aria-label={here?.label ?? "Page"}>
      <nav className="page-crumbs" aria-label="Breadcrumb">
        <button className="page-crumb" onClick={onClose}>
          Chat
        </button>
        {trail.map((route, i) => {
          // A destination is only the last crumb when the panel inside
          // it is at its own root. With panel depth below it, clicking
          // it means "back to the top of this destination", which only
          // the panel can do.
          const lastRoute = i === trail.length - 1;
          const last = lastRoute && !panelCrumbs.length;
          return (
            <span key={`${route.kind}-${i}`} className="page-crumb-group">
              <span className="page-crumb-sep" aria-hidden="true">
                /
              </span>
              {last ? (
                <span className="page-crumb current" aria-current="page">
                  {route.label}
                </span>
              ) : (
                <button
                  className="page-crumb"
                  onClick={() => (lastRoute ? levels?.onTruncate(-1) : onCrumb(i))}
                >
                  {route.label}
                </button>
              )}
            </span>
          );
        })}
        {panelCrumbs.map((label, i) => {
          const last = i === panelCrumbs.length - 1;
          return (
            <span key={`level-${i}`} className="page-crumb-group">
              <span className="page-crumb-sep" aria-hidden="true">
                /
              </span>
              {last ? (
                <span className="page-crumb current" aria-current="page">
                  {label}
                </span>
              ) : (
                <button className="page-crumb" onClick={() => levels?.onTruncate(i)}>
                  {label}
                </button>
              )}
            </span>
          );
        })}
      </nav>

      <div className="page-head">
        <h2 className="page-title">{title ?? leaf}</h2>
        {headerExtras ? (
          <div className="page-head-extras">{headerExtras}</div>
        ) : (
          <div className="header-spacer" />
        )}
      </div>

      {toolbar && <div className="page-toolbar">{toolbar}</div>}
      <div className="page-body">{children}</div>
    </section>
  );
}
