/**
 * The parts of a table the floor was missing.
 *
 * `Table` is a bare `<table>` and stays one — the markup inside is
 * the extension's, per EXTENSIBILITY §8, and a `DataTable` that owns
 * rows would have to own sorting, selection, paging and
 * virtualisation with it. What was missing is narrower and entirely
 * behaviour: a sortable header nobody announces wrongly, and a
 * selection state that reads as selected.
 *
 * `.ui-table` was three CSS declarations until now — width,
 * border-collapse, font-size — so cell padding, the header rule and
 * the sticky header were every extension's problem too. Those are in
 * `igni-ui.css` now; these are the components that drive them.
 */
import type { ReactNode, ThHTMLAttributes } from "react";

function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

/** Which way a column is sorted, or not. */
export type SortDirection = "ascending" | "descending";

export interface SortableThProps
  extends Omit<ThHTMLAttributes<HTMLTableCellElement>, "onClick" | "aria-sort"> {
  /** The heading. */
  children: ReactNode;
  /**
   * How this column is currently sorted, or `undefined` when the
   * table is sorted by some other column.
   */
  direction?: SortDirection;
  /** Asked to sort by this column. Gets the direction to go to. */
  onSort: (direction: SortDirection) => void;
  /** Right-align and use tabular figures. */
  numeric?: boolean;
}

/**
 * A column heading you can sort by.
 *
 * `aria-sort` goes on the `th` and nowhere else, which is what a
 * screen reader reads; the arrow is `aria-hidden` so the state is
 * not announced twice. Hand-rolled versions usually do the reverse —
 * an arrow glyph with no `aria-sort` — and the sort is then
 * invisible to the people most reliant on being told.
 *
 * The button fills the cell so the whole heading is the target
 * rather than a word inside it.
 */
export function SortableTh({
  children,
  direction,
  onSort,
  numeric = false,
  className,
  ...rest
}: SortableThProps) {
  // Clicking the sorted column reverses it; clicking any other
  // column starts ascending. Starting descending is defensible for
  // dates and wrong for everything else, so the caller re-sorts if
  // it wants that.
  const next: SortDirection = direction === "ascending" ? "descending" : "ascending";
  return (
    <th
      {...rest}
      scope="col"
      aria-sort={direction ?? "none"}
      className={cx(numeric && "ui-num", className)}
    >
      <button type="button" className="ui-th-sort" onClick={() => onSort(next)}>
        {children}
        <span className="ui-th-arrow" aria-hidden="true">
          {direction === "descending" ? "▾" : "▴"}
        </span>
      </button>
    </th>
  );
}

export interface StatusProps {
  /** The four states the health contract defines. */
  state: "READY" | "PREPARING" | "DISABLED" | "FAILED";
  /**
   * What to show beside the dot. Defaults to the state itself.
   *
   * There is always a word. The dot is a colour, and a colour on its
   * own is not a message — it says nothing when read aloud and
   * nothing to somebody who cannot tell green from amber.
   */
  children?: ReactNode;
}

/** A state, as a coloured dot and the word it stands for. */
export function Status({ state, children }: StatusProps) {
  return (
    <span className="ui-status" data-state={state}>
      {children ?? state}
    </span>
  );
}
