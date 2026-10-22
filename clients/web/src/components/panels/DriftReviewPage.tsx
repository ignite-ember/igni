import { useCallback, useEffect, useState } from "react";
import { DiffTable, type VersionDiff } from "./ArtifactDiffTable";
import type { Drift, StaleSource } from "./DocumentDrift";

/** What came back from bringing a document up to date. */
interface Applied {
  ok: boolean;
  /** The version written, when one was. */
  n: number;
  /** Where the edge now points. */
  pinned_to: number;
  /** The text was already right; only the pin moved. */
  text_unchanged: boolean;
  message: string;
  error: string;
}
import type { IgniClient } from "../../protocol/client";

/**
 * Reviewing one upstream change, on a page of its own.
 *
 * The banner on the document says a source has moved and offers two
 * questions. Answering them *there* was wrong in the same way the
 * separate queue was wrong, only in the other direction: a comparison
 * and a report unfolding inside the document being reviewed pushes
 * the thing under review off the screen, and leaves no way to come
 * back to a decision after scrolling away from it.
 *
 * So the review is a page, reached by a crumb: ``Login flow /
 * Upstream change``. Everything needed to check the answer is on it,
 * in the order somebody checks things — where we branched, what
 * landed there, which of our sentences lean on it, and only then what
 * the agent makes of it. Each is worth reading on its own; the report
 * is the last of the four, not the point of the page.
 */

export function DriftReviewPage({
  client,
  artifactId,
  source,
  onBack,
  onOpenSource,
  onUpdated,
}: {
  client: IgniClient;
  artifactId: string;
  source: StaleSource;
  onBack: () => void;
  onOpenSource: (id: string) => void;
  /** A version was written, or the pin moved. */
  onUpdated?: () => void;
}) {
  const [drift, setDrift] = useState<Drift | null>(null);
  const [diff, setDiff] = useState<VersionDiff | null>(null);
  const [applied, setApplied] = useState<Applied | null>(null);
  const [thinking, setThinking] = useState("");
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");

  // The facts load with the page. Only the report costs a model run,
  // and only that waits to be asked for.
  useEffect(() => {
    let cancelled = false;
    void client
      .rpc<VersionDiff>("artifact_compare", {
        id: source.artifact_id,
        from_n: source.pinned_n,
        to_n: source.head_n,
      })
      .then((r) => {
        if (!cancelled) setDiff(r ?? null);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    void client
      .rpc<Drift>("artifact_drift", { id: artifactId, source_id: source.artifact_id })
      .then((r) => {
        if (!cancelled) setDrift(r ?? null);
      })
      .catch(() => {
        /* The comparison is the more important half; a failure here
           costs the affected list, not the page. */
      });
    return () => {
      cancelled = true;
    };
  }, [client, artifactId, source]);

  /**
   * Bring the document up to date, and move the pin with it.
   *
   * Two halves, and the second is the one that is easy to forget: the
   * text is revised *and* the edge is re-pinned to the source's
   * current version. Without the re-pin the document is corrected and
   * still reported as drifted — the same warning for ever.
   *
   * Safe to press. The store is append-only, so this writes a new
   * version beside the old one; nothing is overwritten, and the
   * comparison below shows exactly what it did.
   */
  const update = useCallback(async () => {
    setWorking(true);
    setError("");
    setApplied(null);
    setThinking("Reading both versions and rewriting…");
    try {
      const r = await client.rpc<Applied>("artifact_drift_apply", {
        id: artifactId,
        source_id: source.artifact_id,
      });
      if (r?.error) {
        setError(r.error);
        return;
      }
      setApplied(r ?? null);
      onUpdated?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setWorking(false);
      setThinking("");
    }
  }, [client, artifactId, source, onUpdated]);

  const behind = Math.max(source.head_n - source.pinned_n, 0);
  const unchanged =
    diff !== null && diff.added === 0 && diff.removed === 0 && diff.changed === 0;

  return (
    <div className="drift-page">
      <header className="drift-page-head">
        <p className="drift-page-where">
          This document branched from{" "}
          <button type="button" className="drift-page-source" onClick={() => onOpenSource(source.artifact_id)}>
            {source.name}
          </button>{" "}
          at <strong>v{source.pinned_n}</strong>. It is now at{" "}
          <strong>v{source.head_n}</strong> — {behind} version{behind === 1 ? "" : "s"}{" "}
          ahead.
        </p>
        <button type="button" className="btn btn-sm" onClick={onBack}>
          Back to the document
        </button>
      </header>

      {error ? <div className="msg-error">{error}</div> : null}

      <section className="drift-section">
        <h3 className="drift-section-title">What landed in {source.name}</h3>
        {diff === null ? (
          <p className="kb-hint">Comparing…</p>
        ) : unchanged ? (
          <p className="kb-hint">
            {source.name} moved, but not in a way that shows as a text change.
          </p>
        ) : (
          <DiffTable
            diff={diff}
            leftLabel={`v${source.pinned_n} · branched here`}
            rightLabel={`v${source.head_n} · now`}
          />
        )}
      </section>

      {/* The evidence the report is made from, before the report.
          Worth reading on its own, and the only part a reviewer can
          check without re-reading both documents end to end. */}
      {drift && drift.affected.length > 0 && (
        <section className="drift-section">
          <h3 className="drift-section-title">
            Passages here that draw on what moved
          </h3>
          <p className="kb-hint">
            Matched by meaning, not recorded when the document was written — each is a
            lead to check rather than a finding.
          </p>
          <ul className="drift-page-affected">
            {drift.affected.map((passage) => (
              <li key={`${passage.line_from}-${passage.text.slice(0, 24)}`}>
                <span className="kb-drift-where">
                  L{passage.line_from}
                  {passage.line_to !== passage.line_from ? `–${passage.line_to}` : ""}
                </span>
                <span className="kb-drift-text">{passage.text}</span>
                {passage.strength && (
                  <span className="kb-drift-strength">{passage.strength} match</span>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="drift-section">
        <div className="drift-section-head">
          <h3 className="drift-section-title">Bring this document up to date</h3>
          <button
            type="button"
            className="btn btn-sm btn-primary"
            onClick={() => void update()}
            disabled={working}
          >
            {working ? "Updating…" : applied ? "Update again" : "Update this document"}
          </button>
        </div>

        {applied ? (
          <div className="drift-applied" role="status">
            {applied.text_unchanged ? (
              <>
                <strong>Nothing needed changing.</strong>
                <span>
                  The text was already correct against v{source.head_n}. The link now
                  points at v{applied.pinned_to}, so this will not be raised again.
                </span>
              </>
            ) : (
              <>
                <strong>Updated — now at v{applied.n}.</strong>
                <span>
                  Built on {source.name} v{applied.pinned_to}, and the previous version is
                  still there to compare against.
                </span>
              </>
            )}
          </div>
        ) : thinking ? (
          <p className="kb-drift-waiting">{thinking}</p>
        ) : (
          <p className="kb-hint">
            Rewrites only what the change above makes wrong, and re-points the link at{" "}
            {source.name} v{source.head_n} — so this stops being reported as out of date.
            A new version is written beside the current one; nothing is overwritten.
          </p>
        )}
      </section>
    </div>
  );
}
