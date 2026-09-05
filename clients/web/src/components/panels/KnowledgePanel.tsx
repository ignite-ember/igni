import { useCallback, useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import type { IgniClient } from "../../protocol/client";
import { Drawer } from "./Drawer";
import { Skeleton } from "../Skeleton";

// ── Types ────────────────────────────────────────────────────────

interface KnowledgeStatus {
  enabled: boolean;
  collection_name: string;
  document_count: number;
  embedder: string;
}

interface KnowledgeDoc {
  id: string;
  name: string;
  source: string;
  size: number;
  preview: string;
  added_at: string;
  kind: string;
  metadata: Record<string, string>;
}

interface KnowledgeDocFull {
  id: string;
  name: string;
  source: string;
  content: string;
  metadata: Record<string, string>;
  error?: string;
}

interface KnowledgeSearchHit {
  name?: string;
  content?: string;
  source?: string;
  score?: number;
  metadata?: Record<string, string>;
}

type View = "list" | "detail" | "add";
type AddMode = "url" | "path" | "text";

// ── Helpers ──────────────────────────────────────────────────────

function formatBytes(n: number): string {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function formatRelative(iso: string): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return iso;
  const secs = Math.floor((Date.now() - t) / 1000);
  if (secs < 60) return "just now";
  const m = Math.floor(secs / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}d ago`;
  const mo = Math.floor(d / 30);
  return `${mo}mo ago`;
}

// ── Main panel ───────────────────────────────────────────────────

export function KnowledgePanel({
  client,
  onClose,
}: {
  client: IgniClient;
  onClose: () => void;
}) {
  const [status, setStatus] = useState<KnowledgeStatus | null>(null);
  const [docs, setDocs] = useState<KnowledgeDoc[] | null>(null);
  const [view, setView] = useState<View>("list");
  const [selected, setSelected] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [searched, setSearched] = useState<{ q: string; results: KnowledgeSearchHit[] } | null>(null);
  const [busy, setBusy] = useState("");
  const [addError, setAddError] = useState("");
  const [confirmingRemove, setConfirmingRemove] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setStatus(await client.rpc<KnowledgeStatus>("get_knowledge_status"));
    } catch (e) {
      console.error(e);
    }
    try {
      setDocs((await client.rpc<KnowledgeDoc[]>("knowledge_list")) || []);
    } catch (e) {
      console.error(e);
      setDocs([]);
    }
  }, [client]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (view !== "detail") setConfirmingRemove(false);
  }, [view, selected]);

  // Empty query = filtered list, Enter = semantic search
  const filteredDocs = useMemo(() => {
    if (!docs) return [];
    const q = query.trim().toLowerCase();
    if (!q) return docs;
    return docs.filter(
      (d) =>
        d.name.toLowerCase().includes(q) ||
        (d.source || "").toLowerCase().includes(q) ||
        (d.preview || "").toLowerCase().includes(q),
    );
  }, [docs, query]);

  const runSearch = async () => {
    const q = query.trim();
    if (!q) return;
    setBusy("search");
    try {
      const results =
        (await client.rpc<KnowledgeSearchHit[]>("knowledge_search", { query: q })) || [];
      setSearched({ q, results });
    } catch (e) {
      setSearched({ q, results: [{ content: String(e) }] });
    } finally {
      setBusy("");
    }
  };

  const clearSearch = () => {
    setSearched(null);
    setQuery("");
  };

  const sync = async () => {
    setBusy("sync");
    try {
      await client.rpc("auto_sync_knowledge");
      void refresh();
    } finally {
      setBusy("");
    }
  };

  const remove = async (id: string) => {
    setBusy("remove");
    try {
      const r = await client.rpc<{ removed: boolean }>("knowledge_remove", { id });
      if (r.removed) {
        setSelected(null);
        setConfirmingRemove(false);
        setView("list");
        void refresh();
      }
    } finally {
      setBusy("");
    }
  };

  const tryAdd = async (source: string): Promise<boolean> => {
    if (!source.trim()) return false;
    setBusy("add");
    setAddError("");
    try {
      await client.rpc("knowledge_add", { source: source.trim() });
      void refresh();
      setView("list");
      return true;
    } catch (e) {
      setAddError(e instanceof Error ? e.message : String(e));
      return false;
    } finally {
      setBusy("");
    }
  };

  const selectedDoc = selected && docs ? docs.find((d) => d.id === selected) : null;

  // ── Header ──────────────────────────────────────────────────────
  let title: React.ReactNode = "Knowledge";
  if (view === "detail" && selectedDoc) {
    title = (
      <span className="breadcrumb" style={{ margin: 0 }}>
        <button
          className="breadcrumb-link"
          onClick={() => {
            setView("list");
            setSelected(null);
          }}
        >
          Knowledge
        </button>
        <span className="breadcrumb-sep">›</span>
        <strong>{selectedDoc.name}</strong>
      </span>
    );
  } else if (view === "add") {
    title = (
      <span className="breadcrumb" style={{ margin: 0 }}>
        <button className="breadcrumb-link" onClick={() => setView("list")}>
          Knowledge
        </button>
        <span className="breadcrumb-sep">›</span>
        <strong>Add source</strong>
      </span>
    );
  }

  // ── Header extras: per-view actions ─────────────────────────────
  let headerExtras: React.ReactNode = null;
  if (view === "list" && status) {
    headerExtras = (
      <div className="kb-head-actions">
        <button
          className="kb-icon-btn"
          title={busy === "sync" ? "Syncing…" : "Sync git-shared knowledge"}
          disabled={busy === "sync" || !status.enabled}
          onClick={() => void sync()}
        >
          <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">
            <path
              d="M2.5 8a5.5 5.5 0 0 1 9.4-3.9M13.5 8a5.5 5.5 0 0 1-9.4 3.9"
              stroke="currentColor"
              strokeWidth="1.3"
              strokeLinecap="round"
            />
            <path
              d="M12 1.5V5h-3.5M4 14.5V11h3.5"
              stroke="currentColor"
              strokeWidth="1.3"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </button>
        <button
          className="btn btn-sm btn-primary"
          disabled={!status.enabled}
          onClick={() => {
            setAddError("");
            setView("add");
          }}
        >
          + Add
        </button>
      </div>
    );
  } else if (view === "detail" && selected) {
    headerExtras = (
      <div className="kb-head-actions">
        {confirmingRemove ? (
          <>
            <span className="kb-hint">Remove this document?</span>
            <button
              className="btn btn-sm"
              onClick={() => setConfirmingRemove(false)}
              disabled={busy === "remove"}
            >
              Cancel
            </button>
            <button
              className="btn btn-sm btn-danger"
              disabled={busy === "remove"}
              onClick={() => void remove(selected)}
            >
              {busy === "remove" ? "…" : "Confirm"}
            </button>
          </>
        ) : (
          <button
            className="btn btn-sm btn-danger"
            onClick={() => setConfirmingRemove(true)}
          >
            Remove
          </button>
        )}
      </div>
    );
  }

  // ── Toolbar: combined filter + search ───────────────────────────
  const drawerToolbar =
    view === "list" && status && status.enabled ? (
      <div className="kb-toolbar">
        {searched ? (
          <div className="kb-search-chip">
            <span className="kb-search-chip-q">"{searched.q}"</span>
            <button className="kb-chip-close" onClick={clearSearch} title="Clear search">
              ×
            </button>
          </div>
        ) : (
          <input
            className="panel-input"
            placeholder="Filter documents, or press Enter to search content…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void runSearch();
              if (e.key === "Escape") clearSearch();
            }}
            autoFocus
          />
        )}
      </div>
    ) : null;

  return (
    <Drawer
      title={title}
      onClose={onClose}
      headerExtras={headerExtras}
      toolbar={drawerToolbar}
    >
      {status === null ? (
        <KnowledgeSkeleton />
      ) : view === "add" ? (
        <AddSource
          busy={busy === "add"}
          error={addError}
          onAdd={tryAdd}
          onCancel={() => setView("list")}
        />
      ) : view === "detail" && selected ? (
        <DocumentDetail client={client} id={selected} />
      ) : !status.enabled ? (
        <DisabledState />
      ) : searched ? (
        <SearchResults
          query={searched.q}
          results={searched.results}
          docs={docs || []}
          onOpen={(hit) => {
            if (!docs) return;
            const match = docs.find(
              (d) => d.source === (hit.source || hit.name) || d.id === hit.name,
            );
            if (match) {
              setSelected(match.id);
              setView("detail");
            }
          }}
        />
      ) : docs && docs.length === 0 ? (
        <EmptyHero
          onAdd={() => setView("add")}
          embedder={status.embedder}
        />
      ) : (
        <DocumentList
          docs={filteredDocs}
          totalDocs={docs?.length || 0}
          query={query}
          onOpen={(d) => {
            setSelected(d.id);
            setView("detail");
          }}
        />
      )}
    </Drawer>
  );
}

// ── Disabled state ───────────────────────────────────────────────

function DisabledState() {
  return (
    <div className="kb-empty">
      <div className="kb-empty-title">Knowledge base disabled</div>
      <div className="kb-empty-hint">
        Configure an embedder under <code>knowledge.enabled = true</code> in settings to
        enable semantic search.
      </div>
    </div>
  );
}

// ── Empty hero ───────────────────────────────────────────────────

function EmptyHero({
  onAdd,
  embedder,
}: {
  onAdd: () => void;
  embedder: string;
}) {
  return (
    <div className="kb-empty">
      <div className="kb-empty-icon">
        <svg viewBox="0 0 48 48" width="48" height="48" fill="none">
          <path
            d="M10 14h28v22a4 4 0 0 1-4 4H14a4 4 0 0 1-4-4V14z"
            stroke="currentColor"
            strokeWidth="1.6"
            opacity="0.6"
          />
          <path
            d="M14 8h20l4 6H10z"
            stroke="currentColor"
            strokeWidth="1.6"
            opacity="0.4"
          />
          <path
            d="M24 22v12M18 28h12"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
          />
        </svg>
      </div>
      <div className="kb-empty-title">Nothing in the knowledge base yet</div>
      <div className="kb-empty-hint">
        Add a URL, file, or paste text — the agent will use it when answering questions.
      </div>
      <button className="btn btn-primary kb-empty-cta" onClick={onAdd}>
        + Add source
      </button>
      {embedder && (
        <div className="kb-empty-foot">Indexed with <code>{embedder}</code></div>
      )}
    </div>
  );
}

// ── Document list ────────────────────────────────────────────────

function DocumentList({
  docs,
  totalDocs,
  query,
  onOpen,
}: {
  docs: KnowledgeDoc[];
  totalDocs: number;
  query: string;
  onOpen: (d: KnowledgeDoc) => void;
}) {
  if (totalDocs > 0 && docs.length === 0) {
    return <div className="kb-empty-hint kb-empty-inline">No documents match "{query}".</div>;
  }
  return (
    <div className="kb-list">
      {docs.map((d) => (
        <div className="kb-row plugins-card-clickable" key={d.id} onClick={() => onOpen(d)}>
          <div className="kb-row-main">
            <div className="kb-row-name" title={d.source || d.name}>
              {d.name}
            </div>
            {d.preview && <div className="kb-row-preview">{d.preview}</div>}
          </div>
          <div className="kb-row-meta">
            <span>{formatBytes(d.size)}</span>
            {d.added_at && <span>{formatRelative(d.added_at)}</span>}
            {d.kind && <span className="kb-kind">{d.kind}</span>}
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Search results ───────────────────────────────────────────────

function SearchResults({
  query,
  results,
  docs,
  onOpen,
}: {
  query: string;
  results: KnowledgeSearchHit[];
  docs: KnowledgeDoc[];
  onOpen: (hit: KnowledgeSearchHit) => void;
}) {
  if (results.length === 0) {
    return (
      <div className="kb-empty kb-empty-inline">
        <div className="kb-empty-title">No matches for "{query}"</div>
        <div className="kb-empty-hint">
          Try different phrasing, or add more sources to broaden the index.
        </div>
      </div>
    );
  }
  return (
    <div className="kb-list">
      {results.map((r, i) => {
        const docMatch = docs.find(
          (d) => d.source === (r.source || r.name) || d.id === r.name,
        );
        const score = typeof r.score === "number" ? r.score : null;
        const name = r.name || r.source || "untitled";
        const preview = (r.content || "").slice(0, 240);
        return (
          <div
            key={i}
            className={`kb-row ${docMatch ? "plugins-card-clickable" : ""}`}
            onClick={() => docMatch && onOpen(r)}
          >
            <div className="kb-row-main">
              <div className="kb-row-name">{name}</div>
              {preview && <div className="kb-row-preview">{preview}</div>}
            </div>
            <div className="kb-row-meta">
              {score != null && (
                <span
                  className={`kb-score ${
                    score >= 0.6 ? "tone-good" : score >= 0.3 ? "tone-warn" : "tone-muted"
                  }`}
                >
                  {(score * 100).toFixed(0)}%
                </span>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ── Document detail page ─────────────────────────────────────────

function DocumentDetail({
  client,
  id,
}: {
  client: IgniClient;
  id: string;
}) {
  const [doc, setDoc] = useState<KnowledgeDocFull | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setDoc(null);
    void client
      .rpc<KnowledgeDocFull>("knowledge_get", { id })
      .then((r) => {
        if (!cancelled) setDoc(r);
      })
      .catch((e) => {
        if (!cancelled) {
          setDoc({
            id,
            name: id,
            source: "",
            content: "",
            metadata: {},
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
  }, [client, id]);

  if (loading || !doc) {
    return (
      <div style={{ marginTop: 12 }}>
        <Skeleton.Line width="40%" height={20} />
        <Skeleton.Line width="60%" height={11} style={{ marginTop: 8 }} />
        <Skeleton.Rows count={6} style={{ marginTop: 18 }} />
      </div>
    );
  }
  if (doc.error) {
    return <div className="msg-error">{doc.error}</div>;
  }

  return (
    <div className="kb-detail">
      <div className="kb-detail-hero">
        <h2 className="kb-detail-title">{doc.name}</h2>
        {doc.source && doc.source !== doc.name && (
          <div className="kb-detail-source">
            {/^https?:\/\//.test(doc.source) ? (
              <a href={doc.source} target="_blank" rel="noreferrer">
                {doc.source}
              </a>
            ) : (
              <code>{doc.source}</code>
            )}
          </div>
        )}
      </div>

      {Object.keys(doc.metadata).length > 0 && (
        <div className="kb-detail-meta">
          {Object.entries(doc.metadata).map(([k, v]) => (
            <span key={k} className="kb-meta-chip">
              <span className="kb-meta-key">{k}</span>
              <span className="kb-meta-val">{v}</span>
            </span>
          ))}
        </div>
      )}

      <div className="kb-detail-content markdown-body">
        <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
          {doc.content || "_(empty)_"}
        </ReactMarkdown>
      </div>
    </div>
  );
}

// ── Add source page ──────────────────────────────────────────────

function AddSource({
  busy,
  error,
  onAdd,
  onCancel,
}: {
  busy: boolean;
  error: string;
  onAdd: (s: string) => Promise<boolean>;
  onCancel: () => void;
}) {
  const [mode, setMode] = useState<AddMode>("url");
  const [value, setValue] = useState("");

  const submit = async () => {
    if (!value.trim()) return;
    await onAdd(value.trim());
  };

  return (
    <div className="kb-add-page">
      <div className="kb-add-modes">
        {(["url", "path", "text"] as AddMode[]).map((m) => (
          <button
            key={m}
            type="button"
            className={`kb-mode ${mode === m ? "active" : ""}`}
            onClick={() => setMode(m)}
          >
            {m === "url" ? "URL" : m === "path" ? "File / path" : "Text"}
          </button>
        ))}
      </div>
      <div className="kb-add-hint">
        {mode === "url" && "Fetch and ingest a web page. Markdown, blog posts, docs."}
        {mode === "path" && "Read a local file or recursively walk a directory."}
        {mode === "text" && "Paste any text — notes, snippets, conversation excerpts."}
      </div>
      {mode === "text" ? (
        <textarea
          className="panel-input kb-add-textarea"
          placeholder="Paste here…"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          rows={8}
          autoFocus
        />
      ) : (
        <input
          className="panel-input"
          placeholder={
            mode === "url"
              ? "https://example.com/docs/page"
              : "./docs/architecture.md or /abs/path"
          }
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && void submit()}
          autoFocus
        />
      )}
      {error && <div className="msg-error" style={{ marginTop: 10 }}>{error}</div>}
      <div className="kb-add-bottom">
        <button className="btn btn-sm" onClick={onCancel}>
          Cancel
        </button>
        <button
          className="btn btn-sm btn-primary"
          disabled={busy || !value.trim()}
          onClick={() => void submit()}
        >
          {busy ? "Adding…" : "Add to knowledge"}
        </button>
      </div>
    </div>
  );
}

function KnowledgeSkeleton() {
  return (
    <div className="kb-list">
      {Array.from({ length: 5 }).map((_, i) => (
        <div key={i} className="kb-row">
          <div className="kb-row-main">
            <Skeleton.Line width="40%" height={13} />
            <Skeleton.Line width="80%" height={11} style={{ marginTop: 6 }} />
          </div>
          <Skeleton.Line width={36} height={11} />
        </div>
      ))}
    </div>
  );
}
