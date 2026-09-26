import { useCallback, useMemo, useState } from "react";
import type { IgniClient } from "../../protocol/client";

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
 * Laid out by hand rather than with a force simulation: the results
 * are tens of nodes, not thousands, and a deterministic layout means
 * the same query draws the same picture twice — which matters when
 * somebody is comparing two runs. A simulation would also be a
 * dependency, and this panel ships inside an offline app.
 */
function ResultGraph({
  graph,
  onOpen,
}: {
  graph: { nodes: GraphNode[]; edges: GraphEdge[] };
  onOpen: (id: string) => void;
}) {
  const layout = useMemo(() => layOut(graph), [graph]);
  if (!layout) return null;

  const { positions, width, height } = layout;
  return (
    <section className="ag-graph">
      <h3 className="ag-heading">Shape of the answer</h3>
      <svg
        className="ag-canvas"
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={`${graph.nodes.length} documents, ${graph.edges.length} relationships`}
      >
        {graph.edges.map((edge, i) => {
          const a = positions.get(edge.source);
          const b = positions.get(edge.target);
          if (!a || !b) return null;
          return (
            <g key={`${edge.source}-${edge.target}-${i}`} className={edge.stale ? "ag-edge ag-edge-stale" : "ag-edge"}>
              <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} />
              {edge.label && (
                <text x={(a.x + b.x) / 2} y={(a.y + b.y) / 2 - 4} className="ag-edge-label">
                  {edge.label}
                </text>
              )}
            </g>
          );
        })}
        {graph.nodes.map((node) => {
          const at = positions.get(node.id);
          if (!at) return null;
          return (
            <g
              key={node.id}
              className={`ag-node ag-node-${node.kind}`}
              transform={`translate(${at.x} ${at.y})`}
              onClick={() => onOpen(node.id)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") onOpen(node.id);
              }}
            >
              <circle r={7} />
              <text y={20} textAnchor="middle" className="ag-node-label">
                {truncate(node.label, 22)}
                {node.version ? ` v${node.version}` : ""}
              </text>
            </g>
          );
        })}
      </svg>
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

// ── Layout + formatting ──────────────────────────────────────────

/**
 * Place nodes on a circle, or on two rows when the result is a
 * relation.
 *
 * A circle reads badly for "these three depend on that one" — the
 * direction is the information, and a ring hides it. When every edge
 * runs from one group to another, the two groups become two rows and
 * the arrows all point the same way.
 */
function layOut(graph: { nodes: GraphNode[]; edges: GraphEdge[] }) {
  if (graph.nodes.length === 0) return null;
  const positions = new Map<string, { x: number; y: number }>();
  const width = 640;

  const subjects = graph.nodes.filter((n) => n.kind === "artifact");
  const objects = graph.nodes.filter((n) => n.kind !== "artifact");

  if (objects.length > 0 && subjects.length > 0) {
    const height = 200;
    place(subjects, 52, width, positions);
    place(objects, height - 52, width, positions);
    return { positions, width, height };
  }

  const only = graph.nodes;
  if (only.length <= 2) {
    const height = 110;
    place(only, height / 2, width, positions);
    return { positions, width, height };
  }
  const height = 320;
  const radius = Math.min(width, height) / 2 - 46;
  only.forEach((node, i) => {
    const angle = (i / only.length) * Math.PI * 2 - Math.PI / 2;
    positions.set(node.id, {
      x: width / 2 + radius * Math.cos(angle),
      y: height / 2 + radius * Math.sin(angle),
    });
  });
  return { positions, width, height };
}

function place(
  nodes: GraphNode[],
  y: number,
  width: number,
  into: Map<string, { x: number; y: number }>,
) {
  const step = width / (nodes.length + 1);
  nodes.forEach((node, i) => into.set(node.id, { x: step * (i + 1), y }));
}

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

function truncate(text: string, max: number): string {
  return text.length <= max ? text : `${text.slice(0, max - 1)}…`;
}
