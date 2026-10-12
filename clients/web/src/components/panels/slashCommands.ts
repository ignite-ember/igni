/**
 * Typing ``/`` to insert a block.
 *
 * The alternative to hunting a toolbar for a thing you already know
 * the name of. Somebody who wants a table types "/tab" and presses
 * Enter, and never moves their hands off the keys.
 *
 * **Only at the start of an empty block.** A slash in the middle of a
 * sentence is a slash — "and/or", a path, a date — and a menu that
 * opened there would fire constantly while somebody writes prose
 * about file paths, which these documents are full of.
 *
 * The menu itself is not drawn here. This extension decides *when*
 * there is a menu and *what is in it*, and hands that to a caller to
 * render; keeping the drawing out means the list can be a React
 * component with the app's own styling rather than a second
 * rendering system living inside the editor.
 */
import type { Editor, Range } from "@tiptap/core";
import { Extension } from "@tiptap/core";
import Suggestion from "@tiptap/suggestion";

export interface SlashItem {
  /** What the menu says. */
  label: string;
  /** Matched against what was typed after the slash. */
  keywords: string[];
  /** A hint at the right, for the ones a name does not explain. */
  detail?: string;
  run: (editor: Editor, range: Range) => void;
}

/** What the renderer is told, each time the menu should change. */
export interface SlashState {
  items: SlashItem[];
  /** Where the slash is, in viewport coordinates. */
  at: DOMRect | null;
  /** Which item the keyboard is on. */
  active: number;
  /** Run the item at an index, from a click. */
  choose: (index: number) => void;
}

export const SLASH_ITEMS: SlashItem[] = [
  {
    label: "Heading",
    keywords: ["h2", "heading", "title", "section"],
    run: (editor, range) =>
      editor.chain().focus().deleteRange(range).setNode("heading", { level: 2 }).run(),
  },
  {
    label: "Subheading",
    keywords: ["h3", "subheading", "subsection"],
    run: (editor, range) =>
      editor.chain().focus().deleteRange(range).setNode("heading", { level: 3 }).run(),
  },
  {
    label: "Bulleted list",
    keywords: ["bullet", "list", "ul", "points"],
    run: (editor, range) =>
      editor.chain().focus().deleteRange(range).toggleBulletList().run(),
  },
  {
    label: "Numbered list",
    keywords: ["number", "ordered", "ol", "steps"],
    run: (editor, range) =>
      editor.chain().focus().deleteRange(range).toggleOrderedList().run(),
  },
  {
    label: "Quote",
    keywords: ["quote", "blockquote", "note", "callout"],
    run: (editor, range) =>
      editor.chain().focus().deleteRange(range).toggleBlockquote().run(),
  },
  {
    label: "Code block",
    keywords: ["code", "fence", "snippet", "yaml", "json"],
    run: (editor, range) =>
      editor.chain().focus().deleteRange(range).toggleCodeBlock().run(),
  },
  {
    label: "Table",
    keywords: ["table", "grid", "rows", "columns"],
    detail: "3 × 3",
    run: (editor, range) =>
      editor
        .chain()
        .focus()
        .deleteRange(range)
        .insertTable({ rows: 3, cols: 3, withHeaderRow: true })
        .run(),
  },
  {
    label: "Divider",
    keywords: ["divider", "rule", "hr", "separator", "break"],
    run: (editor, range) =>
      editor.chain().focus().deleteRange(range).setHorizontalRule().run(),
  },
];

/** The items matching what was typed after the slash. */
export function matching(query: string, items: SlashItem[] = SLASH_ITEMS): SlashItem[] {
  const asked = query.trim().toLowerCase();
  if (!asked) return items;
  // Prefix, not substring. "co" should offer Code block; it should
  // not also offer Table because "columns" contains an o and a c
  // somewhere — a menu that matches loosely is a menu you have to
  // read, which is the thing the typing was avoiding.
  return items.filter(
    (item) =>
      item.label.toLowerCase().startsWith(asked) ||
      item.keywords.some((word) => word.startsWith(asked)),
  );
}

export interface SlashOptions {
  /** Called whenever the menu should appear, move, or go away. */
  onState: (state: SlashState | null) => void;
}

export function slashCommands({ onState }: SlashOptions) {
  return Extension.create({
    name: "slashCommands",

    addProseMirrorPlugins() {
      let active = 0;
      // Kept from the last onStart/onUpdate. In TipTap 3 the
      // keydown handler is told about the key and nothing else, so
      // the list it is navigating has to be remembered here.
      let shown: SlashItem[] = [];
      let insert: ((item: SlashItem) => void) | null = null;
      let where: DOMRect | null = null;

      return [
        Suggestion<SlashItem>({
          editor: this.editor,
          char: "/",
          // The block has to be empty. See the module note: these
          // documents are full of paths and "and/or".
          startOfLine: true,
          allowSpaces: false,
          items: ({ query }) => matching(query),

          render: () => ({
            onStart: (props) => {
              active = 0;
              remember(props.items, props.command, props.clientRect?.() ?? null);
            },
            onUpdate: (props) => {
              // Clamped rather than reset: narrowing the list with
              // another letter should leave the highlight where it
              // sensibly can, not jump it back to the top each time.
              active = Math.min(active, Math.max(0, props.items.length - 1));
              remember(props.items, props.command, props.clientRect?.() ?? null);
            },
            onKeyDown: ({ event }) => {
              if (event.key === "Escape") {
                onState(null);
                return true;
              }
              const count = shown.length;
              if (!count || !insert) return false;
              if (event.key === "ArrowDown") {
                active = (active + 1) % count;
              } else if (event.key === "ArrowUp") {
                active = (active - 1 + count) % count;
              } else if (event.key === "Enter" || event.key === "Tab") {
                insert(shown[active]);
                return true;
              } else {
                return false;
              }
              publish();
              return true;
            },
            onExit: () => {
              shown = [];
              insert = null;
              onState(null);
            },
          }),

          command: ({ editor, range, props }) => props.run(editor, range),
        }),
      ];

      function remember(
        items: SlashItem[],
        command: (item: SlashItem) => void,
        rect: DOMRect | null,
      ) {
        shown = items;
        insert = command;
        where = rect;
        publish();
      }

      function publish() {
        onState({
          items: shown,
          at: where,
          active,
          choose: (index) => insert?.(shown[index]),
        });
      }
    },
  });
}
