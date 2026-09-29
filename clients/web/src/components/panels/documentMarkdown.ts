/**
 * Getting a document into a rich-text editor and back out unharmed.
 *
 * Two problems, both of which are silent when they go wrong.
 *
 * **Structure the editor does not know about is destroyed.** A
 * markdown table parsed by an editor with no table support comes
 * back as its cells run together — "SettingValueNotesMax sessions
 * per user10" — and nothing raises. Ten of this project's seventy
 * versions contain a table. So the extension list is not a default:
 * it is the list of things these documents actually contain.
 *
 * **Re-wrapping makes every edit look total.** The editor holds a
 * paragraph as a paragraph, not as the lines somebody typed, so
 * serialising gives one long line per paragraph. Where a document
 * was hard-wrapped at seventy columns, saving a one-word change
 * rewrites every line in it — and this app has a Compare versions
 * view that would then show the whole document as changed, which is
 * the same as showing nothing.
 *
 * Re-wrapping the output does not fix it, because there is no single
 * width to re-wrap to: most prose lines in this store are 140-200
 * characters and a minority are hard-wrapped at seventy. Both are
 * somebody's document. So instead each block that *survived the trip
 * unchanged* is written back exactly as it arrived, and only blocks
 * that really were edited take the editor's wrapping.
 */

import { Image } from "@tiptap/extension-image";
import { TaskItem, TaskList } from "@tiptap/extension-list";
import { Table, TableCell, TableHeader, TableRow } from "@tiptap/extension-table";
import StarterKit from "@tiptap/starter-kit";
import { Markdown } from "tiptap-markdown";

/**
 * What the editor is allowed to understand.
 *
 * Not a default list. Every entry past StarterKit is here because
 * these documents contain it, and anything they contain that is
 * missing here is destroyed in silence — a table without
 * ``Table`` comes back as its cells run together. Adding a document
 * that uses something new means adding it here, and the round-trip
 * test over a real document is what notices.
 */
export const DOCUMENT_EXTENSIONS = [
  StarterKit,
  Markdown,
  Table,
  TableRow,
  TableHeader,
  TableCell,
  // Both added when the toolbar started offering them, and both for
  // the same reason as ``Table``: a task list came back with its
  // brackets escaped — "- \[ \] todo" — and an image came back as
  // nothing at all. A button that inserts something the save then
  // deletes is worse than no button.
  TaskList,
  TaskItem.configure({ nested: true }),
  Image,
];

/**
 * The document, as markdown.
 *
 * ``tiptap-markdown`` hangs its serialiser off ``editor.storage``
 * without declaring it, so the cast lives here rather than at each
 * call site — one unchecked access with a reason beside it, instead
 * of three scattered ones.
 */
export function markdownOf(editor: { storage: unknown }): string {
  const storage = editor.storage as { markdown?: { getMarkdown(): string } };
  return storage.markdown?.getMarkdown() ?? "";
}

/** A paragraph, heading, list, table or fence — anything blank-line separated. */
type Block = string;

/** Split on blank lines, keeping fenced code whole. */
export function blocksOf(markdown: string): Block[] {
  const out: Block[] = [];
  let fence: string | null = null;
  let current: string[] = [];

  const flush = () => {
    if (current.length) out.push(current.join("\n"));
    current = [];
  };

  for (const line of markdown.split("\n")) {
    const opens = line.match(/^\s*(```+|~~~+)/);
    if (fence) {
      current.push(line);
      // Closed only by a fence of the same kind. A ``` inside a ~~~
      // block is content, and treating it as a close swallows the
      // rest of the document into a paragraph.
      if (opens && opens[1][0] === fence[0]) {
        fence = null;
        flush();
      }
      continue;
    }
    if (opens) {
      flush();
      fence = opens[1];
      current.push(line);
      continue;
    }
    if (line.trim() === "") flush();
    else current.push(line);
  }
  flush();
  return out;
}

/**
 * What a block says, ignoring how it was wrapped.
 *
 * Only for *comparing* two blocks. Never write this back — it throws
 * away the line breaks that are the whole point of keeping the
 * original.
 */
export function sameContent(a: Block, b: Block): boolean {
  const flat = (block: string) =>
    block
      // A blockquote wraps with a marker on every line; the marker
      // is structure, the line break is not.
      .replace(/^>\s?/gm, "")
      .replace(/\s+/g, " ")
      .trim();
  // Fences compare literally: whitespace inside code is content.
  const fenced = (block: string) => /^\s*(```|~~~)/.test(block);
  if (fenced(a) || fenced(b)) return a.trim() === b.trim();
  return flat(a) === flat(b);
}

/**
 * The editor's output, with untouched blocks written as they were.
 *
 * Matching is positional first and then by content, because a
 * document usually comes back with the same blocks in the same
 * order — and falling straight to a content search would let an
 * edited paragraph adopt the wrapping of an identical paragraph
 * somewhere else in the document.
 */
export function preserveWrapping(original: string, edited: string): string {
  const was = blocksOf(original);
  const now = blocksOf(edited);
  const spare = new Map<string, Block>();
  was.forEach((block, i) => {
    if (!sameContent(block, now[i] ?? "")) spare.set(flatKey(block), block);
  });

  const kept = now.map((block, i) => {
    if (sameContent(block, was[i] ?? "")) return was[i];
    const moved = spare.get(flatKey(block));
    if (moved !== undefined) {
      // Used once: two paragraphs that read the same are still two
      // paragraphs, and the second must not borrow the first's
      // original text after the first has claimed it.
      spare.delete(flatKey(block));
      return moved;
    }
    return block;
  });

  const body = kept.join("\n\n");
  // The trailing newline is a property of the file, not of the last
  // paragraph, and dropping it is a one-character diff on every save.
  return original.endsWith("\n") ? `${body}\n` : body;
}

function flatKey(block: Block): string {
  return block
    .replace(/^>\s?/gm, "")
    .replace(/\s+/g, " ")
    .trim();
}
