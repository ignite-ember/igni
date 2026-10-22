import { useCallback, useEffect, useState } from "react";
import { DiffTable, type VersionDiff } from "./ArtifactDiffTable";
import type { IgniClient } from "../../protocol/client";

/**
 * Two versions of one document, chosen and compared.
 *
 * The comparison itself lives in :module:`ArtifactDiffTable`, because
 * the stale review needs the same rendering for a different question.
 * What is here is the part specific to *this* question: which pair,
 * and the tally.
 */

export function ArtifactCompare({
  client,
  artifactId,
  versions,
  headN,
  onClose,
}: {
  client: IgniClient;
  artifactId: string;
  /** Version numbers available, newest first. */
  versions: number[];
  headN: number;
  onClose: () => void;
}) {
  // Defaults to "the previous version against the current one",
  // because that is what somebody opening a comparison almost always
  // means — "what changed in the last revision".
  const previous = versions.find((n) => n < headN) ?? headN;
  const [fromN, setFromN] = useState(previous);
  const [toN, setToN] = useState(headN);
  const [diff, setDiff] = useState<VersionDiff | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const result = await client.rpc<VersionDiff>("artifact_compare", {
        id: artifactId,
        from_n: fromN,
        to_n: toN,
      });
      setDiff(result ?? null);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setDiff(null);
    }
  }, [client, artifactId, fromN, toN]);

  useEffect(() => {
    void load();
  }, [load]);

  const unchanged =
    diff !== null && diff.added === 0 && diff.removed === 0 && diff.changed === 0;

  return (
    <div className="artifact-compare">
      <div className="artifact-compare-head">
        <label>
          <span className="kb-hint">from</span>
          <select value={fromN} onChange={(e) => setFromN(Number(e.target.value))}>
            {versions.map((n) => (
              <option key={n} value={n}>
                v{n}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span className="kb-hint">to</span>
          <select value={toN} onChange={(e) => setToN(Number(e.target.value))}>
            {versions.map((n) => (
              <option key={n} value={n}>
                v{n}
                {n === headN ? " (current)" : ""}
              </option>
            ))}
          </select>
        </label>

        {diff && !unchanged ? (
          <span className="artifact-compare-tally">
            <span className="tally-added">+{diff.added + diff.changed}</span>
            <span className="tally-removed">−{diff.removed + diff.changed}</span>
          </span>
        ) : null}

        <button type="button" className="artifact-compare-close" onClick={onClose}>
          Close
        </button>
      </div>

      {error ? <div className="msg-error">{error}</div> : null}

      {diff === null ? (
        <div className="kb-hint">Comparing…</div>
      ) : unchanged ? (
        /* A revision that only touched metadata has an empty diff.
           Saying so beats an empty panel that looks like a failure. */
        <div className="kb-hint">
          {fromN === toN
            ? "Same version on both sides."
            : `Nothing changed between v${diff.from_n} and v${diff.to_n}.`}
        </div>
      ) : (
        <>
          <DiffTable
            diff={diff}
            leftLabel={`v${diff.from_n}`}
            rightLabel={`v${diff.to_n}${diff.to_n === headN ? " · current" : ""}`}
          />

          {/* A diff that quietly stops reads as "and nothing else
              changed" — the one thing it must not say. */}
          {diff.truncated > 0 ? (
            <div className="kb-hint">
              {diff.truncated} more line{diff.truncated === 1 ? "" : "s"} not shown.
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}
