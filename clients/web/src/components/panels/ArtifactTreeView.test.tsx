// @vitest-environment jsdom
/**
 * The two pure decisions behind the tree view.
 *
 * Collapsing has to hide a whole branch, not one row — a child left
 * on screen under a folded parent reads as living at the top level.
 *
 * And a drag has to know what it is carrying, because dropping a
 * branch inside itself detaches it from every root: everything on it
 * disappears from navigation while still being in the store. Computing
 * that here rather than asking the server is what lets the drop be
 * refused while the pointer is still moving.
 */
import { describe, expect, it } from "vitest";

import { branchOf, visibleRows, type TreeNode } from "./ArtifactTreeView";

const node = (id: string, depth: number, over: Partial<TreeNode> = {}): TreeNode => ({
  node_id: id,
  name: id,
  kind: "design",
  is_folder: false,
  head_n: 1,
  depth,
  descendants: 0,
  placeholder: false,
  ...over,
});

//  design/
//    auth/
//      login
//      sessions
//    rate-limits
//  checklist
const TREE = [
  node("design", 0, { is_folder: true }),
  node("auth", 1),
  node("login", 2),
  node("sessions", 2),
  node("rate-limits", 1),
  node("checklist", 0),
];

describe("what a drag is carrying", () => {
  it("is the row and everything under it", () => {
    expect([...branchOf(TREE, 1)]).toEqual(["auth", "login", "sessions"]);
  });

  it("stops at the next row of the same depth", () => {
    // "rate-limits" is a sibling of "auth", not part of it.
    expect([...branchOf(TREE, 1)]).not.toContain("rate-limits");
  });

  it("is just the row when nothing is under it", () => {
    expect([...branchOf(TREE, 5)]).toEqual(["checklist"]);
  });

  it("is the whole tree from a root that holds everything", () => {
    expect([...branchOf(TREE, 0)]).toEqual([
      "design",
      "auth",
      "login",
      "sessions",
      "rate-limits",
    ]);
  });

  it("is empty for a row that is not there", () => {
    // ``findIndex`` answers -1 when the dragged node has gone, and
    // an empty forbidden set would make every drop look legal.
    expect(branchOf(TREE, -1).size).toBe(0);
  });
});

describe("what a collapsed branch hides", () => {
  it("hides descendants at every depth, not just children", () => {
    const shown = visibleRows(TREE, new Set(["design"]));
    expect(shown.map((n) => n.node_id)).toEqual(["design", "checklist"]);
  });

  it("hides one level without hiding its siblings", () => {
    const shown = visibleRows(TREE, new Set(["auth"]));
    expect(shown.map((n) => n.node_id)).toEqual([
      "design",
      "auth",
      "rate-limits",
      "checklist",
    ]);
  });

  it("shows everything when nothing is collapsed", () => {
    expect(visibleRows(TREE, new Set()).length).toBe(TREE.length);
  });

  it("does not hide a deeper row that is merely later", () => {
    // The naive version stops hiding at the first shallower row and
    // then forgets to resume, so a second branch after a collapsed
    // one vanishes.
    const wide = [
      node("a", 0, { is_folder: true }),
      node("a1", 1),
      node("b", 0, { is_folder: true }),
      node("b1", 1),
    ];
    expect(visibleRows(wide, new Set(["a"])).map((n) => n.node_id)).toEqual([
      "a",
      "b",
      "b1",
    ]);
  });

  it("handles two collapsed branches at once", () => {
    const shown = visibleRows(TREE, new Set(["design", "auth"]));
    expect(shown.map((n) => n.node_id)).toEqual(["design", "checklist"]);
  });
});
