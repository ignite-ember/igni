import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type React from "react";
import { MarkdownInline } from "../MarkdownInline";
import type { IgniClient } from "../../protocol/client";

/**
 * Find a document.
 *
 * Two ways to look, in one box, because a person searching does not
 * know in advance which one they need:
 *
 * **Filter** runs on every keystroke against the list already loaded
 * — name, kind and preview. Instant, and right when you half-remember
 * a title.
 *
 * **Search** runs on Enter and goes to the store's vector index, which
 * matches on meaning rather than spelling. Right when you remember
 * what a document *said* but not what it was called.
 *
 * The distinction is stated in the UI rather than hidden behind a
 * mode toggle, because the two return different things and a user
 * who cannot tell which one ran cannot tell why a document is
 * missing.
 *
 * What the old results list got wrong, and why the fix matters: it
 * paired a hit back to a document by comparing names and sources,
 * and when that failed the row silently stopped being clickable. A
 * search result you cannot open is worse than no result — it looks
 * like the product found the thing and then refused to show it. Hits
 * now carry their `artifact_id`, so opening one is a lookup, not a
 * guess.
 */

export interface SearchHit {
  artifact_id: string;
  name: string;
  kind: string;
  n: number;
  content: string;
  score?: number | null;
}

export interface SearchableDoc {
  id: string;
  name: string;
  kind: string;
  n?: number;
  preview: string;
  added_at: string;
  size: number;
}

type Mode = "filter" | "search";

export function ArtifactSearch({
  client,
  docs,
  staleIds,
  actions,
  onOpen,
  onAsk,
}: {
  client: IgniClient;
  docs: SearchableDoc[];
  staleIds: Set<string>;
  /** What you can do to the collection — sync, comments, ask, add.
   *  On this bar rather than in a row of their own above it: the
   *  page already says it is the artifacts, and a second line
   *  repeating that pushed the only thing anybody came here to use
   *  down the page. */
  actions?: React.ReactNode;
  onOpen: (artifactId: string) => void;
  onAsk: (question: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState<string>("");
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [mode, setMode] = useState<Mode>("filter");
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Every kind actually present, so the chips describe this store
  // rather than a list of kinds somebody expected it to have.
  const kinds = useMemo(() => {
    const seen = new Map<string, number>();
    docs.forEach((d) => seen.set(d.kind || "note", (seen.get(d.kind || "note") ?? 0) + 1));
    return [...seen.entries()].sort((a, b) => b[1] - a[1]);
  }, [docs]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return docs.filter((d) => {
      if (kind && (d.kind || "note") !== kind) return false;
      if (!q) return true;
      return (
        d.name.toLowerCase().includes(q) ||
        (d.kind || "").toLowerCase().includes(q) ||
        (d.preview || "").toLowerCase().includes(q)
      );
    });
  }, [docs, query, kind]);

  const runSearch = useCallback(async () => {
    const q = query.trim();
    if (!q) return;
    setBusy(true);
    setMode("search");
    try {
      const results = await client.rpc<SearchHit[]>("artifact_search", { query: q });
      setHits(results ?? []);
    } catch {
      setHits([]);
    } finally {
      setBusy(false);
    }
  }, [client, query]);

  const backToFilter = useCallback(() => {
    setMode("filter");
    setHits(null);
  }, []);

  const showingSearch = mode === "search" && hits !== null;

  return (
    <div className="afs">
      <div className="afs-bar">
        <input
          ref={inputRef}
          className="afs-input"
          value={query}
          placeholder="Filter by name, or press Enter to search by meaning"
          onChange={(e) => {
            setQuery(e.target.value);
            if (mode === "search") backToFilter();
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") void runSearch();
            if (e.key === "Escape") {
              setQuery("");
              backToFilter();
            }
          }}
        />
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => void runSearch()}
          disabled={busy || !query.trim()}
          title="Search by meaning, not spelling"
        >
          {busy ? "Searching…" : "Search"}
        </button>
        {actions && <span className="afs-actions">{actions}</span>}
      </div>

      {kinds.length > 1 && !showingSearch && (
        <div className="afs-kinds">
          <button
            type="button"
            className={kind === "" ? "afs-kind afs-kind-on" : "afs-kind"}
            onClick={() => setKind("")}
          >
            All {docs.length}
          </button>
          {kinds.map(([k, n]) => (
            <button
              key={k}
              type="button"
              className={kind === k ? "afs-kind afs-kind-on" : "afs-kind"}
              onClick={() => setKind(kind === k ? "" : k)}
            >
              {k} {n}
            </button>
          ))}
        </div>
      )}

      {showingSearch && (
        <div className="afs-mode">
          <span>
            {hits.length} match{hits.length === 1 ? "" : "es"} by meaning for “{query}”
          </span>
          <button type="button" className="afs-link" onClick={backToFilter}>
            Back to the list
          </button>
        </div>
      )}

      {showingSearch ? (
        hits.length === 0 ? (
          <NoMatches query={query} onAsk={onAsk} />
        ) : (
          <ul className="afs-results">
            {hits.map((h) => (
              <Row
                key={h.artifact_id}
                id={h.artifact_id}
                name={h.name}
                kind={h.kind}
                version={h.n}
                snippet={h.content}
                stale={staleIds.has(h.artifact_id)}
                onOpen={onOpen}
              />
            ))}
          </ul>
        )
      ) : filtered.length === 0 ? (
        <NoMatches query={query} onAsk={onAsk} filtered />
      ) : (
        <ul className="afs-results">
          {filtered.map((d) => (
            <Row
              key={d.id}
              id={d.id}
              name={d.name}
              kind={d.kind}
              version={d.n ?? 0}
              snippet={d.preview}
              stale={staleIds.has(d.id)}
              onOpen={onOpen}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

function Row({
  id,
  name,
  kind,
  version,
  snippet,
  stale,
  onOpen,
}: {
  id: string;
  name: string;
  kind: string;
  version: number;
  snippet: string;
  stale: boolean;
  onOpen: (id: string) => void;
}) {
  return (
    <li>
      <button type="button" className="afs-row" onClick={() => onOpen(id)}>
        <span className="afs-row-head">
          <span className="afs-row-name">{name || id}</span>
          {kind && <span className="afs-row-kind">{kind}</span>}
          {version > 0 && <span className="afs-row-version">v{version}</span>}
          {stale && (
            <span className="afs-row-stale" title="A source has changed since this was written">
              out of date
            </span>
          )}
        </span>
        {/* Rendered, not raw. A preview full of ``##`` and ``**`` is
            the document's source, and nobody scanning a list is
            reading source. The name is passed so a document that
            opens with its own title does not spend both preview
            lines repeating the line above. */}
        <MarkdownInline text={snippet} name={name} className="afs-row-snippet" />
      </button>
    </li>
  );
}

/**
 * The dead end, with the way out of it.
 *
 * "No results" is where a search UI usually stops being useful. The
 * question a person has here is almost never "is there a document
 * called X" — it is something structural, and there is a surface for
 * that. Offering it costs one line and saves them finding it later.
 */
function NoMatches({
  query,
  onAsk,
  filtered = false,
}: {
  query: string;
  onAsk: (question: string) => void;
  filtered?: boolean;
}) {
  return (
    <div className="afs-empty">
      <p className="afs-empty-title">
        {query ? <>Nothing matches “{query}”.</> : <>No documents yet.</>}
      </p>
      {query && (
        <p className="afs-empty-hint">
          {filtered
            ? "Press Enter to search by meaning instead of by name."
            : "Nothing in the store is about this."}
        </p>
      )}
      {query && (
        <button type="button" className="btn btn-sm" onClick={() => onAsk(query)}>
          Ask the graph about it instead
        </button>
      )}
    </div>
  );
}
