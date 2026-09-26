import { useCallback, useState } from "react";
import type { IgniClient } from "../../protocol/client";
import { ArtifactLineageGraph } from "./ArtifactLineageGraph";

/**
 * Ask the artifact graph a question in English.
 *
 * Everything here runs on the local backend: the question goes to the
 * session's own model, the query it writes runs against the Neo4j
 * sidecar on this machine, and the answer never leaves the host.
 *
 * The generated Cypher is shown and **editable**. That is the point of
 * the surface rather than a debugging affordance — a query that is
 * nearly right is far more common than one that is wrong, and a panel
 * that hides the query leaves the user with nothing to do but rephrase
 * and hope.
 */

interface GraphNode {
  id: string;
  label: string;
  kind: string;
  version?: number | null;
}

interface GraphEdge {
  source: string;
  target: string;
  label: string;
  stale: boolean;
}

interface QueryResult {
  question: string;
  cypher: string;
  columns: string[];
  rows: Record<string, unknown>[];
  graph: { nodes: GraphNode[]; edges: GraphEdge[] };
  truncated: boolean;
  attempts: number;
  unanswerable: boolean;
  error: string;
}

const EXAMPLES = [
  "what would be affected if the auth model changed",
  "which documents have gone out of date",
  "what was the login flow built on",
  "which captured sources has nobody reviewed",
];

export function ArtifactGraphSearch({
  client,
  onOpenArtifact,
}: {
  client: IgniClient;
  onOpenArtifact: (id: string) => void;
}) {
  const [question, setQuestion] = useState("");
  const [cypher, setCypher] = useState("");
  const [result, setResult] = useState<QueryResult | null>(null);
  const [busy, setBusy] = useState<"" | "asking" | "running">("");

  const run = useCallback(
    async (method: "artifact_ask" | "artifact_run_cypher", args: Record<string, string>) => {
      setBusy(method === "artifact_ask" ? "asking" : "running");
      try {
        const r = await client.rpc<QueryResult>(method, args);
        setResult(r);
        // Only the planned path replaces the editor's contents. When
        // the user is editing by hand, overwriting what they typed
        // with the server's echo moves their cursor mid-sentence.
        if (method === "artifact_ask") setCypher(r.cypher ?? "");
      } catch (e) {
        setResult({
          question,
          cypher,
          columns: [],
          rows: [],
          graph: { nodes: [], edges: [] },
          truncated: false,
          attempts: 0,
          unanswerable: false,
          error: e instanceof Error ? e.message : String(e),
        });
      } finally {
        setBusy("");
      }
    },
    [client, cypher, question],
  );

  const ask = useCallback(() => {
    if (question.trim()) void run("artifact_ask", { question });
  }, [question, run]);

  const rerun = useCallback(() => {
    if (cypher.trim()) void run("artifact_run_cypher", { cypher });
  }, [cypher, run]);

  return (
    <div className="ag-search">
      <div className="ag-ask">
        <textarea
          className="ag-question"
          value={question}
          placeholder="Ask about the documents — what depends on what, what went stale…"
          rows={2}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            // Enter asks; Shift+Enter is a newline. A question is
            // usually one line, so making the common case need a
            // modifier would be backwards.
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              ask();
            }
          }}
        />
        <button
          type="button"
          className="ag-btn"
          onClick={ask}
          disabled={busy !== "" || !question.trim()}
        >
          {busy === "asking" ? "Writing a query…" : "Ask"}
        </button>
      </div>

      {!result && (
        <ul className="ag-examples">
          {EXAMPLES.map((example) => (
            <li key={example}>
              <button type="button" className="ag-example" onClick={() => setQuestion(example)}>
                {example}
              </button>
            </li>
          ))}
        </ul>
      )}

      {(cypher || result) && (
        <div className="ag-cypher">
          <div className="ag-cypher-head">
            <span>Query</span>
            {result && result.attempts > 1 && (
              <span className="ag-note">corrected after {result.attempts - 1} retry</span>
            )}
            <button
              type="button"
              className="ag-btn ag-btn-quiet"
              onClick={rerun}
              disabled={busy !== "" || !cypher.trim()}
            >
              {busy === "running" ? "Running…" : "Run"}
            </button>
          </div>
          <textarea
            className="ag-cypher-box"
            value={cypher}
            spellCheck={false}
            rows={Math.min(12, Math.max(3, cypher.split("\n").length))}
            onChange={(e) => setCypher(e.target.value)}
          />
        </div>
      )}

      {result?.error && (
        <div className="msg-error ag-error">
          {result.error}
          {!result.unanswerable && cypher && (
            <div className="ag-note">Edit the query above and run it again.</div>
          )}
        </div>
      )}

      {result && !result.error && (
        <>
          <ResultGraph graph={result.graph} onOpen={onOpenArtifact} />
          <ResultTable result={result} onOpen={onOpenArtifact} />
        </>
      )}
    </div>
  );
}

/**
 * The shape of the answer, beside the answer itself.
 *
 * Delegated to :component:`ArtifactLineageGraph`, which lays the
 * result out as the directed graph it is rather than as the ring the
 * hand-rolled version drew. Direction is the information here —
 * "these were built on that" reads in the arrows or not at all.
 */
function ResultGraph({
  graph,
  onOpen,
}: {
  graph: { nodes: GraphNode[]; edges: GraphEdge[] };
  onOpen: (id: string) => void;
}) {
  if (graph.nodes.length === 0) return null;
  return (
    <section className="ag-graph">
      <h3 className="ag-heading">Shape of the answer</h3>
      <ArtifactLineageGraph
        nodes={graph.nodes}
        edges={graph.edges}
        onOpen={onOpen}
      />
    </section>
  );
}

function ResultTable({
  result,
  onOpen,
}: {
  result: QueryResult;
  onOpen: (id: string) => void;
}) {
  if (result.rows.length === 0) {
    return <div className="ag-empty">No rows. The query ran; nothing matched.</div>;
  }
  const columns = result.columns.length
    ? result.columns
    : Object.keys(result.rows[0] ?? {});

  return (
    <section className="ag-results">
      <h3 className="ag-heading">
        {result.rows.length} row{result.rows.length === 1 ? "" : "s"}
        {result.truncated && " (truncated)"}
      </h3>
      <div className="ag-table-wrap">
        <table className="ag-table">
          <thead>
            <tr>
              {columns.map((c) => (
                <th key={c}>{c}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {result.rows.map((row, i) => {
              const id = rowArtifactId(row);
              return (
                <tr
                  key={i}
                  className={id ? "ag-row ag-row-open" : "ag-row"}
                  onClick={id ? () => onOpen(id) : undefined}
                  title={id ? "Open this document" : undefined}
                >
                  {columns.map((c) => (
                    <td key={c}>{renderCell(row[c])}</td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

// ── Formatting ───────────────────────────────────────────────────

function rowArtifactId(row: Record<string, unknown>): string | null {
  const value = row["artifact_id"] ?? row["id"];
  return typeof value === "string" && value ? value : null;
}

function renderCell(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
