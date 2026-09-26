import { useCallback, useEffect, useState } from "react";
import type { IgniClient } from "../../protocol/client";

/**
 * Documents whose sources have moved since they were written.
 *
 * The list is the cheap half. The expensive half — and the reason
 * pinning a version rather than a document was worth doing — is the
 * diff: what actually changed in the source between the version this
 * document was written against and the version it is at now.
 *
 * Nothing here writes. Reconciling means revising a document, and
 * that is authoring: it belongs to the agent, which has the content
 * and the context to make the edit. This surface answers "what needs
 * attention and why", then hands over.
 */

interface StaleRow {
  artifact_id: string;
  name: string;
  n: number;
  source_artifact_id: string;
  source_name: string;
  pinned_n: number;
  source_head_n: number;
  versions_behind: number;
}

export function ArtifactStaleReview({
  client,
  onOpenArtifact,
}: {
  client: IgniClient;
  onOpenArtifact: (id: string) => void;
}) {
  const [rows, setRows] = useState<StaleRow[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    client
      .rpc<StaleRow[]>("artifact_stale")
      .then((r) => {
        if (!cancelled) setRows(r ?? []);
      })
      .catch((e) => {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : String(e));
          setRows([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [client]);

  if (error) return <div className="msg-error">{error}</div>;
  if (rows === null) return <div className="kb-hint">Checking…</div>;

  if (rows.length === 0) {
    return (
      <div className="ag-empty">
        Nothing is out of date. Every confirmed source is still at the version
        its dependents were written against.
      </div>
    );
  }

  return (
    <div className="kb-stale-list">
      <p className="kb-share-sub">
        Each of these was written against a version of its source that has
        since moved. Read what changed before deciding whether the document
        needs revising.
      </p>
      {rows.map((row) => (
        <StaleRowView
          key={`${row.artifact_id}:${row.source_artifact_id}`}
          client={client}
          row={row}
          onOpen={onOpenArtifact}
        />
      ))}
    </div>
  );
}

function StaleRowView({
  client,
  row,
  onOpen,
}: {
  client: IgniClient;
  row: StaleRow;
  onOpen: (id: string) => void;
}) {
  const [diff, setDiff] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const loadDiff = useCallback(async () => {
    // Fetched on demand rather than with the list. A diff per row on
    // open would load every source's full text to render a list most
    // of whose rows nobody expands.
    setBusy(true);
    try {
      const text = await client.rpc<string>("artifact_reconcile_diff", {
        id: row.artifact_id,
        source_id: row.source_artifact_id,
      });
      setDiff(text || "(the source changed, but not in a way that shows as a text diff)");
    } catch (e) {
      setDiff(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [client, row]);

  return (
    <div className="kb-stale-row">
      <div className="kb-stale-head">
        <button className="kb-stale-name" onClick={() => onOpen(row.artifact_id)}>
          {row.name || row.artifact_id}
        </button>
        <span className="kb-stale-detail">
          built on <strong>{row.source_name || row.source_artifact_id}</strong>
          {" "}v{row.pinned_n}, now at v{row.source_head_n}
        </span>
        <span className="kb-stale-behind">
          {row.versions_behind} behind
        </span>
        <button
          className="btn btn-sm"
          onClick={() => (diff === null ? void loadDiff() : setDiff(null))}
          disabled={busy}
        >
          {busy ? "…" : diff === null ? "What changed" : "Hide"}
        </button>
      </div>
      {diff !== null && <pre className="kb-stale-diff">{diff}</pre>}
    </div>
  );
}
