import { useCallback, useEffect, useState } from "react";
import type { IgniClient } from "../../protocol/client";

/**
 * Two versions of a document, side by side.
 *
 * The store already produced a unified diff and it is the right shape
 * for the agent — compact, patch-like, read fluently by a model. It is
 * the wrong shape for a person comparing prose: it interleaves the two
 * texts, so "what did this paragraph used to say" means scanning up
 * and down for the matching `-` line, and a rewritten sentence appears
 * as a deletion and an addition several lines apart.
 *
 * Here the versions sit in columns and the eye compares horizontally.
 * A rewritten line is **one row with two halves**, which is the whole
 * reason the alignment is computed from opcodes rather than by zipping
 * two lists — that difference is invisible on append-only edits and
 * obvious the moment somebody rewrites a sentence.
 *
 * The backend sends rows, not markup. Colour is decided here.
 */

type RowKind = "equal" | "added" | "removed" | "changed";

interface DiffRow {
  kind: RowKind;
  left_n: number;
  left: string;
  right_n: number;
  right: string;
}

interface VersionDiff {
  artifact_id: string;
  name: string;
  from_n: number;
  to_n: number;
  rows: DiffRow[];
  added: number;
  removed: number;
  changed: number;
  truncated: number;
}

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
          <table className="artifact-diff" aria-label={`v${diff.from_n} compared with v${diff.to_n}`}>
            <thead>
              <tr>
                <th colSpan={2}>v{diff.from_n}</th>
                <th colSpan={2}>
                  v{diff.to_n}
                  {diff.to_n === headN ? " · current" : ""}
                </th>
              </tr>
            </thead>
            <tbody>
              {diff.rows.map((row, index) => (
                <tr key={index} data-kind={row.kind}>
                  <td className="diff-num">{row.left_n || ""}</td>
                  <td className="diff-cell diff-left">{row.left}</td>
                  <td className="diff-num">{row.right_n || ""}</td>
                  <td className="diff-cell diff-right">{row.right}</td>
                </tr>
              ))}
            </tbody>
          </table>

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
