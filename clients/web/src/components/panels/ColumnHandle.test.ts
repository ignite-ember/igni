// @vitest-environment jsdom
/**
 * How wide a dragged column is allowed to get.
 *
 * The clamp is the part worth testing, because the failure it
 * prevents is one the reader has to drag their way back out of: two
 * columns pulled wide on a narrow laptop leave the document a few
 * characters across, and the thing you would reach for to fix it is
 * the document.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { allowedWidth, rememberedWidth } from "./ColumnHandle";

const MIN = 150;
const MAX = 520;

describe("clamping a dragged column", () => {
  it("leaves a sensible width alone", () => {
    expect(allowedWidth(300, MIN, MAX, 1600)).toBe(300);
  });

  it("will not go below the minimum", () => {
    expect(allowedWidth(20, MIN, MAX, 1600)).toBe(MIN);
  });

  it("will not go past the maximum", () => {
    expect(allowedWidth(9000, MIN, MAX, 1600)).toBe(MAX);
  });

  it("keeps a column off most of a narrow window", () => {
    // 40% of 800 is 320, below this column's own 520 maximum. The
    // window wins: two columns at 520 on an 800px screen leave
    // nothing for the thing they are docked beside.
    expect(allowedWidth(500, MIN, MAX, 800)).toBe(320);
  });

  it("but never below the minimum, however narrow the window", () => {
    // A column under its minimum is unreadable, and unreadable is
    // worse than wide. 40% of 300 is 120; the floor is 150.
    expect(allowedWidth(400, MIN, MAX, 300)).toBe(MIN);
  });

  it("answers whole pixels", () => {
    expect(Number.isInteger(allowedWidth(301.7, MIN, MAX, 1600))).toBe(true);
  });
});

describe("the width a column had last time", () => {
  // Stubbed rather than used. This runtime has no ``localStorage``
  // at all — node reports it "not available because
  // --localstorage-file was not provided" — which is itself the
  // reason the reader below is wrapped: a store can be missing, not
  // only failing.
  function store(initial: Record<string, string> = {}) {
    const values = new Map(Object.entries(initial));
    const fake = {
      getItem: (k: string) => values.get(k) ?? null,
      setItem: (k: string, v: string) => void values.set(k, v),
      removeItem: (k: string) => void values.delete(k),
    };
    Object.defineProperty(window, "localStorage", {
      value: fake,
      configurable: true,
      writable: true,
    });
    return fake;
  }

  afterEach(() => {
    Reflect.deleteProperty(window, "localStorage");
    vi.restoreAllMocks();
  });

  it("comes back as a number", () => {
    store({ k: "340" });
    expect(rememberedWidth("k")).toBe(340);
  });

  it("is nothing when the column has never been dragged", () => {
    store();
    expect(rememberedWidth("k")).toBeNull();
  });

  it("ignores a stored value that is not a width", () => {
    // Somebody else's key, a half-written value, a schema that moved
    // on. A column at its default is a working column.
    store({ k: "wide please" });
    expect(rememberedWidth("k")).toBeNull();
  });

  it("ignores a stored width of zero or less", () => {
    store({ k: "-40" });
    expect(rememberedWidth("k")).toBeNull();
  });

  it("survives a store that throws", () => {
    // Private browsing, a disabled store, a quota.
    store();
    vi.spyOn(window.localStorage, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });

    expect(rememberedWidth("k")).toBeNull();
  });

  it("survives there being no store at all", () => {
    // Which is this runtime, and a real browser with storage turned
    // off. Reading a preference must not be able to take the page
    // down.
    Reflect.deleteProperty(window, "localStorage");

    expect(rememberedWidth("k")).toBeNull();
  });
});
