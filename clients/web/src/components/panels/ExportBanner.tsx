import { useEffect, useRef } from "react";

/**
 * What happened to an export, said over the page rather than in it.
 *
 * It used to be a line of text at the top of the document body, which
 * put the result of an action in the place the user was reading and
 * then pushed the document down by a row. This floats: the page does
 * not reflow, and the one thing anybody wants next — opening the file
 * — is a button rather than a path to copy out by hand.
 */

export interface ExportOutcome {
  /** Where it landed. Empty when the export failed. */
  path: string;
  /** `html`, `md` or `pdf` — what was asked for, even on failure. */
  format: string;
  error: string;
}

const FORMAT_NAMES: Record<string, string> = {
  html: "HTML",
  md: "Markdown",
  pdf: "PDF",
};

/** The file's own name, which is what identifies it to a person. */
export function fileNameOf(path: string): string {
  const parts = String(path || "").split(/[/\\]/);
  return parts[parts.length - 1] || path;
}

/** The directory it landed in, for the line underneath. */
export function folderOf(path: string): string {
  const text = String(path || "");
  const cut = Math.max(text.lastIndexOf("/"), text.lastIndexOf("\\"));
  return cut > 0 ? text.slice(0, cut) : "";
}

export function ExportBanner({
  outcome,
  busy,
  onOpen,
  onDismiss,
}: {
  outcome: ExportOutcome;
  /** An open is in flight. */
  busy?: boolean;
  onOpen: () => void;
  onDismiss: () => void;
}) {
  const failed = !!outcome.error;
  const close = useRef<HTMLButtonElement>(null);

  // Esc dismisses. Not capture phase, and no `stopPropagation`: this
  // is a notice, not a layer — if it is gone and the page also goes
  // back a level, that is the ordering the user asked for by pressing
  // Escape twice as fast as it takes to read this.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onDismiss();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onDismiss]);

  const format = FORMAT_NAMES[outcome.format] || outcome.format.toUpperCase();

  return (
    <div
      className={failed ? "export-banner export-banner-failed" : "export-banner"}
      /* `status`, not `alert`: a finished export is information, and
         `alert` interrupts a screen reader mid-sentence to deliver it.
         A failure is still not urgent — the file simply is not there. */
      role="status"
      aria-live="polite"
    >
      <div className="export-banner-text">
        {failed ? (
          <>
            <strong>{format} export failed</strong>
            <span className="export-banner-detail">{outcome.error}</span>
          </>
        ) : (
          <>
            <strong>Exported as {format}</strong>
            {/* Filename on its own line, folder below it. A single
                absolute path wraps across three lines in this width
                and reads as an error message. */}
            <span className="export-banner-file">{fileNameOf(outcome.path)}</span>
            <span className="export-banner-detail" title={outcome.path}>
              {folderOf(outcome.path)}
            </span>
          </>
        )}
      </div>

      {!failed && (
        <button
          type="button"
          className="btn btn-sm btn-primary"
          disabled={busy}
          onClick={onOpen}
        >
          {busy ? "Opening…" : "Open"}
        </button>
      )}

      <button
        ref={close}
        type="button"
        className="export-banner-close"
        aria-label="Dismiss"
        title="Dismiss (Esc)"
        onClick={onDismiss}
      >
        ×
      </button>
    </div>
  );
}
