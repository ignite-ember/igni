/**
 * Tests for the two pages built rather than converted.
 *
 * `HelpPage` groups commands by hand, and a hand-written grouping is a
 * second list beside `BUILTIN_COMMANDS` — it goes stale the first time
 * somebody adds a command and does not think to place it. The check
 * below is the thing that notices.
 *
 * `ContextPage`'s arithmetic is worth pinning for its degenerate
 * cases: an empty session divides by zero, and tokenizers have been
 * known to disagree with themselves enough that the parts exceed the
 * whole.
 */

import { describe, expect, it } from "vitest";

import { BUILTIN_COMMANDS } from "../Composer";
import { groupingDrift } from "./HelpPage";
import { shares } from "./ContextPage";

describe("the help grouping", () => {
  it("places every built-in command", () => {
    // A command missing from help because nobody updated the grouping
    // is invisible: help is where you look to find out a command
    // exists at all.
    expect(groupingDrift(BUILTIN_COMMANDS).ungrouped).toEqual([]);
  });

  it("does not name commands that no longer exist", () => {
    expect(groupingDrift(BUILTIN_COMMANDS).missing).toEqual([]);
  });

  it("reports both directions of drift", () => {
    // Pin the detector itself, or the two assertions above pass
    // because it never returns anything.
    const drift = groupingDrift([
      { name: "/clear", description: "" },
      { name: "/invented", description: "" },
    ]);
    expect(drift.ungrouped).toContain("/invented");
    // ``/accept`` is placed by the grouping and absent from the list
    // passed in, which is what "missing" means. ``/clear`` is in both
    // and so is in neither result.
    expect(drift.missing).toContain("/accept");
    expect(drift.missing).not.toContain("/clear");
  });
});

describe("the context split", () => {
  it("divides the parts by the total", () => {
    const s = shares({ total: 1000, runs: 750, floor: 250 }, 10_000);
    expect(s.runsPct).toBeCloseTo(75);
    expect(s.floorPct).toBeCloseTo(25);
  });

  it("measures usage against the window, not the total", () => {
    // The number that says whether a compact is due.
    const s = shares({ total: 1000, runs: 750, floor: 250 }, 4000);
    expect(s.usedPct).toBeCloseTo(25);
  });

  it("is all zeroes for an empty session rather than NaN", () => {
    const s = shares({ total: 0, runs: 0, floor: 0 }, 10_000);
    expect(s.runsPct).toBe(0);
    expect(s.floorPct).toBe(0);
    expect(s.usedPct).toBe(0);
  });

  it("survives a window of zero", () => {
    // ``max_context`` is 0 before the first status arrives.
    expect(shares({ total: 500, runs: 500, floor: 0 }, 0).usedPct).toBe(0);
  });

  it("clamps a part that exceeds the whole", () => {
    // The floor is already clamped backend-side, but a bar wider than
    // its track is a rendering bug either way.
    const s = shares({ total: 100, runs: 150, floor: 0 }, 100);
    expect(s.runsPct).toBe(100);
  });
});
