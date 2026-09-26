import { useCallback, useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import type { IgniClient } from "../../protocol/client";
import { ArtifactGraphSearch } from "./ArtifactGraphSearch";
import { ArtifactSearch } from "./ArtifactSearch";
import { ArtifactShareDialog } from "./ArtifactShareDialog";
import { ArtifactStaleReview } from "./ArtifactStaleReview";
import { Drawer } from "./Drawer";
import { Skeleton } from "../Skeleton";

// ── Types ────────────────────────────────────────────────────────

interface ArtifactStatus {
  enabled: boolean;
  collection_name: string;
  document_count: number;
  version_count?: number;
  stale_count?: number;
  pending_candidates?: number;
  embedder: string;
}

interface ArtifactsDoc {
  id: string;
  name: string;
  source: string;
  kind: string;
  n?: number;
  version_count?: number;
  size: number;
  preview: string;
  added_at: string;
  metadata: Record<string, string>;
}

interface ArtifactsDocFull {
  id: string;
  name: string;
  source: string;
  kind?: string;
  n?: number;
  head_n?: number;
  is_head?: boolean;
  content: string;
  created_at?: string;
  author?: string;
  metadata: Record<string, string>;
  error?: string;
}

interface ArtifactVersionRow {
  n: number;
  created_at: string;
  author: string;
  size: number;
  is_head: boolean;
}

/**
 * One derivation edge. `pinned_n` and `head_n` both arrive so the row
 * can say "pinned to v1, now at v3" without a second request — that
 * comparison is the only question a reader has about a source.
 */
interface ArtifactLineageRow {
  artifact_id: string;
  name: string;
  n: number;
  pinned_n: number;
  head_n: number;
  state: string;
  is_stale: boolean;
}

interface ArtifactLineageView {
  artifact_id: string;
  n: number;
  sources: ArtifactLineageRow[];
  dependents: ArtifactLineageRow[];
  citations: string[];
}

type View = "list" | "detail" | "add" | "ask" | "stale" | "share";
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

export function ArtifactPanel({
  client,
  onClose,
}: {
  client: IgniClient;
  onClose: () => void;
}) {
  const [status, setStatus] = useState<ArtifactStatus | null>(null);
  const [docs, setDocs] = useState<ArtifactsDoc[] | null>(null);
  const [view, setView] = useState<View>("list");
  const [selected, setSelected] = useState<string | null>(null);
  const [busy, setBusy] = useState("");
  const [addError, setAddError] = useState("");
  const [confirmingRemove, setConfirmingRemove] = useState(false);
  const [notice, setNotice] = useState("");
  // Which documents have a source that moved. Fetched once with the
  // list so a search result can say "out of date" without a request
  // per row.
  const [staleIds, setStaleIds] = useState<Set<string>>(new Set());
  // A question handed over from a search that found nothing. The ask
  // view reads it once, so going back does not re-run it.
  const [pendingQuestion, setPendingQuestion] = useState("");

  const refresh = useCallback(async () => {
    try {
      setStatus(await client.rpc<ArtifactStatus>("get_artifact_status"));
    } catch (e) {
      console.error(e);
    }
    try {
      setDocs((await client.rpc<ArtifactsDoc[]>("artifact_list")) || []);
    } catch (e) {
      console.error(e);
      setDocs([]);
    }
    try {
      const rows = (await client.rpc<{ artifact_id: string }[]>("artifact_stale")) || [];
      setStaleIds(new Set(rows.map((r) => r.artifact_id)));
    } catch {
      // A failed staleness sweep costs a badge, not the list.
      setStaleIds(new Set());
    }
  }, [client]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (view !== "detail") setConfirmingRemove(false);
  }, [view, selected]);

  const sync = async () => {
    setBusy("sync");
    try {
      await client.rpc("auto_sync_artifacts");
      void refresh();
    } finally {
      setBusy("");
    }
  };

  const remove = async (id: string) => {
    setBusy("remove");
    try {
      const r = await client.rpc<{ removed: boolean }>("artifact_remove", { id });
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

  const exportDoc = async (id: string) => {
    setBusy("export");
    try {
      const r = await client.rpc<{ path: string; error: string }>("artifact_export", {
        id,
      });
      // Where the file landed is the whole result of the action, and
      // a silent success leaves the user hunting for it.
      setNotice(r.error ? r.error : `Exported to ${r.path}`);
    } catch (e) {
      setNotice(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  };

  const tryAdd = async (source: string): Promise<boolean> => {
    if (!source.trim()) return false;
    setBusy("add");
    setAddError("");
    try {
      await client.rpc("artifact_add", { source: source.trim() });
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
  // The document and the add-source form are one level below the list.
  // Reported to the page trail rather than drawn here: this panel's own
  // breadcrumb used to sit directly under the chrome's, saying half of
  // the same thing.
  const leaf =
    view === "detail" && selectedDoc
      ? selectedDoc.name
      : view === "add"
        ? "Add source"
        : view === "ask"
          ? "Ask the graph"
          : view === "stale"
            ? "Needs attention"
            : view === "share" && selectedDoc
              ? `Share ${selectedDoc.name}`
              : null;
  const title: React.ReactNode = leaf ?? "Artifacts";
  const levels = {
    labels: leaf ? [leaf] : [],
    // One level deep, so any truncation returns to the list.
    onTruncate: () => {
      setView("list");
      setSelected(null);
    },
  };

  // ── Header extras: per-view actions ─────────────────────────────
  let headerExtras: React.ReactNode = null;
  if (view === "list" && status) {
    headerExtras = (
      <div className="kb-head-actions">
        <button
          className="kb-icon-btn"
          title={busy === "sync" ? "Syncing…" : "Sync git-shared artifacts"}
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
        {!!status.stale_count && (
          <button
            className="btn btn-sm btn-warn"
            title="Documents whose sources have changed since they were written"
            onClick={() => setView("stale")}
          >
            {status.stale_count} out of date
          </button>
        )}
        <button
          className="btn btn-sm"
          disabled={!status.enabled}
          title="Ask a question about how these documents relate"
          onClick={() => setView("ask")}
        >
          Ask
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
          <>
            <button
              className="btn btn-sm"
              title="Write a self-contained HTML file next to the project"
              disabled={busy === "export"}
              onClick={() => void exportDoc(selected)}
            >
              {busy === "export" ? "Exporting…" : "Export"}
            </button>
            <button className="btn btn-sm" onClick={() => setView("share")}>
              Share
            </button>
            <button
              className="btn btn-sm btn-danger"
              onClick={() => setConfirmingRemove(true)}
            >
              Remove
            </button>
          </>
        )}
      </div>
    );
  }

  return (
    <Drawer
      title={title}
      levels={levels}
      onClose={onClose}
      headerExtras={headerExtras}
    >
      {notice && (
        <div className="kb-notice" role="status" onClick={() => setNotice("")}>
          {notice}
        </div>
      )}
      {status === null ? (
        <ArtifactsSkeleton />
      ) : view === "stale" ? (
        <ArtifactStaleReview
          client={client}
          onOpenArtifact={(id) => {
            setSelected(id);
            setView("detail");
          }}
        />
      ) : view === "share" && selected ? (
        <ArtifactShareDialog
          client={client}
          artifactId={selected}
          name={selectedDoc?.name ?? selected}
          onClose={() => setView("detail")}
        />
      ) : view === "ask" ? (
        <ArtifactGraphSearch
          client={client}
          initialQuestion={pendingQuestion}
          onConsumeInitial={() => setPendingQuestion("")}
          onOpenArtifact={(id) => {
            setSelected(id);
            setView("detail");
          }}
        />
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
      ) : docs && docs.length === 0 ? (
        <EmptyHero onAdd={() => setView("add")} embedder={status.embedder} />
      ) : (
        <ArtifactSearch
          client={client}
          docs={(docs || []).map((d) => ({
            id: d.id,
            name: d.name,
            kind: d.kind,
            n: d.n,
            preview: d.preview,
            added_at: d.added_at,
            size: d.size,
          }))}
          staleIds={staleIds}
          onOpen={(id) => {
            setSelected(id);
            setView("detail");
          }}
          onAsk={(q) => {
            setPendingQuestion(q);
            setView("ask");
          }}
        />
      )}
    </Drawer>
  );
}

// ── Disabled state ───────────────────────────────────────────────

function DisabledState() {
  // This used to tell the reader to set `IGNI_NEO4J_RUNTIME=1` "for the
  // backend" — a variable a Finder-launched .app cannot inherit, so the
  // instruction could not be carried out by anyone who needed it. The
  // switch is `artifacts.enabled` in config now, which is both settable
  // and the one the rest of the backend already reads.
  return (
    <div className="kb-empty">
      <div className="kb-empty-title">Artifacts disabled</div>
      <div className="kb-empty-hint">
        Semantic search is off because <code>artifacts.enabled = false</code> in{" "}
        <code>~/.ember/config.yaml</code>. Set it to <code>true</code> and reopen the app.
        The first launch after that downloads a local database (~500 MB, one time).
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
      <div className="kb-empty-title">Nothing in the artifacts yet</div>
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



// ── Document detail page ─────────────────────────────────────────

function DocumentDetail({
  client,
  id,
}: {
  client: IgniClient;
  id: string;
}) {
  const [doc, setDoc] = useState<ArtifactsDocFull | null>(null);
  const [loading, setLoading] = useState(true);
  // 0 means "whatever head is", so opening a document after it was
  // revised elsewhere shows the current text rather than a version
  // number this component happened to remember.
  const [version, setVersion] = useState(0);
  const [versions, setVersions] = useState<ArtifactVersionRow[]>([]);
  const [lineage, setLineage] = useState<ArtifactLineageView | null>(null);
  // Bumped after a confirm/dismiss so the lineage refetches. A local
  // edit of the list would drift from the store the moment anything
  // else wrote an edge.
  const [lineageNonce, setLineageNonce] = useState(0);

  // Reset the pinned version when the selected document changes —
  // otherwise v3 of one artifact silently requests v3 of the next.
  useEffect(() => {
    setVersion(0);
  }, [id]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setDoc(null);
    void client
      .rpc<ArtifactsDocFull>("artifact_get", { id, version })
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
  }, [client, id, version]);

  // History and lineage load alongside the document rather than
  // inside it: a failure to walk the graph should leave the document
  // readable, not replace it with an error.
  useEffect(() => {
    let cancelled = false;
    void client
      .rpc<ArtifactVersionRow[]>("artifact_versions", { id })
      .then((r) => {
        if (!cancelled) setVersions(r ?? []);
      })
      .catch(() => {
        if (!cancelled) setVersions([]);
      });
    return () => {
      cancelled = true;
    };
  }, [client, id]);

  useEffect(() => {
    let cancelled = false;
    void client
      .rpc<ArtifactLineageView>("artifact_lineage", { id, version })
      .then((r) => {
        if (!cancelled) setLineage(r ?? null);
      })
      .catch(() => {
        if (!cancelled) setLineage(null);
      });
    return () => {
      cancelled = true;
    };
  }, [client, id, version, lineageNonce]);

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

  const staleSources = (lineage?.sources ?? []).filter(
    (s) => s.state === "confirmed" && s.is_stale,
  );

  // Attribution as a sentence, the way an artifact header reads:
  // who wrote it, which version, when. A row of chips makes three
  // facts look like three filters.
  const attribution = [
    doc.author || null,
    doc.n ? `v${doc.n}` : null,
    doc.is_head === false ? `superseded by v${doc.head_n}` : null,
    doc.created_at ? formatRelative(doc.created_at) : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <article className="kb-doc">
      <header className="kb-doc-head">
        <h2 className="kb-doc-title">{doc.name}</h2>
        {attribution && <p className="kb-doc-attribution">{attribution}</p>}
        {doc.source && doc.source !== doc.name && (
          <p className="kb-doc-origin">
            {/^https?:\/\//.test(doc.source) ? (
              <a href={doc.source} target="_blank" rel="noreferrer">
                {doc.source}
              </a>
            ) : (
              <code>{doc.source}</code>
            )}
          </p>
        )}
      </header>

      {staleSources.length > 0 && (
        <div className="kb-stale-banner" role="status">
          <strong>Sources have changed since this was written.</strong>
          <ul>
            {staleSources.map((s) => (
              <li key={s.artifact_id}>
                {s.name || s.artifact_id} moved from v{s.pinned_n} to v{s.head_n}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="kb-doc-body markdown-body">
        <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
          {doc.content || "_(empty)_"}
        </ReactMarkdown>
      </div>

      <VersionSwitcher
        versions={versions}
        current={doc.n ?? 0}
        onPick={(n) => setVersion(n)}
      />
      <LineageSection
        client={client}
        artifactId={doc.id}
        lineage={lineage}
        onReviewed={() => setLineageNonce((n) => n + 1)}
      />
    </article>
  );
}

/**
 * The version switcher. Hidden for a single-version artifact — a
 * picker with one option is chrome that teaches nothing.
 */
function VersionSwitcher({
  versions,
  current,
  onPick,
}: {
  versions: ArtifactVersionRow[];
  current: number;
  onPick: (n: number) => void;
}) {
  if (versions.length < 2) return null;
  return (
    <section className="kb-lineage">
      <h3 className="kb-lineage-heading">Versions</h3>
      <ul className="kb-lineage-rows">
        {versions.map((v) => (
          <li key={v.n} className={v.n === current ? "kb-row kb-row-current" : "kb-row"}>
            <button
              type="button"
              className="kb-version-btn"
              onClick={() => onPick(v.n)}
              aria-current={v.n === current}
            >
              v{v.n}
            </button>
            <span className="kb-row-when">{formatRelative(v.created_at)}</span>
            <span className="kb-row-note">
              {formatBytes(v.size)}
              {v.is_head ? " · current" : ""}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

/**
 * What this was built on, and what sits on top of it.
 *
 * Confirmed sources and captured candidates are rendered as separate
 * groups rather than one list with a badge: they mean different
 * things — one is an assertion, the other a guess nobody has
 * reviewed — and only the first raises a staleness warning.
 */
function LineageSection({
  client,
  artifactId,
  lineage,
  onReviewed,
}: {
  client: IgniClient;
  artifactId: string;
  lineage: ArtifactLineageView | null;
  onReviewed: () => void;
}) {
  if (!lineage) return null;
  const confirmed = lineage.sources.filter((s) => s.state === "confirmed");
  const candidates = lineage.sources.filter((s) => s.state === "candidate");
  if (
    confirmed.length === 0 &&
    candidates.length === 0 &&
    lineage.citations.length === 0 &&
    lineage.dependents.length === 0
  ) {
    return null;
  }
  return (
    <>
      {confirmed.length > 0 && (
        <section className="kb-lineage">
          <h3 className="kb-lineage-heading">Built on</h3>
          <ul className="kb-lineage-rows">
            {confirmed.map((s) => (
              <li
                key={s.artifact_id}
                className={s.is_stale ? "kb-row kb-row-stale" : "kb-row"}
              >
                <span className="kb-row-name">{s.name || s.artifact_id}</span>
                <span className="kb-row-n">v{s.pinned_n}</span>
                <span className="kb-row-note">
                  {s.is_stale ? `source now at v${s.head_n}` : "up to date"}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {lineage.citations.length > 0 && (
        <section className="kb-lineage">
          <h3 className="kb-lineage-heading">External sources</h3>
          <ul className="kb-lineage-rows">
            {lineage.citations.map((url) => (
              <li key={url} className="kb-row">
                <a href={url} target="_blank" rel="noreferrer">
                  {url}
                </a>
              </li>
            ))}
          </ul>
        </section>
      )}

      {candidates.length > 0 && (
        <section className="kb-lineage">
          <h3 className="kb-lineage-heading">Captured, not confirmed</h3>
          <ul className="kb-lineage-rows">
            {candidates.map((s) => (
              <CandidateRow
                key={s.artifact_id}
                client={client}
                artifactId={artifactId}
                row={s}
                onReviewed={onReviewed}
              />
            ))}
          </ul>
        </section>
      )}

      {lineage.dependents.length > 0 && (
        <section className="kb-lineage">
          <h3 className="kb-lineage-heading">Depended on by</h3>
          <ul className="kb-lineage-rows">
            {lineage.dependents.map((d) => (
              <li
                key={d.artifact_id}
                className={d.is_stale ? "kb-row kb-row-stale" : "kb-row"}
              >
                <span className="kb-row-name">{d.name || d.artifact_id}</span>
                <span className="kb-row-n">v{d.n}</span>
                <span className="kb-row-note">
                  {d.is_stale ? `pinned to v${d.pinned_n}` : ""}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}

/**
 * One captured source, with the verdict that decides whether it
 * counts.
 *
 * Confirming is what turns a captured read into a real dependency —
 * only confirmed edges raise a staleness warning. Dismissing records
 * that somebody looked and said no, which is why it is not a delete:
 * re-reading the same source would otherwise re-capture it, and the
 * same judgement would be asked for again every session.
 */
function CandidateRow({
  client,
  artifactId,
  row,
  onReviewed,
}: {
  client: IgniClient;
  artifactId: string;
  row: ArtifactLineageRow;
  onReviewed: () => void;
}) {
  const [busy, setBusy] = useState("");

  const verdict = async (
    method: "artifact_confirm_source" | "artifact_dismiss_source",
  ) => {
    setBusy(method);
    try {
      await client.rpc<boolean>(method, {
        id: artifactId,
        source_id: row.artifact_id,
      });
      onReviewed();
    } finally {
      setBusy("");
    }
  };

  return (
    <li className="kb-row kb-row-candidate">
      <span className="kb-row-name">{row.name || row.artifact_id}</span>
      <span className="kb-row-n">v{row.pinned_n}</span>
      <button
        className="btn btn-xs"
        disabled={!!busy}
        title="This document really was built on that one"
        onClick={() => void verdict("artifact_confirm_source")}
      >
        {busy === "artifact_confirm_source" ? "…" : "Confirm"}
      </button>
      <button
        className="btn btn-xs btn-quiet"
        disabled={!!busy}
        title="Read at the time, but not a source"
        onClick={() => void verdict("artifact_dismiss_source")}
      >
        {busy === "artifact_dismiss_source" ? "…" : "Not a source"}
      </button>
    </li>
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
          {busy ? "Adding…" : "Add to artifacts"}
        </button>
      </div>
    </div>
  );
}

function ArtifactsSkeleton() {
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
