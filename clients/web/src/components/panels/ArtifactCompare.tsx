import { useCallback, useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeRaw from "rehype-raw";
import remarkGfm from "remark-gfm";
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

interface Part {
  text: string;
  changed: boolean;
}

interface DiffRow {
  kind: RowKind;
  left_n: number;
  left: string;
  right_n: number;
  right: string;
  left_parts: Part[];
  right_parts: Part[];
}

/** How many unchanged lines to keep either side of a change. */
const CONTEXT = 2;

/**
 * Fold long stretches of untouched text away.
 *
 * A document is mostly unchanged, so showing all of it buries the
 * edit. Keeping a couple of lines either side gives the change
 * somewhere to sit — and a fold that can be opened means nothing is
 * hidden, only set aside.
 */
export function foldUnchanged(
  rows: DiffRow[],
  context = CONTEXT,
): ({ fold: number; from: number } | DiffRow)[] {
  const keep = new Set<number>();
  rows.forEach((row, index) => {
    if (row.kind === "equal") return;
    for (let i = index - context; i <= index + context; i += 1) keep.add(i);
  });

  const out: ({ fold: number; from: number } | DiffRow)[] = [];
  let run = 0;
  rows.forEach((row, index) => {
    if (keep.has(index)) {
      if (run > 0) {
        out.push({ fold: run, from: index - run });
        run = 0;
      }
      out.push(row);
    } else {
      run += 1;
    }
  });
  if (run > 0) out.push({ fold: run, from: rows.length - run });
  return out;
}

/** Escape the few characters that would be read as markup. */
function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/**
 * One side of a row, rendered as the document rather than as source.
 *
 * A comparison that shows `## Overview` and `| a | b |` is showing the
 * markup somebody typed, not the document they wrote — and the point
 * of comparing two versions of a *document* is to read them.
 *
 * So each block goes through the same markdown renderer the document
 * view uses, and the changed words are wrapped in `<mark>` first.
 * `rehype-raw` is what lets that tag survive the pipeline; the
 * alternative — rendering to HTML and then trying to find the words
 * again — has to re-solve a problem the diff already solved.
 *
 * Code blocks are the exception: a fenced line is content, not markup,
 * and running it through markdown would try to interpret it.
 */
/** Cells of a pipe-delimited row, or null if it is not one. */
export function tableCells(text: string): string[] | null {
  const trimmed = text.trim();
  if (!trimmed.startsWith("|")) return null;
  const cells = trimmed.replace(/^\||\|$/g, "").split("|");
  // The `| --- | --- |` separator carries no content; drawing it as a
  // row of dashes says nothing and takes a row to say it.
  if (cells.every((cell) => /^\s*:?-{2,}:?\s*$/.test(cell))) return null;
  return cells.map((cell) => cell.trim());
}

function Side({ parts, text, code }: { parts: Part[]; text: string; code: boolean }) {
  if (code) {
    return (
      <pre className="diff-code">
        {parts && parts.length > 0
          ? parts.map((part, index) =>
              part.changed ? (
                <span key={index} className="diff-word">
                  {part.text}
                </span>
              ) : (
                <span key={index}>{part.text}</span>
              ),
            )
          : text}
      </pre>
    );
  }

  // A single table row is not valid markdown on its own — a table
  // needs a header and a separator — so it renders as literal pipes.
  // Rows are compared one at a time on purpose, which is what makes
  // "one cell changed" land on that cell, so the row is drawn here
  // instead of being handed to the markdown parser.
  const cells = tableCells(text);
  if (cells) {
    const marked = parts && parts.length > 0;
    return (
      <table className="diff-row-table">
        <tbody>
          <tr>
            {cells.map((cell, index) => (
              <td key={index}>
                {marked ? <Marked parts={parts} only={cell} /> : cell}
              </td>
            ))}
          </tr>
        </tbody>
      </table>
    );
  }

  const markup =
    parts && parts.length > 0
      ? parts
          .map((part) =>
            part.changed
              ? `<mark class="diff-word">${escapeHtml(part.text)}</mark>`
              : part.text,
          )
          .join("")
      : text;

  return (
    <div className="diff-md markdown-body">
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeRaw]}>
        {markup}
      </ReactMarkdown>
    </div>
  );
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
  const [opened, setOpened] = useState<Set<number>>(new Set());

  const load = useCallback(async () => {
    try {
      const result = await client.rpc<VersionDiff>("artifact_compare", {
        id: artifactId,
        from_n: fromN,
        to_n: toN,
      });
      setDiff(result ?? null);
      setError("");
      setOpened(new Set());
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
  const inCode = diff ? codeRows(diff.rows) : [];

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
            {/* Widths declared here, not on the cells.
                `table-layout: fixed` takes its columns from the first
                row — which is the header, and the header spans two
                cells per side. So the `width` on `.diff-num` was
                never consulted and each gutter took a quarter of the
                table. */}
            <colgroup>
              <col className="diff-col-num" />
              <col />
              <col className="diff-col-num" />
              <col />
            </colgroup>
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
              {foldUnchanged(diff.rows).map((entry, index) =>
                "fold" in entry ? (
                  opened.has(entry.from) ? (
                    diff.rows
                      .slice(entry.from, entry.from + entry.fold)
                      .map((row, i) => (
                        <Row
                          key={`${entry.from}-${i}`}
                          row={row}
                          inCode={inCode[entry.from + i]}
                        />
                      ))
                  ) : (
                    <tr key={`fold-${entry.from}`} className="diff-fold">
                      <td colSpan={4}>
                        <button
                          type="button"
                          onClick={() =>
                            setOpened((prev) => new Set(prev).add(entry.from))
                          }
                        >
                          {entry.fold} unchanged line{entry.fold === 1 ? "" : "s"}
                        </button>
                      </td>
                    </tr>
                  )
                ) : (
                  <Row key={index} row={entry} inCode={inCode[diff.rows.indexOf(entry)]} />
                ),
              )}
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

/** A fenced-code line, which must not be run through markdown. */
function isCode(text: string): boolean {
  return /^\s*(?:```|~~~)/.test(text);
}

function Row({ row, inCode }: { row: DiffRow; inCode: boolean }) {
  return (
    <tr data-kind={row.kind}>
      <td className="diff-num">{row.left_n || ""}</td>
      <td className="diff-cell diff-left">
        <Side parts={row.left_parts} text={row.left} code={inCode} />
      </td>
      <td className="diff-num">{row.right_n || ""}</td>
      <td className="diff-cell diff-right">
        <Side parts={row.right_parts} text={row.right} code={inCode} />
      </td>
    </tr>
  );
}

/**
 * Which rows are inside a fence.
 *
 * The backend emits each fenced line as its own block, so the fence
 * state has to be recovered here — a line of YAML carries nothing that
 * says it is code, and rendering it as markdown would interpret it.
 */
export function codeRows(rows: DiffRow[]): boolean[] {
  let open = false;
  return rows.map((row) => {
    const fence = isCode(row.left) || isCode(row.right);
    if (fence) {
      open = !open;
      return true;
    }
    return open;
  });
}

/**
 * The changed words of a row, shown inside one cell.
 *
 * The diff marks a *row*, not a cell, so the cell has to work out
 * which of those marks fall inside it. Matching on text rather than
 * on position because the pipes were stripped on the way here and the
 * offsets no longer line up.
 */
function Marked({ parts, only }: { parts: Part[]; only: string }) {
  const changed = parts.filter((p) => p.changed).map((p) => p.text.trim());
  if (changed.length === 0) return <>{only}</>;

  const pattern = changed
    .filter(Boolean)
    .map((word) => word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
    .join("|");
  if (!pattern) return <>{only}</>;

  const pieces = only.split(new RegExp(`(${pattern})`, "g"));
  return (
    <>
      {pieces.map((piece, index) =>
        changed.includes(piece) ? (
          <span key={index} className="diff-word">
            {piece}
          </span>
        ) : (
          <span key={index}>{piece}</span>
        ),
      )}
    </>
  );
}
