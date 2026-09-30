import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import type { IgniClient } from "../../protocol/client";
import { ArtifactGraphSearch } from "./ArtifactGraphSearch";
import { ArtifactSearch } from "./ArtifactSearch";
import { ArtifactShareDialog, type Anchor } from "./ArtifactShareDialog";
import { ArtifactCommentReview } from "./ArtifactCommentReview";
import { ArtifactCompare } from "./ArtifactCompare";
import { MenuIcon } from "../MenuIcon";
import { DocumentPeople } from "./DocumentPeople";
import { LockedDocument, type LockedState } from "./LockedDocument";
import { useDocumentRoom } from "./useDocumentRoom";
import { ArtifactStaleReview } from "./ArtifactStaleReview";
import { Drawer } from "./Drawer";
import { OverflowMenu } from "../OverflowMenu";
import { MarkdownInline } from "../MarkdownInline";
import { DocumentComment } from "./DocumentComment";
import { ColumnHandle } from "./ColumnHandle";
import { formatRelative } from "../../utils/when";
import { DriftBanner, type StaleSource } from "./DocumentDrift";
import { DriftReviewPage } from "./DriftReviewPage";
import { ExportBanner, type ExportOutcome } from "./ExportBanner";
import { ProvenanceView, type CandidateOverlap, type Provenance } from "./ProvenanceView";
import { DocumentCitations } from "./DocumentCitations";
import { DocumentEditor } from "./DocumentEditor";
import { MermaidBlock, SpecBlock, fenceInfo } from "../markdown/RichFences";
import { ArtifactTreeView } from "./ArtifactTreeView";

/** Where the reader's preference about the comment rail is kept. */
/**
 * What the column beside the document is showing.
 *
 * Two things live there and they are wanted at different times: the
 * history when you are working out what changed, the conversation
 * when you are working out what to change. ``both`` is the default
 * and truncates the history, because a fifty-version list shown in
 * full is a wall that pushes the conversation off the screen.
 */
type AsideView = "both" | "versions" | "comments";

const ASIDE_VIEW_KEY = "igni.artifact.aside";

/** Both by default: a section nobody knows about is a feature nobody has. */
function readAsideView(): AsideView {
  try {
    const kept = localStorage.getItem(ASIDE_VIEW_KEY);
    return kept === "versions" || kept === "comments" ? kept : "both";
  } catch {
    return "both";
  }
}

/** How many versions the combined view shows before it stops. */
const VERSIONS_IN_BOTH = 5;

/** What `artifact_export` accepts, and what to call each one. */
type ExportFormat = "html" | "md" | "pdf";

const EXPORT_FORMATS: {
  value: ExportFormat;
  label: string;
  title: string;
}[] = [
  {
    value: "html",
    label: "HTML",
    title: "A self-contained page — styling, lineage and version history included",
  },
  {
    value: "md",
    label: "Markdown",
    title: "The source text as written, under a front-matter header",
  },
  {
    value: "pdf",
    label: "PDF",
    title: "The same page as the HTML export, printed",
  },
];
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
  archived?: boolean;
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

type View = "list" | "detail" | "add" | "ask" | "stale" | "comments" | "drift";
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

// ── Main panel ───────────────────────────────────────────────────

/**
 * What a fenced block becomes in a document.
 *
 * ```mermaid``` draws, ```json-render``` renders, everything else
 * stays the code block it already was. Keyed on the language so
 * adding a third kind is a line here rather than a branch somewhere
 * in the renderer.
 */
const DOC_FENCES = {
  pre: ({ children, ...rest }: { children?: React.ReactNode }) => {
    const fence = fenceInfo(children);
    if (fence?.lang === "mermaid") return <MermaidBlock source={fence.source} />;
    if (fence?.lang === "json-render") return <SpecBlock source={fence.source} />;
    return <pre {...rest}>{children}</pre>;
  },
};

/** How long the typing has to stop before the draft is written.
 *
 * Long enough that ordinary typing is one request rather than sixty,
 * short enough that stepping away from the keyboard for a moment
 * leaves the words somewhere other than this tab. The keystrokes
 * themselves do not wait for this — they go to everybody else with
 * the document open over the room socket, which is a different
 * question from where the words are kept. */
const DRAFT_IDLE = 900;

/** The draft as the backend describes it. */
export interface ArtifactDraftView {
  exists?: boolean;
  content?: string;
  /** Which version it was written from. */
  base_n?: number;
  /** Which version is current. Behind it means somebody published. */
  head_n?: number;
  updated_by?: string;
  error?: string;
}

/** What the header can ask the open editor to do. */
export interface DocumentEditing {
  /** Stop editing. Writes whatever is unsaved to the draft first. */
  done: () => Promise<void>;
  /** Turn the draft into a version. */
  publish: () => Promise<void>;
}

/** What the header says about it while it happens. */
export interface EditingState {
  busy: "" | "save" | "publish";
  /** Typed since the last write to the draft. */
  dirty: boolean;
  /** Something worth saying — a refusal, usually. */
  note: string;
}

export function ArtifactPanel({
  client,
  onClose,
  openSlug = "",
}: {
  client: IgniClient;
  onClose: () => void;
  /** A document to open on arrival, named by the slug in a shared
   *  link rather than by a local id — the whole point is that the
   *  reader has never seen this document, so their own graph has no
   *  name for it until it has been pulled in. */
  openSlug?: string;
}) {
  const [status, setStatus] = useState<ArtifactStatus | null>(null);
  const [docs, setDocs] = useState<ArtifactsDoc[] | null>(null);
  const [view, setView] = useState<View>("list");
  const [selected, setSelected] = useState<string | null>(null);
  const [busy, setBusy] = useState("");
  const [addError, setAddError] = useState("");
  /** Where a document started from the tree will be filed. Empty is
   *  the top level, which is also where the panel's own Add starts. */
  const [addParent, setAddParent] = useState("");
  const [addParentName, setAddParentName] = useState("");
  /** Bumped when the documents changed, to make the tree re-read. */
  const [treeVersion, setTreeVersion] = useState(0);
  const [confirmingRemove, setConfirmingRemove] = useState(false);
  /** Reading, or editing. One document at a time, so a boolean. */
  const [editing, setEditing] = useState(false);
  // What the open editor can be told to do, and what it is doing.
  // The controls for editing are in the header — where Edit and
  // Share are when nobody is editing — and the text they act on is
  // three components down, so the header holds a handle to it rather
  // than a copy of it. A copy would be the older of the two the
  // moment anybody typed.
  const editRef = useRef<DocumentEditing | null>(null);
  const [editState, setEditState] = useState<EditingState>({
    busy: "",
    dirty: false,
    note: "",
  });
  /** Why the delete was refused: the documents it is joined to. */
  const [removeBlocked, setRemoveBlocked] = useState<
    { artifact_id: string; name: string; direction: string }[] | null
  >(null);
  /** The last export, until it is dismissed. */
  const [exported, setExported] = useState<ExportOutcome | null>(null);
  // Comparing is a state of the page, so it lives with the page —
  // the control that toggles it is in the header, beside the other
  // things you can do to a document.
  const [comparing, setComparing] = useState(false);
  /** The upstream change being reviewed. */
  const [reviewing, setReviewing] = useState<StaleSource | null>(null);
  // A third state of the page, beside reading and comparing: the same
  // text, with where each passage came from shown against it.
  const [tracing, setTracing] = useState(false);
  const [versionCount, setVersionCount] = useState(0);
  /** The open share popover: where it hangs and what it opens on.
   *  Null when it is shut. An object rather than a boolean because it
   *  is anchored to whichever of two controls opened it. */
  const [sharing, setSharing] = useState<ShareOpening | null>(null);
  /** Following a link: what came of it. Null until the slug has been
   *  asked about, and then either a local id to open or a door to
   *  draw. */
  const [arrival, setArrival] = useState<Arrival | null>(null);
  const [askingIn, setAskingIn] = useState(false);
  /** How the document on screen is shared, as last reported by the
   *  detail view. A ref, not state: the overflow menu reads it at the
   *  moment it is pressed, and making it state would re-render the
   *  whole panel every time a roster arrived. */
  const sharedNow = useRef<{ reach: ShareReach; listed: string[]; url: string }>({
    reach: "private",
    listed: [],
    url: "",
  });
  /** The link was just copied. Shown on the control that did it,
   *  because a copy with no acknowledgement is indistinguishable
   *  from a copy that did not happen. */
  const [copied, setCopied] = useState(false);
  // Stable, so the effect that reports the roster does not re-run on
  // every render of the panel above it.
  const rememberSharing = useCallback(
    (reach: ShareReach, listed: string[], url: string) => {
      sharedNow.current = { reach, listed, url };
      // State as well as the ref, so the padlock and the link button
      // redraw when the answer arrives. The ref is what the menu
      // reads at the moment it is pressed; this is what the header
      // renders from, and they are updated together so they cannot
      // disagree.
      setSharedReach(reach);
      setShareUrl(url);
    },
    [],
  );
  const [sharedReach, setSharedReach] = useState<ShareReach>("private");
  const [shareUrl, setShareUrl] = useState("");

  const copyLink = useCallback(async () => {
    if (!shareUrl) return;
    try {
      await navigator.clipboard.writeText(shareUrl);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      /* Clipboard refused. Nothing useful to say that the tooltip
         does not already say, and a banner over a document because a
         copy failed is worse than the failure. */
    }
  }, [shareUrl]);

  /**
   * Bring in the document a link named, and open it.
   *
   * Runs once per slug. The pull applies the whole chain through the
   * ordinary write path, so this is the slow part of following a link
   * and the panel says so rather than sitting blank.
   */
  useEffect(() => {
    if (!openSlug) return;
    let dropped = false;
    setArrival({ state: "arriving" });
    void client
      .rpc<OpenedShared>("artifact_open_shared", { slug: openSlug })
      .then((answer) => {
        if (dropped) return;
        if (answer?.ok && answer.artifact_id) {
          setArrival(null);
          setSelected(answer.artifact_id);
          setView("detail");
        } else if (answer?.locked) {
          setArrival({ state: "locked", locked: answer.locked });
        } else {
          setArrival({ state: "failed", why: answer?.error || "That link did not open." });
        }
      })
      .catch(() => {
        if (!dropped) {
          setArrival({ state: "failed", why: "Could not reach the server." });
        }
      });
    return () => {
      dropped = true;
    };
  }, [client, openSlug]);

  /** Ask for a document that is not in this graph, by its slug. */
  const askForSlug = useCallback(
    async (note: string) => {
      setAskingIn(true);
      try {
        const done = await client.rpc<{ ok: boolean; error: string }>("artifact_ask_access", {
          slug: openSlug,
          note,
        });
        if (!done?.ok) return;
        // Marked here rather than re-read. The request went through,
        // which is the only thing the page reports, and a second
        // round trip to be told what we just did is a wait for
        // nothing.
        setArrival((was) =>
          was?.state === "locked"
            ? { state: "locked", locked: { ...was.locked, asked: true, declined: false } }
            : was,
        );
      } catch {
        /* Left as it was. The form is still there to try again. */
      } finally {
        setAskingIn(false);
      }
    },
    [client, openSlug],
  );
  /**
   * Whether the document on screen is archived.
   *
   * Reported up from the detail rather than read off the list: the
   * list deliberately no longer contains archived documents, so the
   * one place that knows is the fetch that opened this one.
   */
  const [openArchived, setOpenArchived] = useState(false);
  /** Bumped when the page archived or restored the open document. */
  const [detailNonce, setDetailNonce] = useState(0);
  /**
   * Whether the comment rail is put away.
   *
   * Remembered, and deliberately not per document. Wanting the page to
   * yourself is a preference about reading, not about this one
   * document — a reader who puts the rail away and finds it back on
   * the next document has not been given a setting, they have been
   * given the same chore again.
   */
  const [asideView, setAsideView] = useState<AsideView>(readAsideView);

  const chooseAsideView = useCallback((view: AsideView) => {
    setAsideView(view);
    try {
      localStorage.setItem(ASIDE_VIEW_KEY, view);
    } catch {
      /* Private browsing and locked-down origins both refuse this.
         The toggle still works for as long as the page is open, which
         is the part the reader is asking for. */
    }
  }, []);

  // A new document starts from its content. Carrying the comparison
  // state across would open the next document already mid-diff, of a
  // revision the reader has not seen.
  useEffect(() => {
    setComparing(false);
  }, [selected]);
  const [notice, setNotice] = useState("");
  // Which documents have a source that moved. Fetched once with the
  // list so a search result can say "out of date" without a request
  // per row.
  const [staleIds, setStaleIds] = useState<Set<string>>(new Set());
  // A question handed over from a search that found nothing. The ask
  // view reads it once, so going back does not re-run it.
  const [pendingQuestion, setPendingQuestion] = useState("");

  const refresh = useCallback(async () => {
    // The tree loads itself, so it has to be told when the set of
    // documents changed from somewhere else on the page — adding one
    // and not seeing it appear reads as the placement having failed,
    // which is exactly what it did not do.
    setTreeVersion((v) => v + 1);
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
    if (view !== "detail") {
      setConfirmingRemove(false);
      setComparing(false);
    }
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

  /**
   * Delete from the document's own menu, through the same gate as the
   * tree's.
   *
   * It used to call ``artifact_remove``, which deletes without asking
   * what is joined to the document — so the rule that a document in
   * the lineage is archived rather than deleted held in the tree and
   * was defeated by the menu on the page itself. One entrance.
   */
  const remove = async (id: string) => {
    setBusy("remove");
    setRemoveBlocked(null);
    try {
      const r = await client.rpc<{
        ok?: boolean;
        error?: string;
        blocked_by?: { artifact_id: string; name: string; direction: string }[];
      }>("artifact_delete_node", { node_id: id });
      if (r?.blocked_by?.length) {
        setRemoveBlocked(r.blocked_by);
        return;
      }
      if (r?.error) {
        setNotice(r.error);
        setConfirmingRemove(false);
        return;
      }
      setSelected(null);
      setConfirmingRemove(false);
      setRemoveBlocked(null);
      setView("list");
      void refresh();
    } finally {
      setBusy("");
    }
  };

  /** Archive the document on screen, and stay on it: it is still readable. */
  const archive = async (id: string, archived: boolean) => {
    setBusy("archive");
    try {
      const r = await client.rpc<{ ok?: boolean; error?: string; message?: string }>(
        "artifact_archive_node",
        { node_id: id, archived },
      );
      setNotice(r?.error || r?.message || "");
      setConfirmingRemove(false);
      setRemoveBlocked(null);
      setDetailNonce((v) => v + 1);
      void refresh();
    } finally {
      setBusy("");
    }
  };

  const exportDoc = async (id: string, format: ExportFormat) => {
    setBusy("export");
    setExported(null);
    try {
      const r = await client.rpc<ExportOutcome>("artifact_export", { id, format });
      // Where the file landed is the whole result of the action, and
      // a silent success leaves the user hunting for it. It goes to a
      // banner with an Open button rather than a line of text in the
      // page: the path is a thing to act on, not to read.
      setExported({ path: r.path || "", format: r.format || format, error: r.error || "" });
    } catch (e) {
      setExported({
        path: "",
        format,
        error: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setBusy("");
    }
  };

  const openExport = async (path: string) => {
    setBusy("open");
    try {
      const r = await client.rpc<{ opened: boolean; error: string }>(
        "artifact_open_export",
        { path },
      );
      // A refusal replaces the banner's success rather than stacking a
      // second notice on top of it — there is one export in question
      // and it now has one state.
      if (r.error) setExported((was) => (was ? { ...was, error: r.error } : was));
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      setExported((was) => (was ? { ...was, error: message } : was));
    } finally {
      setBusy("");
    }
  };

  const tryAdd = async (source: string): Promise<boolean> => {
    if (!source.trim()) return false;
    setBusy("add");
    setAddError("");
    try {
      await client.rpc("artifact_add", { source: source.trim(), parent_id: addParent });
      setAddParent("");
      setAddParentName("");
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
            : view === "comments"
              ? "Comments"
            : null;
  const title: React.ReactNode = leaf ?? "Artifacts";
  // Two levels deep on the drift review: the document, then the
  // change under it. A crumb per level is what lets the reader step
  // back to the document rather than out to the list — which was the
  // only way back when this panel only ever had one.
  const levels = {
    labels:
      view === "drift" && selectedDoc
        ? [selectedDoc.name, "Upstream change"]
        : leaf
          ? [leaf]
          : [],
    onTruncate: (index: number) => {
      if (index === 0 && view === "drift") {
        setView("detail");
        return;
      }
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
        {/* Always offered, not gated on a count. "Nothing is waiting
            on you" is worth being able to confirm, and a button that
            appears only when there is bad news is one people learn to
            dread rather than to use. */}
        <button
          className="btn btn-sm"
          title="Comments people left on documents you published"
          onClick={() => setView("comments")}
        >
          Comments
        </button>
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
            setAddParent("");
            setAddParentName("");
            setView("add");
          }}
        >
          + Add
        </button>
      </div>
    );
  }

  /* The document's own actions. Rendered inside the article rather
     than in the page header, next to the name they act on — the
     header was showing that name a second time, four lines above the
     one the document itself carries. */
  /** The ways of *looking* at this document, which sit on the line
   *  under its title rather than beside it.
   *
   *  Beside the title they shared a row with Edit and Share, and the
   *  five of them read as one undifferentiated bar — a toggle you
   *  turn on next to a button that writes a version. Below, at the
   *  other end of the line that already says who wrote it and when,
   *  they read as what they are: other ways to look at the thing
   *  that line describes. */
  let docViews: React.ReactNode = null;
  let docActions: React.ReactNode = null;
  if (view === "detail" && selected) {
    // Nothing to compare against while you are writing. Comparing two
    // versions and tracing where a sentence came from are both ways
    // of reading a document that is finished; offered mid-sentence
    // they are two ways to lose your place, and the second of them
    // replaces the body you are typing into.
    docViews = confirmingRemove || editing ? null : (
          <div className="doc-views" role="group" aria-label="How to view this document">
            {versionCount > 1 && (
              <button
                type="button"
                className={comparing ? "doc-view is-on" : "doc-view"}
                aria-pressed={comparing}
                title={
                  comparing
                    ? "Back to reading the document"
                    : "See what changed between two versions"
                }
                onClick={() => {
                  // One at a time. Both replace the document body, and
                  // entering one while the other is on would leave a
                  // stale toggle lit beside the view that won.
                  setTracing(false);
                  setComparing((on) => !on);
                }}
              >
                Compare versions
              </button>
            )}
            <button
              type="button"
              className={tracing ? "doc-view is-on" : "doc-view"}
              aria-pressed={tracing}
              title={
                tracing
                  ? "Back to reading the document"
                  : "Show which passage came from which part of its sources"
              }
              onClick={() => {
                setComparing(false);
                setTracing((on) => !on);
              }}
            >
              Where this came from
            </button>
          </div>
    );
    docActions = (
      <div className="kb-head-actions">
        {confirmingRemove ? (
          removeBlocked ? (
            /* The refusal, where the delete was asked for. Naming
               what it is joined to is what turns "you cannot" into
               something the reader can act on. */
            <>
              <span className="kb-hint kb-head-blocked">
                Part of the lineage —{" "}
                {removeBlocked
                  .map(
                    (d) =>
                      `${d.name}${d.direction === "source" ? " (this is built on it)" : " (built on this)"}`,
                  )
                  .join(", ")}
                . Archive it instead.
              </span>
              <button
                className="btn btn-sm"
                onClick={() => {
                  setConfirmingRemove(false);
                  setRemoveBlocked(null);
                }}
              >
                Leave it
              </button>
              <button
                className="btn btn-sm"
                disabled={busy === "archive"}
                onClick={() => void archive(selected, true)}
              >
                {busy === "archive" ? "…" : "Archive"}
              </button>
            </>
          ) : (
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
          )
        ) : editing ? (
          /* Editing, so the header is about editing.

             Edit and Share are the two things you do to a document
             you are reading, and neither is what somebody halfway
             through a paragraph reached for. The controls that
             matter were at the bottom of the document — which is
             fine at three paragraphs and absurd at fifty, where the
             way to stop editing is to scroll to the end of your own
             writing.

             Two of them, and no third. There used to be Discard, and
             it was safe exactly as long as an edit was private: the
             draft is shared now, so throwing it away throws away
             whatever the other person just wrote in it. Leaving the
             editor leaves the draft where it is, which is where they
             are already looking.

             **Done** stops editing. **Publish** makes the version.
             They are deliberately not the same button — a version is
             what other documents are built on and other people have
             read, and an afternoon of saving should not make forty of
             them. */
          <div className="doc-acts doc-acts-edit">
            {/* Said rather than pressed. Whether the words are safe
                is the question somebody editing has all the time,
                and a button they must press to find out is a worse
                answer than a line that already knows. */}
            <span className="doc-edit-state" role="status">
              {editState.busy === "save"
                ? "Saving…"
                : editState.busy === "publish"
                  ? "Publishing…"
                  : editState.note || (editState.dirty ? "Not saved yet" : "Saved to the draft")}
            </span>
            <button
              type="button"
              className="doc-act"
              disabled={!!editState.busy}
              title="Stop editing. What is written stays in the draft."
              onClick={() => void editRef.current?.done()}
            >
              <MenuIcon name="resolve" />
              Done
            </button>
            <button
              type="button"
              className="doc-act doc-act-go"
              disabled={!!editState.busy}
              title="Turn the draft into a new version everybody reads"
              onClick={() => void editRef.current?.publish()}
            >
              <MenuIcon name="export" />
              Publish
            </button>
          </div>
        ) : (
          /* Two groups, and the split between them is what each kind
             of control does. On the left, the ways of *looking* at
             this document: reading it, comparing two versions,
             following where its sentences came from. In the menu, the
             things you *do* to it — export, share, remove.

             They were all in the menu, which read tidily and behaved
             badly. A view toggle in a menu shows its state only while
             the menu is open, so the page gave no standing answer to
             "why am I looking at a diff?"; the way back was behind
             the same three dots as the button that deletes the
             document. Out here they stay lit for as long as they are
             on, which is the whole job. */
          <>

          {/* The two things you do to a document, said out loud. They
              were in the menu with Export and Remove, which read
              tidily and hid the two verbs anybody actually came for
              behind three dots — and put "Edit" one slip of the
              wrist above the button that deletes the thing.

              Share is a split: the left half opens the controls, the
              right half copies the link. They are one subject and
              two intentions — deciding who can see this, and sending
              it to somebody — and a person who already shared it
              wants the second one without being asked the first
              again. */}
          <div className="doc-acts">
            <button
              type="button"
              className="doc-act"
              disabled={openArchived}
              title={
                openArchived
                  ? "Archived documents are frozen. Restore it to make changes."
                  : "Edit this document and save it as a new version"
              }
              onClick={() => setEditing(true)}
            >
              <MenuIcon name="rename" />
              {openArchived ? "Read only" : "Edit"}
            </button>

            <span className="doc-act-split">
              <button
                type="button"
                className="doc-act"
                title="Choose who can open this"
                onClick={(e) =>
                  setSharing({
                    at: anchorOf(e.currentTarget.closest(".doc-act-split")),
                    reach: sharedNow.current.reach,
                    listed: sharedNow.current.listed,
                  })
                }
              >
                {/* Open or shut, from how the document is actually
                    shared. A padlock that never changed would be
                    decoration; this one is the shortest true sentence
                    about the document that fits in a header. */}
                <MenuIcon
                  name={
                    sharedReach === "domain" || sharedReach === "public"
                      ? "unlock"
                      : "lock"
                  }
                />
                Share
              </button>
              <button
                type="button"
                className="doc-act doc-act-link"
                disabled={!shareUrl}
                title={
                  shareUrl
                    ? copied
                      ? "Copied"
                      : "Copy the link"
                    : "Share it first — there is no link yet"
                }
                aria-label="Copy the link"
                onClick={() => void copyLink()}
              >
                <MenuIcon name={copied ? "resolve" : "link"} />
              </button>
            </span>
          </div>

          <OverflowMenu
            label="Document actions"
            actions={[
              {
                key: "export",
                icon: "export",
                label: busy === "export" ? "Exporting…" : "Export",
                title: "Write this document to a file next to the project",
                disabled: busy === "export",
                items: EXPORT_FORMATS.map((f) => ({
                  key: `export-${f.value}`,
                  label: f.label,
                  title: f.title,
                  onSelect: () => void exportDoc(selected, f.value),
                })),
              },
              {
                key: "archive",
                // The two are opposite acts and get opposite arrows.
                // A single box glyph for both left the menu saying
                // the same thing whichever state it was in.
                icon: openArchived ? "restore" : "archive",
                label: openArchived ? "Restore" : "Archive",
                onSelect: () => void archive(selected, !openArchived),
              },
              {
                key: "remove",
                // A bin, and deliberately the least box-like glyph
                // in the set: Archive is the thing people mean to
                // press, and these two sit next to each other.
                icon: "remove",
                label: "Remove",
                danger: true,
                onSelect: () => {
                  setRemoveBlocked(null);
                  setConfirmingRemove(true);
                },
              },
            ]}
          />
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
      titleInBody={view === "detail"}
    >
      {notice && (
        <div className="kb-notice" role="status" onClick={() => setNotice("")}>
          {notice}
        </div>
      )}
      {exported && (
        <ExportBanner
          outcome={exported}
          busy={busy === "open"}
          onOpen={() => void openExport(exported.path)}
          onDismiss={() => setExported(null)}
        />
      )}
      {/* Hierarchy left, the artifact in the middle, its comments on
          the right. The tree belongs to the *panel*, not to the list:
          a reader who opens a document has not stopped needing to know
          where they are, and a tree that disappears on the way in is a
          tree you navigate by going back. */}
      <div className={status === null ? "kb-shell" : "kb-shell kb-shell-with-tree"}>
        {status !== null && status.enabled && (
          /* The tree is pinned inside a dock that spans the whole
             scrollable height. It has to be *inside* something that
             tall for ``position: sticky`` to hold all the way down —
             docked to the viewport-height page body it came unstuck
             partway through a long document, which is the one thing a
             navigation column must not do. */
          <div className="kb-tree-dock">
            {/* The rule between the hierarchy and the document, and
                the grip for moving it. How deep somebody's folders
                go is not a thing this app can know. */}
            <ColumnHandle
              variable="--tree-width"
              side="left"
              min={150}
              max={520}
              storageKey="igni.artifact.treeWidth"
              label="Resize the hierarchy"
            />
          <ArtifactTreeView
            client={client}
            selectedId={selected}
            staleIds={staleIds}
            reloadKey={treeVersion}
            onOpen={(id) => {
              setTracing(false);
              setComparing(false);
              setSelected(id);
              setView("detail");
            }}
            onChanged={() => {
              setDetailNonce((v) => v + 1);
              void refresh();
            }}
            onCreateInside={(parentId, parentName) => {
              setAddError("");
              setAddParent(parentId);
              setAddParentName(parentName);
              setView("add");
            }}
          />
          </div>
        )}
        <div className="kb-shell-body">
      {/* Arriving from a link comes first: whatever view the panel
          would otherwise draw is about documents this reader already
          has, and the one they followed a link to is not among them
          yet. */}
      {arrival?.state === "arriving" ? (
        <ArtifactsSkeleton />
      ) : arrival?.state === "locked" ? (
        <LockedDocument
          locked={arrival.locked}
          busy={askingIn}
          onAsk={(note) => void askForSlug(note)}
        />
      ) : arrival?.state === "failed" ? (
        <div className="kb-empty">
          <p className="kb-hint">{arrival.why}</p>
        </div>
      ) : status === null ? (
        <ArtifactsSkeleton />
      ) : view === "comments" ? (
        <ArtifactCommentReview
          client={client}
          onOpenArtifact={(id) => {
            setSelected(id);
            setView("detail");
          }}
        />
      ) : view === "stale" ? (
        <ArtifactStaleReview
          client={client}
          onOpenArtifact={(id) => {
            setSelected(id);
            setView("detail");
          }}
        />
      ) : view === "drift" && selected && reviewing ? (
        <DriftReviewPage
          client={client}
          artifactId={selected}
          source={reviewing}
          onBack={() => setView("detail")}
          onUpdated={() => {
            // The document itself refetches on its own: it unmounts
            // while this page is showing and reloads on the way back.
            // What does not is the panel's own status — the count of
            // documents out of date, which has just gone down by one.
            void refresh();
          }}
          onOpenSource={(id) => {
            setReviewing(null);
            setSelected(id);
            setView("detail");
          }}
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
          into={addParentName}
          onAdd={tryAdd}
          onCancel={() => {
            setAddParent("");
            setAddParentName("");
            setView("list");
          }}
        />
      ) : view === "detail" && selected ? (
        <DocumentDetail
          client={client}
          id={selected}
          comparing={comparing}
          tracing={tracing}
          asideView={asideView}
          onAsideViewChange={chooseAsideView}
          actions={docActions}
          views={docViews}
          onReviewDrift={(source) => {
            setReviewing(source);
            setView("drift");
          }}
          onOpenArtifact={(id) => {
            setTracing(false);
            setSelected(id);
            setView("detail");
          }}
          onCompareClosed={() => setComparing(false)}
          onShare={(opening) => {
            sharedNow.current = {
              reach: opening.reach,
              listed: opening.listed,
              url: opening.url ?? sharedNow.current.url,
            };
            setSharing(opening);
          }}
          onSharingChanged={rememberSharing}
          onVersionCount={setVersionCount}
          onArchivedChange={setOpenArchived}
          editing={editing}
          onEditingChange={setEditing}
          editHandle={editRef}
          onEditState={setEditState}
          onPublished={() => setDetailNonce((n) => n + 1)}
          reloadNonce={detailNonce}
        />
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
        </div>
      </div>
      {/* Once for the panel rather than inside whichever control
          opened it: two copies would be two pieces of state, and the
          one left open behind the other is the bug that costs an
          afternoon. */}
      {selected && (
        <ArtifactShareDialog
          client={client}
          artifactId={selected}
          name={selectedDoc?.name ?? selected}
          at={sharing?.at ?? null}
          reach={sharing?.reach}
          listed={sharing?.listed}
          // What was just saved, so the padlock and the link beside
          // it change with it. The roster will say the same thing on
          // its next fetch; waiting for that would leave the header
          // describing the document as it was a moment ago.
          onShared={(reach, listed, url) => {
            rememberSharing(reach as ShareReach, listed, url || shareUrl);
          }}
          onClose={() => setSharing(null)}
        />
      )}
    </Drawer>
  );
}

/** What came of following a link.
 *
 *  Three states and no more, because there are three things to draw:
 *  a wait, a door, and a reason it did not work. Opening successfully
 *  is not one of them — that ends with a document selected and this
 *  back to null. */
type Arrival =
  | { state: "arriving" }
  | { state: "locked"; locked: LockedState }
  | { state: "failed"; why: string };

/** The answer to "open this slug". */
interface OpenedShared {
  ok: boolean;
  artifact_id: string;
  title: string;
  locked: LockedState | null;
  error: string;
}

/** How the document on screen is shared, and where to hang the panel
 *  that changes it. */
export type ShareReach = "private" | "users" | "domain" | "public";

export interface ShareOpening {
  at: Anchor | null;
  reach: ShareReach;
  listed: string[];
  url?: string;
}

/** The rectangle an element occupies, for a popover to hang off. */
function anchorOf(el: Element | null): Anchor | null {
  if (!el) return null;
  const box = el.getBoundingClientRect();
  return { left: box.left, right: box.right, top: box.top, bottom: box.bottom };
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
  comparing,
  tracing,
  asideView = "both",
  onAsideViewChange,
  actions,
  views,
  onOpenArtifact,
  onReviewDrift,
  onCompareClosed,
  onVersionCount,
  onArchivedChange,
  editing = false,
  onEditingChange,
  editHandle,
  onEditState,
  onPublished,
  onShare,
  onSharingChanged,
  reloadNonce = 0,
}: {
  client: IgniClient;
  id: string;
  /** Whether the page is in its comparison state. */
  comparing: boolean;
  /** Whether the page is showing where each passage came from. */
  tracing?: boolean;
  /** Whether the comment rail is put away. Owned by the panel, because
   *  the control that toggles it is in the header beside the other
   *  things you can do to a document. */
  asideView?: AsideView;
  onAsideViewChange?: (view: AsideView) => void;
  /** Jump to another document — a source, from a traced passage. */
  onOpenArtifact?: (id: string) => void;
  /** Open the review page for an upstream change. */
  onReviewDrift?: (source: StaleSource) => void;
  /** What you can do to this document, shown beside its name. */
  actions?: React.ReactNode;
  /** Other ways to look at it, shown on the line below — at the far
   *  end of the one that already says who wrote it and when. */
  views?: React.ReactNode;
  onCompareClosed: () => void;
  /** Open the sharing controls, anchored to what was pressed, and
   *  carrying how the document is shared today — the detail view has
   *  that on screen already, and a panel that fetched it again would
   *  open on a default and correct itself a moment later. Absent for
   *  anybody who may not change them. */
  onShare?: (opening: ShareOpening) => void;
  /** How this document is shared, reported whenever it is learned —
   *  so the overflow menu, which sits outside this view, can open the
   *  share panel on the right answer rather than on a default. */
  onSharingChanged?: (reach: ShareReach, listed: string[], url: string) => void;
  /** So the header knows whether there is anything to compare. */
  onVersionCount: (count: number) => void;
  /** Editing rather than reading. Owned by the panel, because the
   *  control that turns it on is in the header beside the other
   *  things you can do to a document. */
  editing?: boolean;
  onEditingChange?: (editing: boolean) => void;
  /** Where to hang the editor's controls, so the header can press
   *  them. The header is the panel's; the text is this view's. */
  editHandle?: React.MutableRefObject<DocumentEditing | null>;
  /** What the header should say while editing. */
  onEditState?: (state: EditingState) => void;
  /** A version was just made, so the page should read it again. */
  onPublished?: () => void;
  /** Whether this document is frozen, for the page's own menu. */
  onArchivedChange: (archived: boolean) => void;
  /** Bumped by the page when it archived or restored this document. */
  reloadNonce?: number;
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
  // Bumped when the agent may have written a version. The document
  // effect keys on ``version``, and answering a comment leaves that
  // at 0 — "whatever head is" — so nothing would refetch and the
  // page would sit on the text the comment was about.
  const [docNonce, setDocNonce] = useState(0);
  // ── The draft ────────────────────────────────────────────────────
  //
  // What is typed goes here and nowhere else until somebody
  // publishes. One draft per document, shared by everybody who can
  // read it, so two people editing the same paragraph are editing
  // the same text rather than two copies of it that one of them is
  // about to lose.
  const [draft, setDraft] = useState<ArtifactDraftView | null>(null);
  /** Whether the draft has been asked for yet. The editor must not
   *  open on the published text and then have the draft arrive
   *  underneath it — that reads as somebody else's edit landing on
   *  yours, and lands the cursor somewhere else. */
  const [draftAsked, setDraftAsked] = useState(false);
  /** The latest markdown, and whether it has reached the server.
   *  Refs, not state: they are read by callbacks the header holds,
   *  and a stale closure here would save the paragraph before last. */
  const typed = useRef("");
  const unsaved = useRef(false);
  const idle = useRef(0);
  const [editBusy, setEditBusy] = useState<"" | "save" | "publish">("");
  const [editNote, setEditNote] = useState("");
  const [editDirty, setEditDirty] = useState(false);
  // Handed to the comment composer so only text inside the document
  // can be quoted — without it, selecting a version number in the
  // list below would be carried in as a passage.
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const [provenance, setProvenance] = useState<Provenance | null>(null);
  // Who this is shared with, and who has it open. Both come from the
  // cloud copy through this machine's backend, so a document that was
  // never shared simply has nobody on it.
  //
  // Up here with the other hooks, not beside the row it draws. This
  // component returns early while a document is loading, and a hook
  // below that return is a hook React sees on some renders and not
  // others — which is not a subtle bug, it is a blank page.
  const room = useDocumentRoom(client, id);

  // Tell the panel above how this document is shared, so a control
  // outside this view can open the share popover on it.
  useEffect(() => {
    if (!room.roster) return;
    onSharingChanged?.(
      room.roster.reach as ShareReach,
      room.roster.people.filter((p) => p.role !== "owner").map((p) => p.identity),
      room.roster.url ?? "",
    );
  }, [room.roster, onSharingChanged]);
  // Whether the shared copy is open to this account. Up here with
  // every other hook, for the reason the one above learned the hard
  // way: this component returns early while a document loads.
  const [locked, setLocked] = useState<LockedState | null>(null);
  const [asking, setAsking] = useState(false);

  useEffect(() => {
    if (!id) return;
    let dropped = false;
    void client
      .rpc<LockedState>("artifact_locked", { artifact_id: id })
      .then((answer) => {
        if (!dropped) setLocked(answer ?? null);
      })
      .catch(() => {
        // Open, on a failure to ask. A server we cannot reach is not
        // grounds for putting a lock over work already on this
        // machine.
        if (!dropped) setLocked(null);
      });
    return () => {
      dropped = true;
    };
  }, [client, id]);

  // Ask what is already being written, on opening the editor. A
  // document somebody left half-rewritten yesterday should open on
  // their words, not on the version they were rewriting.
  useEffect(() => {
    if (!id) return;
    let dropped = false;
    void client
      .rpc<ArtifactDraftView>("artifact_draft", { artifact_id: id })
      .then((answer) => {
        if (dropped) return;
        setDraft(answer?.exists ? answer : null);
        setDraftAsked(true);
      })
      .catch(() => {
        // No draft we could read is not the same as no draft, but it
        // is the same thing to do: open on the published text. The
        // save that follows will be refused too, and say so then.
        if (!dropped) setDraftAsked(true);
      });
    return () => {
      dropped = true;
    };
    // Asked while reading as well as while editing: an unpublished
    // draft belongs in the version list whether or not the person
    // looking at it is the one who wrote it. Re-asked on ``docNonce``
    // so publishing, or the agent writing one, is reflected there.
  }, [client, id, editing, docNonce]);

  // Leaving the editor forgets what was being typed — not the draft,
  // which is on the server, but this component's copy of it. Keeping
  // it would mean re-entering the editor showed the words as unsaved
  // a second time and offered to write them again.
  useEffect(() => {
    if (editing) return;
    setDraftAsked(false);
    setEditNote("");
    setEditDirty(false);
    unsaved.current = false;
    typed.current = "";
  }, [editing]);

  /** Write what has been typed, if it has not been written already. */
  const keepDraft = useCallback(async () => {
    if (!unsaved.current) return;
    const words = typed.current;
    setEditBusy("save");
    try {
      const r = await client.rpc<{ ok?: boolean; error?: string }>("artifact_save_draft", {
        artifact_id: id,
        content: words,
      });
      if (r?.error || !r?.ok) throw new Error(r?.error || "Could not save that.");
      // Only if nothing was typed while this was in flight, or the
      // keystrokes that arrived meanwhile would be marked saved
      // without ever having been sent.
      if (typed.current === words) {
        unsaved.current = false;
        setEditDirty(false);
      }
      setEditNote("");
    } catch (e) {
      setEditNote(e instanceof Error ? e.message : String(e));
      throw e;
    } finally {
      setEditBusy("");
    }
  }, [client, id]);

  /** Typed in the editor. Held here, and written when the typing
   *  stops — a save per keystroke is a request per keystroke, and
   *  the keystrokes themselves already go to whoever else has this
   *  document open, over the room socket. */
  const edited = useCallback(
    (markdown: string, changed: boolean) => {
      typed.current = markdown;
      if (!changed) return;
      unsaved.current = true;
      setEditDirty(true);
      window.clearTimeout(idle.current);
      idle.current = window.setTimeout(() => {
        void keepDraft().catch(() => {
          // Said in the header by the line above. Nothing is lost:
          // the words are still in the editor and still unsaved, so
          // Done will try again.
        });
      }, DRAFT_IDLE);
    },
    [keepDraft],
  );

  useEffect(() => () => window.clearTimeout(idle.current), []);

  // What the header presses. Assigned rather than passed down,
  // because the controls are up there and the text is down here.
  useEffect(() => {
    if (!editHandle) return;
    editHandle.current = {
      done: async () => {
        window.clearTimeout(idle.current);
        try {
          await keepDraft();
        } catch {
          // Still editing, still unsaved, and the header says why.
          // Closing on a failed save is the one way to lose the
          // words, since the draft is where they were going.
          return;
        }
        onEditingChange?.(false);
      },
      publish: async () => {
        window.clearTimeout(idle.current);
        try {
          await keepDraft();
        } catch {
          return;
        }
        setEditBusy("publish");
        try {
          const r = await client.rpc<{
            ok?: boolean;
            n?: number;
            stale?: boolean;
            message?: string;
            error?: string;
          }>("artifact_publish_draft", { artifact_id: id });
          if (r?.error || r?.stale || !r?.ok) {
            // A conflict keeps the editor open on purpose: the draft
            // is the only copy of what was written from the older
            // version, and it now needs reading against the newer.
            setEditNote(r?.message || r?.error || "Could not publish that.");
            return;
          }
          setEditNote("");
          onEditingChange?.(false);
          setDocNonce((n) => n + 1);
          onPublished?.();
        } finally {
          setEditBusy("");
        }
      },
    };
    return () => {
      editHandle.current = null;
    };
  }, [editHandle, keepDraft, client, id, onEditingChange, onPublished]);

  // Tell the header what to say. Up here rather than in the render,
  // because the header belongs to another component and setting its
  // state during this one's render is a React warning and a loop.
  useEffect(() => {
    onEditState?.({ busy: editBusy, dirty: editDirty, note: editNote });
  }, [onEditState, editBusy, editDirty, editNote]);

  const askForAccess = useCallback(
    async (note: string) => {
      setAsking(true);
      try {
        await client.rpc("artifact_ask_access", { artifact_id: id, note });
        const again = await client.rpc<LockedState>("artifact_locked", { artifact_id: id });
        setLocked(again ?? null);
      } catch {
        setLocked((prev) => (prev ? { ...prev, error: "Could not reach the server." } : prev));
      } finally {
        setAsking(false);
      }
    },
    [client, id],
  );

  // Derived above the early returns, because hooks cannot be called
  // conditionally and ``useDrift`` needs the source.
  // Confirmed only. A candidate is an unadjudicated guess, and
  // printing one as a citation in the body asserts a relationship
  // nobody confirmed — in the reader's own document, where it reads
  // as the author's own attribution.
  const citedSources = (lineage?.sources ?? [])
    .filter((s) => s.state === "confirmed")
    .map((s) => ({
      artifact_id: s.artifact_id,
      name: s.name || s.artifact_id,
      pinned_n: s.pinned_n,
    }));
  const drifted = (lineage?.sources ?? []).filter((s) => s.state === "confirmed" && s.is_stale);
  const driftedSource = drifted.length
    ? {
        artifact_id: drifted[0].artifact_id,
        name: drifted[0].name || drifted[0].artifact_id,
        pinned_n: drifted[0].pinned_n,
        head_n: drifted[0].head_n,
      }
    : null;


  // Reset the pinned version when the selected document changes —
  // otherwise v3 of one artifact silently requests v3 of the next.
  // Clearing ``doc`` belongs here and *only* here: a different
  // document must not be read as this one for a frame, while a
  // refetch of the same one must not blank the page.
  useEffect(() => {
    setVersion(0);
    setDoc(null);
  }, [id]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    // Deliberately *not* clearing ``doc``. Refetching after the agent
    // answers a comment would blank the page for a frame, and the
    // skeleton that replaces it unmounts the conversation along with
    // the exchange the reader is in the middle of — the reply they
    // were reading disappears at the moment it completes.
    void client
      .rpc<ArtifactsDocFull>("artifact_get", { id, version })
      .then((r) => {
        if (cancelled) return;
        setDoc(r);
        // The page's own menu offers Archive or Restore, and the list
        // it would otherwise read from no longer holds archived
        // documents. This fetch is the one place that knows.
        onArchivedChange(r?.archived === true);
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
  }, [client, id, version, docNonce, reloadNonce]);

  // History and lineage load alongside the document rather than
  // inside it: a failure to walk the graph should leave the document
  // readable, not replace it with an error.
  useEffect(() => {
    let cancelled = false;
    void client
      .rpc<ArtifactVersionRow[]>("artifact_versions", { id })
      .then((r) => {
        if (!cancelled) {
          setVersions(r ?? []);
          onVersionCount((r ?? []).length);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setVersions([]);
          onVersionCount(0);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [client, id, docNonce]);

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

  // Fetched on entering the tracing state rather than with the
  // document: it compares every passage against every passage of
  // every source, which is work nobody asked for while they are only
  // reading.
  useEffect(() => {
    if (!tracing) {
      setProvenance(null);
      return;
    }
    let cancelled = false;
    void client
      .rpc<Provenance>("artifact_provenance", { id, n: version })
      .then((r) => {
        if (!cancelled) setProvenance(r ?? null);
      })
      .catch((e) => {
        if (!cancelled) {
          setProvenance({
            artifact_id: id,
            n: version,
            passages: [],
            sources: [],
            affected_count: 0,
            inferred: true,
            unplaced: 0,
            error: e instanceof Error ? e.message : String(e),
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [client, id, version, tracing]);

  // Only while there is nothing to show. A reload of a document
  // already on screen keeps showing it, so answering a comment does
  // not flash a skeleton over what was being read.
  if (!doc) {
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

  const staleSources = drifted.map((s) => ({
    artifact_id: s.artifact_id,
    name: s.name || s.artifact_id,
    pinned_n: s.pinned_n,
    head_n: s.head_n,
  }));

  // Drift is only answerable from the current version. Reading an
  // older one you can look backwards, not forwards: every version
  // after it already exists, so there is nothing to bring up to date
  // that has not been overtaken.
  //
  // Not a tidiness rule. ``_divergence_for`` reads the dependent at
  // version 0 — head — whatever is on screen, so the banner offered
  // from v2 led to a page whose button would have rewritten v3. A
  // control that acts on a document you are not looking at is worse
  // than one that makes no sense.
  const onHead = doc.is_head !== false;

  /**
   * Archived, and therefore frozen.
   *
   * Every control on this page that writes a version is withdrawn
   * rather than left to fail: a button that is refused every time it
   * is pressed is worse than no button, because the first press is
   * spent finding that out and the rest are spent doubting it.
   */
  const frozen = doc.archived === true;

  /** Rendered in one place or another; never twice. */
  const lineageSection = (
    <LineageSection
      client={client}
      artifactId={doc.id}
      lineage={lineage}
      onReviewed={() => setLineageNonce((n) => n + 1)}
    />
  );

  // Shut out of the shared copy. Drawn instead of the document
  // rather than over it, and below every hook — the checks above
  // must all have run by now, or the next render calls a different
  // number of them.
  if (locked && !locked.open) {
    return <LockedDocument locked={locked} busy={asking} onAsk={(note) => void askForAccess(note)} />;
  }

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
    <article
      className={[
        "kb-doc",
        comparing ? "kb-doc-comparing" : "",
        tracing ? "kb-doc-tracing" : "",
        !comparing && !tracing ? "kb-doc-reading" : "",
        // A reload of a document already on screen keeps showing it,
        // so this is the only sign a refetch is in flight. Without it
        // the text simply changes under the reader with no warning
        // that it was about to.
        loading ? "kb-doc-reloading" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      aria-busy={loading || undefined}
    >
      <header className="kb-doc-head">
        {/* Name left, actions right, on one line — so the actions sit
            against the thing they act on rather than against the
            window. */}
        <div className="kb-doc-head-row">
          <h2 className="kb-doc-title">{doc.name}</h2>
          {actions}
        </div>
        {/* One line: who wrote it, which version, when — and then who
            it is shared with and who is in it. All of it is the same
            kind of fact about the document, and on two lines the
            faces read as a second section with its own heading
            missing. */}
        {(attribution || room.roster) && (
          <div className="kb-doc-meta">
            {attribution && <p className="kb-doc-attribution">{attribution}</p>}
                <DocumentPeople
              roster={room.roster}
              here={room.here}
              // Only for somebody who could act on it. A control that
              // opens a dialog whose every field is refused is worse
              // than no control.
              onManage={
                room.roster?.can_manage && onShare
                  ? (at) =>
                      onShare({
                        at,
                        url: room.roster?.url ?? "",
                        reach: (room.roster?.reach ?? "private") as ShareReach,
                        // The addresses the document names, which is
                        // what the audience box holds. The owner is
                        // not one of them: they are on it by owning
                        // it, and listing them would add them to
                        // their own audience on the next publish.
                        listed: (room.roster?.people ?? [])
                          .filter((p) => p.role !== "owner")
                          .map((p) => p.identity),
                      })
                  : undefined
              }
            />
            {/* Pushed to the far end of the line. The facts about
                this document read from the left; the other ways of
                looking at it are not more of the same sentence, and
                setting them against the opposite margin says so
                without a rule or a heading. */}
            {views && <div className="kb-doc-meta-views">{views}</div>}
          </div>
        )}
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

      {/* Where we branched, and what to do about it. This used to
          say "Sources have changed since this was written" and stop —
          naming neither the source, nor how far it had gone, nor
          anything anyone could do. The answers now live on the
          document rather than in a separate queue: the page with the
          problem is the page with the answer.

          On head only. See ``onHead``. */}
      {frozen && (
        <p className="kb-doc-frozen">
          <strong>Archived.</strong> This document is frozen — it keeps every
          version and every link, and it does not take new ones. Restore it
          from the tree if it needs to change.
        </p>
      )}

      {frozen ? null : onHead ? (
        <DriftBanner
          sources={staleSources}
          onReview={() => driftedSource && onReviewDrift?.(driftedSource)}
        />
      ) : (
        staleSources.length > 0 && (
          /* Said, not silently dropped. A reader who knows a source
             moved and finds no sign of it here would reasonably
             conclude it had been dealt with. */
          <p className="kb-doc-drift-elsewhere">
            {staleSources[0].name} has moved since this version was written.
            Bringing the document up to date happens from the current version
            {doc.head_n ? ` (v${doc.head_n})` : ""}
            {" — "}
            <button type="button" className="kb-doc-drift-goto" onClick={() => setVersion(0)}>
              go there
            </button>
            .
          </p>
        )
      )}

      {/* Comparing is a *state of the document*, not a panel under
          it. Showing both at once meant reading the current version,
          scrolling past it, and then reading two more copies of the
          same text. */}
      {tracing ? (
        provenance === null ? (
          <div className="kb-hint">Tracing…</div>
        ) : (
          <ProvenanceView
            provenance={provenance}
            content={doc.content}
            onOpenSource={onOpenArtifact}
          />
        )
      ) : comparing ? (
        <ArtifactCompare
          client={client}
          artifactId={doc.id}
          versions={versions.map((v) => v.n)}
          headN={versions.find((v) => v.is_head)?.n ?? doc.n ?? 0}
          onClose={onCompareClosed}
        />
      ) : (
        /* Document left, its conversation right. Two columns rather
           than one under the other: a comment is about a *passage*,
           and a reader who has to scroll past the whole document to
           read the answer — and scroll back to find the sentence it
           was about — is doing the join by hand. */
        /* One width, whichever of the two the column is showing.
           Narrowing it for the history made the document reflow every
           time the switch was touched — the text you are reading
           jumping sideways to make room for a list of version numbers
           is a worse trade than the room it buys. */
        <div className="kb-doc-split">
          {/* The column that scrolls. Everything belonging to the
              document is inside it, so the conversation beside it can
              scroll on its own without dragging the text along. */}
          <div className="kb-doc-scroll">
          {editing ? (
            /* Opened on the draft when there is one, and on the
               published text when there is not — and not at all
               until we know which. An editor that opened on the
               version and had the draft arrive underneath would look
               like somebody else's edit landing on yours, and would
               put the cursor somewhere you did not leave it. */
            !draftAsked ? (
              <div className="kb-doc-body markdown-body">
                <p className="kb-hint">Opening the draft…</p>
              </div>
            ) : (
              <DocumentEditor
                content={draft?.content ?? doc.content}
                frozen={doc.archived}
                onChange={edited}
              />
            )
          ) : (
          <div className="kb-doc-body markdown-body" ref={bodyRef}>
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              rehypePlugins={[rehypeHighlight]}
              components={DOC_FENCES}
            >
              {doc.content || "_(empty)_"}
            </ReactMarkdown>
            {/* Where each passage came from, marked in the passage.
                An overlay, not nodes spliced into the markdown above —
                text React owns and something else mutates is a
                reconciliation bug waiting for a re-render. */}
            <DocumentCitations
              client={client}
              artifactId={doc.id}
              sources={citedSources}
              bodyRef={bodyRef}
              version={doc.n ?? 0}
              onOpenSource={onOpenArtifact}
            />
          </div>
          )}

          {lineageSection}
          </div>

          {/* Commenting on the local copy is talking to the agent —
              there is nobody else here — and the exchange stays on
              this page. Right-click a selection to ask about that
              passage, or use the composer for the document as a
              whole; the reply streams into this rail either way.

              ``onRevised`` refetches, because the answer to a comment
              is usually a new version and the page under it is then
              out of date with itself. */}
          <div className="kb-doc-aside">
            {/* And the one on the other side of the text. A passage
                with an argument on it wants more room than a
                passage with a remark. */}
            <ColumnHandle
              variable="--rail-width"
              side="right"
              min={240}
              max={720}
              storageKey="igni.artifact.railWidth"
              label="Resize the conversation"
            />
            {/* Both sections pin together. The thread used to be the
                only sticky thing here, so scrolling a long document
                slid the version list out of the top of the window
                while the conversation stayed — which read as the
                history having been closed. */}
            <div className="kb-doc-aside-pin">
            {/* Which of the two this column is showing. The history
                and the conversation are wanted at different moments —
                one while working out what changed, the other while
                working out what to change — and a fifty-version list
                shown in full is a wall the conversation falls off the
                bottom of. */}
            <AsideSwitch
              value={asideView}
              versionCount={versions.length}
              onChange={(v) => onAsideViewChange?.(v)}
            />

            {/* The history first, and the conversation under it.
                Which version you are reading is the frame everything
                else in this column is said within — a comment on v2
                means something different from the same comment on the
                current one — so it is settled before the thread, not
                after it.

                It is also the shorter and the more fixed of the two:
                a list that does not change while you read it sits
                still above one that grows. */}
            {asideView !== "comments" && (
              <VersionSwitcher
                versions={versions}
                draft={draft}
                onOpenDraft={() => onEditingChange?.(true)}
                current={doc.n ?? 0}
                full={asideView === "versions"}
                limit={asideView === "both" ? VERSIONS_IN_BOTH : 0}
                onShowAll={() => onAsideViewChange?.("versions")}
                onPick={(n) => setVersion(n)}
              />
            )}
            {asideView === "both" && versions.length > 1 && (
              <div className="kb-aside-rule" />
            )}

            <DocumentComment
              client={client}
              artifactId={doc.id}
              n={doc.n ?? 0}
              bodyRef={bodyRef}
              frozen={frozen}
              hidden={asideView === "versions"}
              switched
              /* Commenting on a passage while the conversation is put
                 away would file the thread somewhere invisible. The
                 switch comes back on its own. */
              onThreadStarted={() => onAsideViewChange?.("both")}
              onRevised={() => {
                // Back to head: the new version is the answer, and
                // staying pinned to the one that prompted it would
                // show the reply beside the text it asked to change.
                setVersion(0);
                setDocNonce((v) => v + 1);
                setLineageNonce((v) => v + 1);
              }}
            />
            </div>
          </div>
        </div>
      )}

      {/* Where it goes depends on which column scrolls. Reading, it
          belongs inside the one holding the text — pinned below a
          fixed-height column it would be a permanent strip nobody
          asked for, and outside it entirely it would be unreachable. */}
      {(tracing || comparing) && lineageSection}
    </article>
  );
}

/**
 * Which of the two the column beside the document is showing.
 *
 * Three states rather than a pair of toggles: they are exclusive in
 * practice — nobody wants neither — and one control with the current
 * state visible says what two checkboxes only imply.
 */
function AsideSwitch({
  value,
  versionCount,
  onChange,
}: {
  value: AsideView;
  versionCount: number;
  onChange: (view: AsideView) => void;
}) {
  // Nothing to choose between when there is no history to speak of.
  if (versionCount < 2) return null;
  const options: { key: AsideView; label: string; title: string }[] = [
    { key: "both", label: "Both", title: "Recent versions, then the conversation" },
    { key: "versions", label: "Versions", title: `All ${versionCount} versions` },
    { key: "comments", label: "Comments", title: "The conversation only" },
  ];
  return (
    <div className="kb-aside-switch" role="group" aria-label="What to show beside the document">
      {options.map((o) => (
        <button
          key={o.key}
          type="button"
          className={value === o.key ? "kb-aside-tab is-on" : "kb-aside-tab"}
          aria-pressed={value === o.key}
          title={o.title}
          onClick={() => onChange(o.key)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/**
 * The version switcher. Hidden for a single-version artifact — a
 * picker with one option is chrome that teaches nothing.
 *
 * ``limit`` truncates to the most recent few. A long history is a
 * thing you look something up in rather than read to the end, and
 * shown in full beside the conversation it is a wall that pushes the
 * conversation off the bottom of the window.
 */
function VersionSwitcher({
  versions,
  draft,
  onOpenDraft,
  current,
  limit = 0,
  full = false,
  onShowAll,
  onPick,
}: {
  versions: ArtifactVersionRow[];
  /** What is being written and has not been published. It belongs in
   *  this list: it is the next version, and somebody wondering why
   *  the document has not changed since yesterday is asking a
   *  question the list is the natural place to answer. */
  draft?: ArtifactDraftView | null;
  onOpenDraft?: () => void;
  current: number;
  limit?: number;
  /** Showing the whole history, so this section takes the column and
   *  scrolls inside itself rather than sizing to its rows. */
  full?: boolean;
  onShowAll?: () => void;
  onPick: (n: number) => void;
}) {
  // A single version and a draft is still a history worth showing —
  // it says the document has an unpublished next version, which is
  // exactly what somebody who cannot find their edits needs to read.
  if (versions.length < 2 && !draft?.exists) return null;
  // The one being read stays on the list even when it is older than
  // the cut — a picker that hides the current selection is a picker
  // that looks broken.
  const shown = limit > 0 ? versions.slice(0, limit) : versions;
  const hiddenCurrent =
    limit > 0 && !shown.some((v) => v.n === current)
      ? versions.find((v) => v.n === current)
      : undefined;
  const rows = hiddenCurrent ? [...shown, hiddenCurrent] : shown;
  return (
    <section className={full ? "kb-lineage kb-lineage-full" : "kb-lineage"}>
      <h3 className="kb-lineage-heading">Versions</h3>
      <ul className="kb-lineage-rows">
        {draft?.exists && (
          <li className="kb-row kb-row-draft">
            <button
              type="button"
              className="kb-version-btn kb-version-draft"
              onClick={() => onOpenDraft?.()}
              title="Open the draft and go on writing it"
            >
              <span className="kb-draft-chip">Draft</span>
            </button>
            <span className="kb-row-when">
              {draft.updated_by ? "being written" : "unpublished"}
            </span>
            {/* Behind the head means somebody published while this
                was being written, and publishing it will be refused
                until it has been read against theirs. Better said
                here than discovered at the moment of pressing. */}
            <span className="kb-row-note">
              {(draft.head_n ?? 0) > (draft.base_n ?? 0)
                ? `from v${draft.base_n} · behind v${draft.head_n}`
                : `from v${draft.base_n}`}
            </span>
          </li>
        )}
        {rows.map((v) => (
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
      {limit > 0 && versions.length > limit && (
        <button type="button" className="kb-lineage-more" onClick={onShowAll}>
          All {versions.length} versions
        </button>
      )}
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
  // Defaulted, not assumed. A failed ``artifact_lineage`` answers with
  // an error payload that carries no lists, and reading ``.filter`` off
  // one threw inside render — which in React means the whole panel
  // unmounts. A transient RPC failure blanked the page, document and
  // all, with nothing on screen to say why.
  const sources = lineage.sources ?? [];
  const citations = lineage.citations ?? [];
  const dependents = lineage.dependents ?? [];
  const confirmed = sources.filter((s) => s.state === "confirmed");
  const candidates = sources.filter((s) => s.state === "candidate");
  if (
    confirmed.length === 0 &&
    candidates.length === 0 &&
    citations.length === 0 &&
    dependents.length === 0
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
              <ConfirmedSourceRow
                key={s.artifact_id}
                client={client}
                artifactId={artifactId}
                row={s}
              />
            ))}
          </ul>
        </section>
      )}

      {citations.length > 0 && (
        <section className="kb-lineage">
          <h3 className="kb-lineage-heading">External sources</h3>
          <ul className="kb-lineage-rows">
            {citations.map((url) => (
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

      {dependents.length > 0 && (
        <section className="kb-lineage">
          <h3 className="kb-lineage-heading">Depended on by</h3>
          <ul className="kb-lineage-rows">
            {dependents.map((d) => (
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
interface CandidateVerdict {
  /** "source" | "not-a-source" | "unclear". */
  recommendation: string;
  reason: string;
  error: string;
}

/** The agent's call, in the reviewer's words. */
const VERDICT_LABEL: Record<string, string> = {
  source: "The agent thinks this was a source",
  "not-a-source": "The agent thinks this was not a source",
  unclear: "The agent could not tell",
};

interface CandidateEvidence {
  source_name: string;
  source_n: number;
  captured_at: string;
  overlaps: CandidateOverlap[];
  passages_touched: number;
  total_passages: number;
  unplaced: number;
  error: string;
}

/**
 * Which part of a source this document draws on.
 *
 * Shared by both halves of the lineage panel, because both were
 * asking the reader to take a relationship on trust. "Built on Rate
 * limiting v3" names a document and stops — it does not say which
 * paragraph, so a reader who wants to check it re-reads both
 * documents by hand. A captured read had the same problem plus a
 * decision attached.
 *
 * Worked out once and kept on the edge. The comparison is every
 * passage against every passage and the verdict is a model call, and
 * neither side moves on its own — recomputing on every open charges
 * twice for one answer, and a *verdict* recomputed can come back
 * different, so a reviewer who reopens a row to think about it finds
 * the recommendation changed under them.
 */
function SourceEvidenceBody({
  client,
  artifactId,
  sourceId,
  sourceName,
  withVerdict,
  onVerdict,
}: {
  client: IgniClient;
  artifactId: string;
  sourceId: string;
  sourceName: string;
  /** Confirmed sources are already decided; only candidates need a call. */
  withVerdict: boolean;
  onVerdict?: (verdict: CandidateVerdict | null) => void;
}) {
  const [evidence, setEvidence] = useState<CandidateEvidence | null>(null);
  const [loading, setLoading] = useState(true);
  const [verdict, setVerdict] = useState<CandidateVerdict | null>(null);
  const [thinking, setThinking] = useState(withVerdict);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    void client
      .rpc<CandidateEvidence>("artifact_candidate_evidence", {
        id: artifactId,
        source_id: sourceId,
      })
      .then((r) => !cancelled && setEvidence(r ?? null))
      .catch((e) => {
        if (cancelled) return;
        setEvidence({
          source_name: "",
          source_n: 0,
          captured_at: "",
          overlaps: [],
          passages_touched: 0,
          total_passages: 0,
          unplaced: 0,
          error: e instanceof Error ? e.message : String(e),
        });
      })
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [client, artifactId, sourceId]);

  // The agent answers, and the reviewer accepts or rejects the
  // answer. That is the right way round: similarity says two
  // paragraphs are about the same thing and cannot say which way the
  // influence ran, so leaving the judgement to a score left the
  // reviewer adjudicating a question the agent — which read both
  // documents — is better placed to take a position on.
  useEffect(() => {
    if (!withVerdict) return;
    let cancelled = false;
    setThinking(true);
    void client
      .rpc<CandidateVerdict>("artifact_candidate_verdict", {
        id: artifactId,
        source_id: sourceId,
      })
      .then((r) => {
        if (cancelled) return;
        setVerdict(r ?? null);
        onVerdict?.(r ?? null);
      })
      .catch((e) => {
        if (cancelled) return;
        const failed = {
          recommendation: "unclear",
          reason: "",
          error: e instanceof Error ? e.message : String(e),
        };
        setVerdict(failed);
        onVerdict?.(failed);
      })
      .finally(() => !cancelled && setThinking(false));
    return () => {
      cancelled = true;
    };
    // ``onVerdict`` is deliberately out: it is recreated every render
    // by the parent, and depending on it would re-ask the model on
    // every keystroke anywhere in the panel.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [withVerdict, client, artifactId, sourceId]);

  return (
    <div className="kb-cand-body">
      {/* The verdict leads. The measurements under it are what it was
          formed from, and what a reviewer checks it against. */}
      {withVerdict && thinking && !verdict && (
        <p className="kb-hint">The agent is reading both documents…</p>
      )}
      {verdict && !verdict.error && (
        <div className={`kb-cand-verdict kb-cand-verdict-${verdict.recommendation}`}>
          <strong>{VERDICT_LABEL[verdict.recommendation] ?? VERDICT_LABEL.unclear}</strong>
          {verdict.reason && <span>{verdict.reason}</span>}
          <span className="kb-cand-advisory">
            A recommendation, not a decision — nothing is written until you press one of
            the buttons above.
          </span>
        </div>
      )}
      {verdict?.error && (
        <p className="kb-cand-verdict kb-cand-verdict-unclear">
          <strong>No verdict.</strong>
          <span>{verdict.error}</span>
        </p>
      )}

      {loading && <p className="kb-hint">Comparing the two documents…</p>}
      {evidence?.error && <p className="msg-error">{evidence.error}</p>}

      {evidence && !evidence.error && (
        <>
          {evidence.overlaps.length === 0 ? (
            /* The most useful answer there is, and the one a bare row
               could never give. */
            <p className="kb-cand-none">
              Nothing in this document resembles it closely enough to measure.
              {evidence.unplaced > 0
                ? ` ${evidence.unplaced} passage${evidence.unplaced === 1 ? "" : "s"} could not be compared yet — reopen this in a moment before deciding.`
                : withVerdict
                  ? " On this evidence it was read, not used."
                  : " The link was recorded, but the texts no longer show it."}
            </p>
          ) : (
            <>
              <p className="kb-cand-count">
                {evidence.passages_touched} of {evidence.total_passages} passage
                {evidence.total_passages === 1 ? "" : "s"} here resemble something in it.
                Matched by meaning, not recorded — each is a lead to check.
              </p>
              <ul className="kb-cand-overlaps">
                {evidence.overlaps.map((o) => (
                  <li key={`${o.here_line_from}-${o.there_line_from}`}>
                    <div className="kb-cand-pair">
                      <span className="kb-cand-side">
                        <span className="kb-cand-where">
                          here · L{o.here_line_from}
                          {o.here_line_to !== o.here_line_from ? `–${o.here_line_to}` : ""}
                        </span>
                        {/* Rendered inline, not raw: printed literally
                            the evidence reads as "# Client retry guide
                            … `RateLimit-Remaining`" — markup where the
                            reviewer is trying to judge prose. */}
                        <MarkdownInline className="kb-cand-text" text={o.here_text} limit={400} />
                      </span>
                      <span className="kb-cand-side">
                        <span className="kb-cand-where">
                          {sourceName} · L{o.there_line_from}
                          {o.there_line_to !== o.there_line_from
                            ? `–${o.there_line_to}`
                            : ""}
                        </span>
                        <MarkdownInline className="kb-cand-text" text={o.there_text} limit={400} />
                      </span>
                    </div>
                    <span className="kb-cand-strength">
                      {o.strength} · {o.similarity.toFixed(2)}
                    </span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </>
      )}
    </div>
  );
}

/**
 * A confirmed source, with which part of it this document draws on.
 *
 * "Built on Rate limiting v3" was the whole row. It names the
 * dependency and says nothing about where it bites, which is the only
 * question a reader has when that source moves.
 */
function ConfirmedSourceRow({
  client,
  artifactId,
  row,
}: {
  client: IgniClient;
  artifactId: string;
  row: ArtifactLineageRow;
}) {
  const [open, setOpen] = useState(false);
  const name = row.name || row.artifact_id;

  return (
    <li className={row.is_stale ? "kb-row kb-row-source kb-row-stale" : "kb-row kb-row-source"}>
      <div className="kb-cand-head">
        <button
          type="button"
          className="kb-cand-toggle"
          aria-expanded={open}
          onClick={() => setOpen((on) => !on)}
        >
          <span className={open ? "kb-cand-caret kb-cand-caret-open" : "kb-cand-caret"}>
            ›
          </span>
          <span className="kb-row-name">{name}</span>
          <span className="kb-row-n">v{row.pinned_n}</span>
          <span className="kb-row-note">
            {row.is_stale ? `source now at v${row.head_n}` : "up to date"}
          </span>
        </button>
      </div>
      {open && (
        <SourceEvidenceBody
          client={client}
          artifactId={artifactId}
          sourceId={row.artifact_id}
          sourceName={name}
          withVerdict={false}
        />
      )}
    </li>
  );
}

/**
 * A captured read, with enough to decide about it.
 *
 * This row used to be a name, a version number, Confirm and Not a
 * source — the decision with none of what it rests on. Confirm is not
 * a small button: it makes the edge real, and from then on this
 * document is reported as out of date every time that one moves.
 * Being asked to press it knowing only that *something* was read is
 * how a reviewer learns to press whichever is on the left.
 *
 * So the agent's call is marked on the button it points at. It is a
 * *proposal*, not a default — nothing is pre-pressed and nothing
 * happens on a timer, because an accept-by-doing-nothing would make
 * the model's opinion into the record without anybody agreeing to it.
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
  const [open, setOpen] = useState(false);
  const [proposed, setProposed] = useState("");

  // ``decide``, not ``verdict``: the agent gives a verdict, this
  // records the reviewer's decision about it.
  const decide = async (
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

  const name = row.name || row.artifact_id;
  const confirmProposed = proposed === "source";
  const dismissProposed = proposed === "not-a-source";

  return (
    <li className="kb-row kb-row-candidate">
      <div className="kb-cand-head">
        <button
          type="button"
          className="kb-cand-toggle"
          aria-expanded={open}
          onClick={() => setOpen((on) => !on)}
        >
          <span className={open ? "kb-cand-caret kb-cand-caret-open" : "kb-cand-caret"}>
            ›
          </span>
          <span className="kb-row-name">{name}</span>
          <span className="kb-row-n">v{row.pinned_n}</span>
        </button>
        <button
          className={confirmProposed ? "btn btn-xs btn-primary" : "btn btn-xs"}
          disabled={!!busy}
          title={
            confirmProposed
              ? "The agent's recommendation — warn me when this source changes"
              : "This document really was built on that one — warn me when it changes"
          }
          onClick={() => void decide("artifact_confirm_source")}
        >
          {busy === "artifact_confirm_source" ? "…" : "Confirm"}
        </button>
        <button
          className={dismissProposed ? "btn btn-xs btn-primary" : "btn btn-xs btn-quiet"}
          disabled={!!busy}
          title={
            dismissProposed
              ? "The agent's recommendation — read at the time, but not a source"
              : "Read at the time, but not a source"
          }
          onClick={() => void decide("artifact_dismiss_source")}
        >
          {busy === "artifact_dismiss_source" ? "…" : "Not a source"}
        </button>
        {proposed && (
          /* Named, so the highlight reads as somebody's suggestion
             rather than as the button that was always going to be
             pressed. */
          <span className="kb-cand-proposed">agent's pick</span>
        )}
      </div>

      {open && (
        <SourceEvidenceBody
          client={client}
          artifactId={artifactId}
          sourceId={row.artifact_id}
          sourceName={name}
          withVerdict
          onVerdict={(v) => setProposed(v && !v.error ? v.recommendation : "")}
        />
      )}
    </li>
  );
}

// ── Add source page ──────────────────────────────────────────────

function AddSource({
  busy,
  error,
  into,
  onAdd,
  onCancel,
}: {
  busy: boolean;
  error: string;
  /** The document or folder this will be filed under, if any. */
  into: string;
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
      {/* Where it lands, said on the form rather than discovered
          afterwards. Coming here from a row in the tree is the only
          way to file something on creation, and a form that looks
          identical either way makes the placement a surprise. */}
      {into && <p className="kb-hint kb-add-into">Filed under {into}.</p>}
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
