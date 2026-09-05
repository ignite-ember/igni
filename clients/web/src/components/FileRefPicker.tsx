import { useEffect, useRef, useState } from "react";
import type { IgniClient } from "../protocol/client";

/** Picks an existing project file via the BE's complete_files RPC.
 *  Returns a relative path the BE's @-mention handler can resolve;
 *  no upload, no content shipped over the wire. */
export function FileRefPicker({
  client,
  onPick,
  onCancel,
}: {
  client: IgniClient;
  onPick: (path: string) => void;
  onCancel: () => void;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<string[]>([]);
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const seq = useRef(0);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  useEffect(() => {
    const my = ++seq.current;
    client
      .completeFiles(query, 30)
      .then(({ matches }) => {
        if (my !== seq.current) return;
        setResults(matches);
        setActive(0);
      })
      .catch(() => {
        if (my === seq.current) setResults([]);
      });
  }, [client, query]);

  const choose = (path: string) => onPick(path);

  return (
    <>
      <div className="drawer-backdrop" onClick={onCancel} />
      <div className="file-ref-picker" role="dialog" aria-label="Attach file from project">
        <input
          ref={inputRef}
          className="file-ref-input"
          placeholder="Search files in this project…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setActive((i) => Math.min(results.length - 1, i + 1));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setActive((i) => Math.max(0, i - 1));
            } else if (e.key === "Enter" && results[active]) {
              e.preventDefault();
              choose(results[active]);
            } else if (e.key === "Escape") {
              e.preventDefault();
              onCancel();
            }
          }}
        />
        <div className="file-ref-list">
          {results.length === 0 && <div className="msg-info">No matches.</div>}
          {results.map((path, i) => (
            <div
              key={path}
              className={`file-ref-item ${i === active ? "active" : ""}`}
              onMouseEnter={() => setActive(i)}
              onMouseDown={(e) => {
                e.preventDefault();
                choose(path);
              }}
            >
              {path}
            </div>
          ))}
        </div>
      </div>
    </>
  );
}
