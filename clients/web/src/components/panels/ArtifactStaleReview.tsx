import { useCallback, useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { DiffTable, type VersionDiff } from "./ArtifactDiffTable";
import type { IgniClient } from "../../protocol/client";

/**
 * Documents whose foundation has moved, told the way branches work.
 *
 * Revising a document produces v6 after v5 the way a commit follows a
 * commit on the branch you are working on — nothing has drifted and
 * nothing needs deciding. What matters is the *other* branch: the
 * document this one was built on, and whether it has moved since we
 * branched from it. Once it has, this document is a feature branch
 * whose main moved on: still readable, no longer safely mergeable,
 * and the only way to know which is to read what landed.
 *
 * So each row answers three questions in order — where we branched,
 * what landed there since, and what that means for us. The third is a
 * judgement about prose, which is why it is asked of the agent rather
 * than computed: the machinery can say a source moved and which of
 * our sentences lean on it, and cannot say whether they are now
 * wrong.
 */

interface Affected {
  text: string;
  line_from: number;
  line_to: number;
  strength: string;
}

interface Drift {
  dependent_name: string;
  source_name: string;
  pinned_n: number;
  source_head_n: number;
  versions_behind: number;
  summary: string;
  affected: Affected[];
  /** The turn that asks what the drift means. */
  prompt: string;
  error: string;
}

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
  const [diff, setDiff] = useState<VersionDiff | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const loadDiff = useCallback(async () => {
    // Fetched on demand rather than with the list. A diff per row on
    // open would load every source's full text to render a list most
    // of whose rows nobody expands.
    //
    // `artifact_compare` on the *source*, between the version this
    // document was pinned to and the version it is at now. That is
    // exactly the question, and it is the same comparison the version
    // picker runs — this row used to render the raw unified diff
    // instead, which is the terminal output the comparison view
    // exists to replace.
    setBusy(true);
    try {
      const result = await client.rpc<VersionDiff>("artifact_compare", {
        id: row.source_artifact_id,
        from_n: row.pinned_n,
        to_n: row.source_head_n,
      });
      setDiff(result ?? null);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [client, row]);

  const [drift, setDrift] = useState<Drift | null>(null);
  const [verdict, setVerdict] = useState("");
  const [thinking, setThinking] = useState("");
  const [asking, setAsking] = useState(false);

  /** Ask the agent what the change upstream means for this document. */
  const analyse = useCallback(async () => {
    setAsking(true);
    setError("");
    setVerdict("");
    setThinking("Reading what landed…");
    try {
      const d = await client.rpc<Drift>("artifact_drift", {
        id: row.artifact_id,
        source_id: row.source_artifact_id,
      });
      setDrift(d ?? null);
      if (!d?.prompt) {
        setError(d?.error || "Nothing has diverged.");
        return;
      }

      let answer = "";
      await client.runMessage(d.prompt, (m) => {
        if (m.type === "content_delta" && !m.is_thinking) {
          answer += m.text;
          setVerdict(answer);
        } else if (m.type === "run_started" && m.parent_run_id) {
          setThinking(`${m.agent_name} is reading both versions`);
        } else if (m.type === "tool_started") {
          setThinking(m.friendly_name || m.tool_name);
        } else if (m.type === "run_error") {
          setError(m.error);
        }
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setAsking(false);
      setThinking("");
    }
  }, [client, row]);

  const behind = row.versions_behind;

  return (
    <div className="kb-stale-row">
      <div className="kb-stale-head">
        <button className="kb-stale-name" onClick={() => onOpen(row.artifact_id)}>
          {row.name || row.artifact_id}
        </button>
        {/* Branched from, and where that branch is now. The wording is
            the point: "built on v1, now at v3" says a dependency
            moved, where "sources have changed" said something had
            happened to us. */}
        <span className="kb-stale-detail">
          branched from{" "}
          <strong>{row.source_name || row.source_artifact_id}</strong> v{row.pinned_n}, now
          at v{row.source_head_n}
        </span>
        <span className="kb-stale-behind">
          {behind} version{behind === 1 ? "" : "s"} behind
        </span>

        <button
          className="btn btn-sm"
          onClick={() => {
            if (diff === null) void loadDiff();
            else {
              setDiff(null);
              setError("");
            }
          }}
          disabled={busy}
        >
          {busy ? "…" : diff === null ? "What landed" : "Hide"}
        </button>

        {/* On demand, never on open. Every row in this queue would
            otherwise start a model run the moment the page was
            looked at. */}
        <button
          className="btn btn-sm btn-primary"
          onClick={() => void analyse()}
          disabled={asking}
          title="Read both versions and say whether this document is still correct"
        >
          {asking ? "Checking…" : "What does it mean for us?"}
        </button>
      </div>

      {error ? <div className="msg-error">{error}</div> : null}

      {/* Which of our sentences lean on the parts that moved. Shown
          before the verdict because it is the evidence the verdict is
          made from, and it is worth reading even if the agent is
          never asked. */}
      {drift && drift.affected.length > 0 && (
        <ul className="kb-drift-affected">
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
      )}

      {(verdict || thinking) && (
        <div className="kb-drift-verdict" aria-live="polite">
          {verdict ? (
            <div className="markdown-body">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{verdict}</ReactMarkdown>
            </div>
          ) : (
            <p className="kb-drift-waiting">{thinking}</p>
          )}
        </div>
      )}

      {diff !== null ? (
        diff.added === 0 && diff.removed === 0 && diff.changed === 0 ? (
          <div className="kb-hint">
            The source moved, but not in a way that shows as a text change.
          </div>
        ) : (
          <div className="kb-stale-diff">
            <DiffTable
              diff={diff}
              leftLabel={`${row.source_name || row.source_artifact_id} v${row.pinned_n} · branched here`}
              rightLabel={`v${row.source_head_n} · now`}
            />
          </div>
        )
      ) : null}
    </div>
  );
}
