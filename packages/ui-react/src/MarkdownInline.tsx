import { useMemo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * Markdown rendered as one flowing line of text.
 *
 * For previews: a list row, a snippet, anywhere a document has to be
 * recognisable in two lines. Showing the raw source there leaks `##`
 * and `**` into prose; rendering it normally is worse, because a
 * heading at heading size in a 11.5px row is a heading, and a bullet
 * list becomes four lines where there was room for two.
 *
 * So every block becomes inline. Headings keep their emphasis but not
 * their size, list items are separated by middots rather than laid
 * out, and code keeps its face. The result reads as the sentence the
 * author wrote, which is what a preview is for.
 *
 * Two constraints are load-bearing, both from where this gets used:
 *
 * - **Nothing interactive.** These previews sit inside a `<button>`
 *   that opens the document. A link inside a button is invalid, and
 *   worse, it is a second click target inside the first.
 * - **Nothing that loads.** An image in a preview is a network
 *   request to render two lines of text, and a broken one is an icon
 *   where a sentence should be.
 */

/** Blocks flow inline, so a preview is one sentence rather than a page. */
const INLINE_COMPONENTS: Components = {
  // Emphasis without the size. A document whose first line is a
  // heading should read as its first line, not as a title.
  h1: ({ children }) => <strong className="mdi-heading">{children}</strong>,
  h2: ({ children }) => <strong className="mdi-heading">{children}</strong>,
  h3: ({ children }) => <strong className="mdi-heading">{children}</strong>,
  h4: ({ children }) => <strong className="mdi-heading">{children}</strong>,
  h5: ({ children }) => <strong className="mdi-heading">{children}</strong>,
  h6: ({ children }) => <strong className="mdi-heading">{children}</strong>,

  p: ({ children }) => <span className="mdi-block">{children}</span>,
  blockquote: ({ children }) => <span className="mdi-block">{children}</span>,

  ul: ({ children }) => <span className="mdi-block">{children}</span>,
  ol: ({ children }) => <span className="mdi-block">{children}</span>,
  li: ({ children }) => <span className="mdi-item">{children}</span>,

  // A fenced block and an inline span both become inline code: the
  // point of showing code in a preview is that it is code.
  pre: ({ children }) => <span className="mdi-block">{children}</span>,
  code: ({ children }) => <code className="mdi-code">{children}</code>,

  // Text only. The row itself is the link.
  a: ({ children }) => <span>{children}</span>,

  img: () => null,
  hr: () => null,
  br: () => <span> </span>,

  // Tables flatten to their cells. A preview of a table is not a
  // table, and rendering one inside two clamped lines produces a
  // column of single characters.
  table: ({ children }) => <span className="mdi-block">{children}</span>,
  thead: () => null,
  tbody: ({ children }) => <span>{children}</span>,
  tr: ({ children }) => <span className="mdi-item">{children}</span>,
  td: ({ children }) => <span className="mdi-cell">{children}</span>,
  th: ({ children }) => <span className="mdi-cell">{children}</span>,
};

/**
 * Repair markdown cut mid-syntax.
 *
 * Previews arrive pre-truncated, and the cut lands wherever it lands
 * — often inside `**bold` or an unclosed backtick. Rendered as-is the
 * marker shows up literally, which is the thing this component
 * exists to stop. Closing what was left open is cheaper than asking
 * every producer of a preview to cut carefully.
 */
export function closeOpenMarks(text: string): string {
  let out = text;
  // Fences first: an unclosed one swallows everything after it.
  if ((out.match(/^```/gm) ?? []).length % 2 === 1) out += "\n```";
  // Then inline runs, longest marker first so `**` is not read as two
  // single `*`.
  for (const mark of ["**", "`", "*", "_"]) {
    const count = out.split(mark).length - 1;
    if (count % 2 === 1) out += mark;
  }
  return out;
}

/**
 * Drop a leading line that only repeats the document's name.
 *
 * Most documents open with their own title. In a list the name is
 * already the row's first line, so rendering it again spends the
 * preview — the two lines somebody reads to tell this document from
 * the next one — saying what they just read.
 *
 * Heading or plain line: the duplication is the same either way, and
 * whether the author marked it up is not the reader's problem. The
 * line has to *be* the name, though, not merely open with it —
 * "Session handling is a signed JWT flow" is the first sentence of
 * the document and belongs in the preview.
 */
export function dropEchoedTitle(text: string, name: string): string {
  const title = name.trim().toLowerCase();
  if (!title) return text;
  const lines = text.split("\n");
  let i = 0;
  while (i < lines.length && !lines[i].trim()) i++;
  const first = lines[i]?.trim() ?? "";
  const heading = first.replace(/^#{1,6}\s+/, "").trim();
  // Only when it *is* the title, not when it merely starts with it —
  // "Session handling in detail" is a different heading.
  if (heading.toLowerCase() !== title) return text;
  return lines
    .slice(i + 1)
    .join("\n")
    .trimStart();
}

export function MarkdownInline({
  text,
  /** The document's name, so a title echo can be dropped. */
  name = "",
  /** Characters to keep. The clamp is visual; this bounds the work. */
  limit = 320,
  className = "",
}: {
  text: string;
  name?: string;
  limit?: number;
  className?: string;
}) {
  const source = useMemo(() => {
    const body = dropEchoedTitle(String(text || ""), name);
    if (body.length <= limit) return closeOpenMarks(body);
    // Cut back to a space so the preview does not end mid-word.
    const cut = body.slice(0, limit);
    const space = cut.lastIndexOf(" ");
    return (
      closeOpenMarks(space > limit * 0.6 ? cut.slice(0, space) : cut) + "…"
    );
  }, [text, name, limit]);

  if (!source.trim()) return null;

  return (
    <span className={className ? `mdi ${className}` : "mdi"}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={INLINE_COMPONENTS}>
        {source}
      </ReactMarkdown>
    </span>
  );
}
