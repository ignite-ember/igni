/**
 * The small glyphs beside a menu item.
 *
 * A menu of five verbs reads as five equal options, and picking the
 * wrong one of Archive and Remove costs quite differently. An icon is
 * what makes a list scannable: the shape is recognised before the
 * word is read, so the item somebody wants is found by looking rather
 * than by reading the list from the top each time.
 *
 * **One set, one grammar.** All of them are 16×16, drawn in strokes
 * of the same weight on the same grid, and coloured ``currentColor``
 * so a disabled item's icon dims with its label and a destructive
 * one turns red with it. Mixing a filled glyph into a stroked set is
 * the single most obvious way to make a menu look assembled from
 * parts.
 *
 * Inline rather than a sprite or a font: these are a dozen paths used
 * in three menus, and a build step, a network request or a font with
 * a flash of missing glyphs are all more machinery than the problem
 * has.
 */

export type MenuIconName =
  | "open"
  | "rename"
  | "export"
  | "share"
  | "archive"
  | "restore"
  | "remove"
  | "document"
  | "document-add"
  | "folder"
  | "folder-add"
  | "outdent"
  | "compare"
  | "lineage"
  // Formatting. Drawn in the same grammar as the rest so the
  // editor's bar and the document's menus look like one product.
  | "code"
  | "list-bullet"
  | "list-ordered"
  | "list-task"
  | "quote"
  | "code-block"
  | "table"
  | "divider"
  | "link"
  | "image"
  | "undo"
  | "redo";

/** The drawing for each name, on a 16×16 grid. */
const PATHS: Record<MenuIconName, string> = {
  // A page with a corner turned.
  open: "M9 1.5H4.5A1.5 1.5 0 0 0 3 3v10a1.5 1.5 0 0 0 1.5 1.5h7A1.5 1.5 0 0 0 13 13V5.5zM9 1.5V5.5h4",
  // A pencil across a line.
  rename: "M11.2 2.3 13.7 4.8 6.2 12.3 3 13l.7-3.2zM2.5 14.5h11",
  // Out of a tray, downwards — the file leaves.
  export: "M8 2v7m0 0 2.8-2.8M8 9 5.2 6.2M2.8 10.5v2.2a1 1 0 0 0 1 1h8.4a1 1 0 0 0 1-1v-2.2",
  // Two nodes joined to a third: one thing, several people.
  share: "M5.6 9.3 10.4 12M10.4 4 5.6 6.7M12 2.2a1.8 1.8 0 1 1 0 3.6 1.8 1.8 0 0 1 0-3.6zM4 6.2a1.8 1.8 0 1 1 0 3.6 1.8 1.8 0 0 1 0-3.6zM12 10.2a1.8 1.8 0 1 1 0 3.6 1.8 1.8 0 0 1 0-3.6z",
  // A box with a lid, and a handle on the drawer.
  archive: "M2 3.2h12v2.6H2zM3 5.8v7a.8.8 0 0 0 .8.8h8.4a.8.8 0 0 0 .8-.8v-7M6.4 8.4h3.2",
  // The same box, with the arrow coming back up out of it.
  restore: "M2 3.2h12v2.6H2zM3 5.8v7a.8.8 0 0 0 .8.8h8.4a.8.8 0 0 0 .8-.8v-7M8 11.6V7.4m0 0L6.2 9.2M8 7.4l1.8 1.8",
  // A bin. Deliberately the least like the archive of any of these.
  remove: "M2.8 4.3h10.4M6.4 4.3V2.9a.7.7 0 0 1 .7-.7h1.8a.7.7 0 0 1 .7.7v1.4M4.2 4.3l.6 8.5a.9.9 0 0 0 .9.8h4.6a.9.9 0 0 0 .9-.8l.6-8.5M6.7 6.8v4.2M9.3 6.8v4.2",
  // A page. The same shape as ``open`` without the action.
  document: "M9 1.5H4.5A1.5 1.5 0 0 0 3 3v10a1.5 1.5 0 0 0 1.5 1.5h7A1.5 1.5 0 0 0 13 13V5.5zM9 1.5V5.5h4M5.5 8.5h5M5.5 11h3",
  // The same page and folder, with the plus that makes one. Drawn
  // from the same outlines rather than new shapes: "new document"
  // and "document" should be recognisably the same noun.
  "document-add":
    "M8.4 1.5H4.5A1.5 1.5 0 0 0 3 3v10a1.5 1.5 0 0 0 1.5 1.5h3.2M8.4 1.5V5.5h3.6M11.8 8.8v4.6M9.5 11.1h4.6",
  folder: "M1.8 4.2a1 1 0 0 1 1-1h3.1l1.4 1.6h5.9a1 1 0 0 1 1 1v6.4a1 1 0 0 1-1 1H2.8a1 1 0 0 1-1-1z",
  "folder-add":
    "M1.8 4.2a1 1 0 0 1 1-1h3.1l1.4 1.6h5.9a1 1 0 0 1 1 1v1.5M1.8 4.2v8.4a1 1 0 0 0 1 1h4.9M11.4 8.8v4.6M9.1 11.1h4.6",
  // Up and to the left: out of where it is, to the level above.
  outdent: "M13 4H6M13 8H8.5M13 12H6M4.5 5.5 2 8l2.5 2.5",
  // Two columns, side by side.
  compare: "M6.4 2.5v11M9.6 2.5v11M2 2.5h12v11H2zM2 2.5",
  // Chevrons, the way code is written about.
  code: "M5.8 4.6 1.8 8l4 3.4M10.2 4.6 14.2 8l-4 3.4",
  // Three dots with three lines. The dots are round rather than
  // square so a bullet list and a checklist are not the same
  // silhouette at a glance.
  "list-bullet":
    "M6.2 4.2h7.6M6.2 8h7.6M6.2 11.8h7.6M2.6 3.6a.6.6 0 1 1 0 1.2.6.6 0 0 1 0-1.2zM2.6 7.4a.6.6 0 1 1 0 1.2.6.6 0 0 1 0-1.2zM2.6 11.2a.6.6 0 1 1 0 1.2.6.6 0 0 1 0-1.2z",
  "list-ordered": "M6.4 4.2h7.4M6.4 8h7.4M6.4 11.8h7.4M2 3.2h1v2.4M1.7 5.6h1.8M1.7 9.6h2l-2 2.6h2.2",
  // A tick in a box, against the bullet list's round dots.
  "list-task":
    "M6.6 4.2h7.2M6.6 11.8h7.2M1.4 2.7h3.4v3.4H1.4zM1.4 10.3h3.4v3.4H1.4zM2.1 11.9l.9.9 1.4-1.6",
  // The bar a quote is set behind, with the lines it holds.
  quote: "M2.6 3.4v9.2M5.8 5h8M5.8 8h8M5.8 11h5.4",
  // A fence: the chevrons again, boxed.
  "code-block":
    "M1.8 3.2h12.4v9.6H1.8zM6.2 6.4 4.4 8l1.8 1.6M9.8 6.4 11.6 8l-1.8 1.6",
  // A grid with its header row set apart, which is what makes a
  // table a table rather than a box with lines in it.
  table: "M1.8 3.2h12.4v9.6H1.8zM1.8 6.4h12.4M6.4 6.4v6.4M10.2 6.4v6.4",
  divider: "M2 8h12",
  // A chain of two links, at an angle.
  link: "M6.6 9.4 9.4 6.6M6.9 4.6 8.4 3.1a2.6 2.6 0 0 1 3.7 3.7l-1.5 1.5M9.1 11.4l-1.5 1.5a2.6 2.6 0 0 1-3.7-3.7l1.5-1.5",
  // A frame with a horizon and a sun: recognisable at this size in
  // a way a literal photograph is not.
  image: "M1.8 3.2h12.4v9.6H1.8zM1.8 10.4l3.4-3.2 3 2.8 2.2-2 3.8 3.4M10.6 5.6a.9.9 0 1 1 0 1.8.9.9 0 0 1 0-1.8z",
  undo: "M4.2 6.6H10a3.4 3.4 0 0 1 0 6.8H6.6M4.2 6.6l2.6-2.6M4.2 6.6l2.6 2.6",
  redo: "M11.8 6.6H6a3.4 3.4 0 0 0 0 6.8h3.4M11.8 6.6 9.2 4M11.8 6.6 9.2 9.2",
  // A node with two feeding into it: what this was built on.
  lineage: "M3.6 4.2h3.1a2 2 0 0 1 2 2v3.6a2 2 0 0 0 2 2h1.7M3.6 11.8h3.1M1.4 2.8a1.4 1.4 0 1 1 0 2.8 1.4 1.4 0 0 1 0-2.8zM1.4 10.4a1.4 1.4 0 1 1 0 2.8 1.4 1.4 0 0 1 0-2.8zM13.2 10.4a1.4 1.4 0 1 1 0 2.8 1.4 1.4 0 0 1 0-2.8z",
};

export interface MenuIconProps {
  name: MenuIconName;
  className?: string;
}

export function MenuIcon({ name, className }: MenuIconProps) {
  const d = PATHS[name];
  // An unknown name draws nothing rather than a broken box. The
  // label is the thing that says what the item does; the icon is how
  // it is found again.
  if (!d) return null;
  return (
    <svg
      className={className ? `menu-icon ${className}` : "menu-icon"}
      viewBox="0 0 16 16"
      aria-hidden="true"
      focusable="false"
    >
      <path d={d} />
    </svg>
  );
}
