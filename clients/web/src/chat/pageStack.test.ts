/**
 * Tests for the page stack.
 *
 * The stack is what replaced a single `PanelState` slot, so the cases
 * worth pinning are the ones a single slot could not express at all:
 * going back one level, jumping to a breadcrumb, and surviving a
 * reload. Plus the two ways a persisted value can be wrong — it
 * crosses releases, so it will eventually hold a route kind that no
 * longer exists.
 */

import { describe, expect, it } from "vitest";

import {
  current,
  openRoot,
  pop,
  push,
  reset,
  restore,
  serialise,
  truncateTo,
  type PageRoute,
} from "./pageStack";

const plugins: PageRoute = { kind: "plugins", label: "Plugins" };
const detail: PageRoute = {
  kind: "plugins",
  label: "acme-tools",
  params: { id: "acme-tools" },
};
const knowledge: PageRoute = { kind: "knowledge", label: "Knowledge" };

describe("current", () => {
  it("is null for an empty stack — the chat is showing", () => {
    expect(current([])).toBeNull();
  });

  it("is the last entry; everything below is a breadcrumb", () => {
    expect(current([plugins, detail])).toEqual(detail);
  });
});

describe("openRoot", () => {
  it("opens a destination from the chat", () => {
    expect(openRoot(plugins)).toEqual([plugins]);
  });

  it("replaces one destination with another rather than nesting", () => {
    // `Plugins / Knowledge` would claim Knowledge lives inside
    // Plugins. They are siblings.
    expect(openRoot(knowledge)).toEqual([knowledge]);
  });

  it("returns to the top of the destination you are already in", () => {
    // Clicking "Plugins" in the header while three levels deep in
    // Plugins goes to the top of Plugins, not to a fourth level.
    expect(openRoot(plugins)).toEqual([plugins]);
  });
});

describe("push", () => {
  it("drills one level deeper", () => {
    // The case that caught the first version: a plugin's detail has
    // `kind: "plugins"` exactly like its parent, so a push that
    // switched on kind collapsed the drill-down into a reset.
    expect(push([plugins], detail)).toEqual([plugins, detail]);
  });

  it("keeps stacking", () => {
    const readme = { ...detail, label: "readme" };
    expect(push([plugins, detail], readme)).toEqual([plugins, detail, readme]);
  });
});

describe("pop", () => {
  it("goes back one level", () => {
    expect(pop([plugins, detail])).toEqual([plugins]);
  });

  it("returns to the chat from the root", () => {
    expect(pop([plugins])).toEqual([]);
  });

  it("is safe on an empty stack", () => {
    expect(pop([])).toEqual([]);
  });
});

describe("truncateTo", () => {
  it("drops everything above the crumb clicked", () => {
    const deep = [plugins, detail, { ...detail, label: "readme" }];
    expect(truncateTo(deep, 0)).toEqual([plugins]);
    expect(truncateTo(deep, 1)).toEqual([plugins, detail]);
  });

  it("treats a negative index as back to the chat", () => {
    expect(truncateTo([plugins, detail], -1)).toEqual([]);
  });
});

describe("reset", () => {
  it("goes back to the chat", () => {
    expect(reset()).toEqual([]);
  });
});

describe("persistence", () => {
  it("round-trips a stack", () => {
    const stack = [plugins, detail];
    expect(restore(serialise(stack))).toEqual(stack);
  });

  it("keeps params, which are what re-open a level", () => {
    expect(restore(serialise([detail]))[0].params).toEqual({ id: "acme-tools" });
  });

  it("restores nothing from an absent value", () => {
    expect(restore(null)).toEqual([]);
    expect(restore(undefined)).toEqual([]);
    expect(restore("")).toEqual([]);
  });

  it("restores nothing from a malformed value rather than throwing", () => {
    // A window that refuses to render because it cannot parse where it
    // used to be is worse than one that opens on the chat.
    expect(restore("{not json")).toEqual([]);
    expect(restore('{"kind":"plugins"}')).toEqual([]);
  });

  it("stops at a route kind it does not recognise", () => {
    // The value crosses releases: a kind that existed last version may
    // not exist now. The recognisable prefix still restores.
    const stored = JSON.stringify([plugins, { kind: "retired", label: "Gone" }]);
    expect(restore(stored)).toEqual([plugins]);
  });

  it("falls back to the kind when a label is missing", () => {
    const stored = JSON.stringify([{ kind: "plugins" }]);
    expect(restore(stored)).toEqual([{ kind: "plugins", label: "plugins" }]);
  });
});
