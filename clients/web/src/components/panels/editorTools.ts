/**
 * What the editor can do, as a list.
 *
 * Defined apart from the toolbar that draws it so the same set feeds
 * the bar at the top, the bubble over a selection, and the tests —
 * three renderings of one list rather than three lists that drift.
 *
 * **Every entry survives being saved.** That is the entry
 * requirement, not a nice-to-have: the document is stored as
 * markdown, so a button offering something markdown cannot carry is
 * a button that quietly deletes the thing it just inserted. Task
 * lists and images were both in that state — one came back with its
 * brackets escaped, the other came back as nothing — and are in the
 * list now because the extensions that carry them were added.
 * ``documentMarkdown.test.ts`` is what keeps that true.
 *
 * Underline is deliberately absent. Markdown has no underline, so
 * the only honest options were to leave it out or to store HTML in
 * a markdown document, and a button that turns a document into a
 * different format is not a formatting button.
 */
import type { Editor } from "@tiptap/core";

import type { MenuIconName } from "../MenuIcon";

export interface EditorTool {
  key: string;
  /** Said in the tooltip and to a screen reader. */
  label: string;
  /** A drawn icon, for the tools that are about structure. */
  icon?: MenuIconName;
  /** A letterform, for the tools that are about type.
   *
   *  Bold, italic and the heading levels are shown as letters
   *  because that is what they are — an icon of a bold letter B is
   *  a picture of the letter it could simply be. They are styled to
   *  look like what they do: the B is bold, the I is italic, the S
   *  is struck through. */
  glyph?: string;
  /** The keystroke, for the tooltip. Shown as written. */
  keys?: string;
  /** Whether it is currently on. */
  isOn: (editor: Editor) => boolean;
  run: (editor: Editor) => void;
  /** Greyed rather than hidden when it cannot run. */
  can?: (editor: Editor) => boolean;
}

export interface ToolGroup {
  key: string;
  tools: EditorTool[];
}

const mark = (
  key: string,
  label: string,
  shown: { glyph?: string; icon?: MenuIconName },
  name: string,
  keys?: string,
): EditorTool => ({
  key,
  label,
  ...shown,
  keys,
  isOn: (editor) => editor.isActive(name),
  run: (editor) => editor.chain().focus().toggleMark(name).run(),
});

/**
 * Grouped as somebody reaches for them: what a word looks like, what
 * a block is, what goes in one, and undoing it.
 */
export const TOOL_GROUPS: ToolGroup[] = [
  {
    key: "marks",
    tools: [
      mark("bold", "Bold", { glyph: "B" }, "bold", "⌘B"),
      mark("italic", "Italic", { glyph: "I" }, "italic", "⌘I"),
      mark("strike", "Strikethrough", { glyph: "S" }, "strike"),
      mark("code", "Inline code", { icon: "code" }, "code", "⌘E"),
    ],
  },
  {
    key: "headings",
    tools: [1, 2, 3].map((level) => ({
      key: `h${level}`,
      label: `Heading ${level}`,
      glyph: `H${level}`,
      keys: `⌘⌥${level}`,
      isOn: (editor: Editor) => editor.isActive("heading", { level }),
      run: (editor: Editor) =>
        editor.chain().focus().toggleHeading({ level: level as 1 | 2 | 3 }).run(),
    })),
  },
  {
    key: "lists",
    tools: [
      {
        key: "bulletList",
        label: "Bulleted list",
        icon: "list-bullet",
        isOn: (editor) => editor.isActive("bulletList"),
        run: (editor) => editor.chain().focus().toggleBulletList().run(),
      },
      {
        key: "orderedList",
        label: "Numbered list",
        icon: "list-ordered",
        isOn: (editor) => editor.isActive("orderedList"),
        run: (editor) => editor.chain().focus().toggleOrderedList().run(),
      },
      {
        key: "taskList",
        label: "Checklist",
        icon: "list-task",
        isOn: (editor) => editor.isActive("taskList"),
        run: (editor) => editor.chain().focus().toggleTaskList().run(),
      },
    ],
  },
  {
    key: "blocks",
    tools: [
      {
        key: "blockquote",
        label: "Quote",
        icon: "quote",
        isOn: (editor) => editor.isActive("blockquote"),
        run: (editor) => editor.chain().focus().toggleBlockquote().run(),
      },
      {
        key: "codeBlock",
        label: "Code block",
        icon: "code-block",
        isOn: (editor) => editor.isActive("codeBlock"),
        run: (editor) => editor.chain().focus().toggleCodeBlock().run(),
      },
      {
        key: "table",
        label: "Table",
        icon: "table",
        isOn: (editor) => editor.isActive("table"),
        run: (editor) =>
          editor.chain().focus().insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run(),
      },
      {
        key: "rule",
        label: "Divider",
        icon: "divider",
        isOn: () => false,
        run: (editor) => editor.chain().focus().setHorizontalRule().run(),
      },
    ],
  },
  {
    key: "history",
    tools: [
      {
        key: "undo",
        label: "Undo",
        icon: "undo",
        keys: "⌘Z",
        isOn: () => false,
        can: (editor) => editor.can().undo(),
        run: (editor) => editor.chain().focus().undo().run(),
      },
      {
        key: "redo",
        label: "Redo",
        icon: "redo",
        keys: "⇧⌘Z",
        isOn: () => false,
        can: (editor) => editor.can().redo(),
        run: (editor) => editor.chain().focus().redo().run(),
      },
    ],
  },
];

/** The marks and headings, for the bar that floats over a selection. */
export const SELECTION_TOOLS: EditorTool[] = TOOL_GROUPS.filter((group) =>
  ["marks", "headings"].includes(group.key),
).flatMap((group) => group.tools);

/** Every tool, flat. */
export const ALL_TOOLS: EditorTool[] = TOOL_GROUPS.flatMap((group) => group.tools);
