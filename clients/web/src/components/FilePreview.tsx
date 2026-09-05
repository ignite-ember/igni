/**
 * FilePreview — plain-browser fallback for `host.openFile()`.
 *
 * Only ever instantiated when no native host bridge is available
 * (Tauri / VSCode / JetBrains route the open through their own
 * editor). For a regular browser there's nothing better we can do,
 * so we read the file via `read_file` and render it in a code block
 * with a path / copy / close affordance.
 */

import { useEffect, useState } from "react";
import type { IgniClient } from "../protocol/client";

interface ReadFileResp {
  path: string;
  contents: string;
  size: number;
  language?: string;
  error?: string;
}

export function FilePreview({
  client,
  path,
  onClose,
}: {
  client: IgniClient;
  path: string;
  onClose: () => void;
}) {
  const [data, setData] = useState<ReadFileResp | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setData(null);
    void client
      .rpc<ReadFileResp>("read_file", { path })
      .then((r) => {
        if (!cancelled) setData(r);
      })
      .catch((e) => {
        if (!cancelled) {
          setData({
            path,
            contents: "",
            size: 0,
            error: e instanceof Error ? e.message : String(e),
          });
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [client, path]);

  // Esc closes
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const displayPath = data?.path || path;
  const ext = displayPath.split("/").pop()?.split(".").pop() || "";

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside className="drawer file-preview">
        <div className="drawer-head">
          <code className="file-preview-path" title={displayPath}>
            {displayPath}
          </code>
          <button
            className="icon-btn"
            title="Copy path"
            onClick={() => void navigator.clipboard?.writeText(displayPath)}
          >
            <CopyIcon />
          </button>
          <button className="icon-btn" title="Close (Esc)" onClick={onClose}>
            <CloseIcon />
          </button>
        </div>
        <div className="drawer-body file-preview-body">
          {loading ? (
            <div className="msg-info">Loading…</div>
          ) : data?.error ? (
            <div className="msg-error">{data.error}</div>
          ) : (
            <pre className={`file-preview-pre lang-${data?.language || ext}`}>
              <code>{data?.contents || ""}</code>
            </pre>
          )}
        </div>
      </aside>
    </>
  );
}

function CopyIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor"
      strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="4" y="4" width="8.5" height="9.5" rx="1.5" />
      <path d="M3 11.5V3a1 1 0 0 1 1-1h7.5" />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor"
      strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 4l8 8M12 4l-8 8" />
    </svg>
  );
}
