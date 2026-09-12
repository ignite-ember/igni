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
 * The frame a destination renders in.
 *
 * Deliberately the same prop surface as `panels/Drawer` — `title`,
 * `headerExtras`, `toolbar`, `children` — because nine panels convert
 * by swapping this in for that, and `PluginsPanel` alone is a thousand
 * lines. The wrapper changes; the bodies do not.
 *
 * What it adds over `Drawer` is the thing a single modal slot could
 * never have: a trail. The breadcrumbs come from the stack, so a panel
 * that walks collections into documents can say where it is and get
 * back one level, instead of being one box with a close button.
 *
 * What it drops is the backdrop and the fixed 620px. A page takes the
 * pane.
 */
export function PageShell({
  trail,
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
  /** A crumb was clicked: truncate to `index`. */
  onCrumb: (index: number) => void;
  /** Back one level. At the root this is the same as `onClose`. */
  onBack: () => void;
  /** Leave the pages and return to the chat. */
  onClose: () => void;
  children: ReactNode;
}) {
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
      onBack();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onBack]);

  const here = trail[trail.length - 1];

  return (
    <section className="page" aria-label={here?.label ?? "Page"}>
      <nav className="page-crumbs" aria-label="Breadcrumb">
        <button className="page-crumb" onClick={onClose}>
          Chat
        </button>
        {trail.map((route, i) => {
          const last = i === trail.length - 1;
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
                <button className="page-crumb" onClick={() => onCrumb(i)}>
                  {route.label}
                </button>
              )}
            </span>
          );
        })}
      </nav>

      <div className="page-head">
        <h2 className="page-title">{title ?? here?.label}</h2>
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
