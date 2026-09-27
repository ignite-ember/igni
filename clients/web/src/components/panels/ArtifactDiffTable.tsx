import { useMemo } from "react";
import ReactMarkdown from "react-markdown";
import rehypeRaw from "rehype-raw";
import remarkGfm from "remark-gfm";

/**
 * Rendering a comparison of two texts, side by side.
 *
 * Separated from the panel that asks for one because two surfaces
 * need it and they ask different questions. "What changed between two
 * versions of this document" is one; "what changed in the source this
 * document was built on" is the other, and that second one used to
 * render a raw unified diff — the terminal output this whole module
 * exists to replace.
 *
 * The backend sends rows, not markup. Colour is decided here.
 */

type RowKind = "equal" | "added" | "removed" | "changed";

export interface Part {
  text: string;
  changed: boolean;
}

export interface DiffRow {
  kind: RowKind;
  left_n: number;
  left: string;
  right_n: number;
  right: string;
  left_parts: Part[];
  right_parts: Part[];
}

/** How many unchanged lines to keep either side of a change. */
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

export interface VersionDiff {
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

/** A fenced-code line, which must not be run through markdown. */
function isCode(text: string): boolean {
  return /^\s*(?:```|~~~)/.test(text);
}

function Row({
  row,
  inCode,
  anchor,
}: {
  row: DiffRow;
  inCode: boolean;
  /** Set on changed rows, so the navigator has something to scroll to. */
  anchor?: string;
}) {
  return (
    <tr data-kind={row.kind} id={anchor}>
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


/** The comparison itself: a table of aligned rows. */
/**
 * How much moved, said once above the comparison.
 *
 * There was briefly a stepper here — arrows and a ``1 / 3`` — on the
 * reasoning that showing the whole document makes the changes hard to
 * find. It does not: they are highlighted, and a document is a few
 * screens, not a few thousand lines. Two buttons and an index to walk
 * five rows is machinery in front of something already visible.
 */
function ChangeCount({ rows }: { rows: DiffRow[] }) {
  const edits = useMemo(
    () =>
      rows.reduce(
        (count, row, index) =>
          // Runs of adjacent changed rows are one edit. Counting each
          // line of a rewritten paragraph separately reports five
          // changes where a reader sees one.
          row.kind !== "equal" && (index === 0 || rows[index - 1].kind === "equal")
            ? count + 1
            : count,
        0,
      ),
    [rows],
  );

  if (edits === 0) return null;
  return (
    <p className="diff-count">
      {edits} change{edits === 1 ? "" : "s"} across {rows.length} rows
    </p>
  );
}

export function DiffTable({
  diff,
  leftLabel,
  rightLabel,
}: {
  diff: VersionDiff;
  leftLabel: string;
  rightLabel: string;
}) {
  const inCode = codeRows(diff.rows);

  return (
    <>
      <ChangeCount rows={diff.rows} />
      <table className="artifact-diff" aria-label={`${leftLabel} compared with ${rightLabel}`}>
      {/* Widths declared here, not on the cells. `table-layout: fixed`
          takes its columns from the first row — which is the header,
          and the header spans two cells per side. So the `width` on
          `.diff-num` was never consulted and each gutter took a
          quarter of the table. */}
      <colgroup>
        <col className="diff-col-num" />
        <col />
        <col className="diff-col-num" />
        <col />
      </colgroup>
      <thead>
        <tr>
          <th colSpan={2}>{leftLabel}</th>
          <th colSpan={2}>{rightLabel}</th>
        </tr>
      </thead>
      <tbody>
        {/* Every row, always. Folding the unchanged parts away is what
            a code review wants — the question there is "what did this
            change". Here the question is "what does the document say
            now, and what moved", and a document with its middle
            replaced by "29 unchanged lines" cannot answer the first
            half of it. The changes are found by jumping between them
            instead; see the navigator above. */}
        {diff.rows.map((row, index) => (
          <Row
            key={index}
            row={row}
            inCode={inCode[index]}
            anchor={row.kind === "equal" ? undefined : `diff-change-${index}`}
          />
        ))}
        </tbody>
      </table>
    </>
  );
}
