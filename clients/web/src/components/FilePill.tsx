import { useEffect, useRef, useState } from "react";
import { FileTypeIcon } from "./FileTypeIcon";
import { host } from "../lib/host";

/** Inline file reference rendered inside a user bubble. The same
 *  component is used whether the user attached via the `+` button
 *  or typed `@<path>` manually. Click → small action menu. */
export function FilePill({ path }: { path: string }) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const wrapRef = useRef<HTMLSpanElement>(null);
  const name = path.split("/").pop() || path;

  useEffect(() => {
    if (!menuOpen) return;
    const onDown = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setMenuOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenuOpen(false);
    };
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(path);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1300);
    } catch {
      /* clipboard blocked */
    }
  };

  return (
    <span className="file-pill-wrap" ref={wrapRef}>
      <button
        type="button"
        className="file-pill"
        title={path}
        onClick={(e) => {
          e.stopPropagation();
          setMenuOpen((v) => !v);
        }}
      >
        <span className="file-pill-icon">
          <FileTypeIcon name={name} size={14} />
        </span>
        <span className="file-pill-name">{name}</span>
      </button>
      {menuOpen && (
        <span className="file-pill-menu" role="menu">
          <span className="file-pill-menu-path">{path}</span>
          <button
            type="button"
            className="file-pill-menu-item"
            onClick={() => {
              void copy();
              setMenuOpen(false);
            }}
          >
            {copied ? "Copied!" : "Copy path"}
          </button>
          <button
            type="button"
            className="file-pill-menu-item"
            onClick={() => {
              setMenuOpen(false);
              // Route through the host bridge — Tauri opens it via
              // the OS shell, a browser tab in the preview.
              void host.openFile(path);
            }}
          >
            {host.kind === "web" ? "Preview" : "Open"}
          </button>
        </span>
      )}
    </span>
  );
}
