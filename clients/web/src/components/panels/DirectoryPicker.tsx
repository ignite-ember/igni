import { useCallback, useEffect, useRef, useState } from "react";
import type { EmberClient } from "../../protocol/client";

interface DirListing {
  path: string;
  parent: string;
  dirs: string[];
  home: string;
  error: string;
}

/**
 * Server-side directory browser (the BE lists via the list_dirs
 * RPC — webviews can't touch the FS). Used to pick a project root
 * for a new session: navigate by clicking, jump via the editable
 * path field, confirm with "Use this folder".
 */
export function DirectoryPicker({
  client,
  title,
  onSelect,
  onCancel,
}: {
  client: EmberClient;
  title: string;
  onSelect: (path: string) => void;
  onCancel: () => void;
}) {
  const [listing, setListing] = useState<DirListing | null>(null);
  const [pathInput, setPathInput] = useState("");
  const [showHidden, setShowHidden] = useState(false);
  const [loading, setLoading] = useState(true);
  const seq = useRef(0);

  const load = useCallback(
    async (path: string, hidden = showHidden) => {
      const mySeq = ++seq.current;
      setLoading(true);
      try {
        const res = await client.rpc<DirListing>("list_dirs", {
          path,
          show_hidden: hidden,
        });
        if (mySeq !== seq.current) return; // stale navigation
        setListing(res);
        setPathInput(res.path);
      } catch (e) {
        if (mySeq !== seq.current) return;
        setListing((prev) => prev && { ...prev, error: String(e), dirs: [] });
      } finally {
        if (mySeq === seq.current) setLoading(false);
      }
    },
    [client, showHidden],
  );

  useEffect(() => {
    void load(""); // BE defaults to the home directory
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const sep = listing?.path.includes("\\") ? "\\" : "/";

  return (
    <div className="overlay" onClick={onCancel}>
      <div className="dialog dir-picker" onClick={(e) => e.stopPropagation()}>
        <div className="dialog-title">{title}</div>

        <div className="dir-pathbar">
          <button
            className="btn btn-sm"
            title="Home"
            onClick={() => listing && void load(listing.home)}
          >
            ⌂
          </button>
          <button
            className="btn btn-sm"
            title="Up one level"
            disabled={!listing?.parent}
            onClick={() => listing?.parent && void load(listing.parent)}
          >
            ↑
          </button>
          <input
            className="dir-path-input"
            value={pathInput}
            spellCheck={false}
            onChange={(e) => setPathInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void load(pathInput.trim());
            }}
          />
        </div>

        <div className="dir-list">
          {loading && <div className="msg-info">Loading…</div>}
          {!loading && listing?.error && (
            <div className="msg-error">{listing.error}</div>
          )}
          {!loading && !listing?.error && listing?.dirs.length === 0 && (
            <div className="msg-info">No subfolders.</div>
          )}
          {!loading &&
            listing?.dirs.map((name) => (
              <div
                key={name}
                className="popup-item"
                onClick={() => void load(`${listing.path}${sep}${name}`)}
              >
                <span className="dir-icon">▸</span>
                <span className="cmd">{name}</span>
              </div>
            ))}
        </div>

        <div className="dialog-actions" style={{ marginTop: 14 }}>
          <button className="btn btn-primary" disabled={!listing} onClick={() => listing && onSelect(listing.path)}>
            Use this folder
          </button>
          <label className="dir-hidden-toggle">
            <input
              type="checkbox"
              checked={showHidden}
              onChange={(e) => {
                setShowHidden(e.target.checked);
                if (listing) void load(listing.path, e.target.checked);
              }}
            />
            hidden folders
          </label>
          <div className="header-spacer" />
          <button className="btn" onClick={onCancel}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
