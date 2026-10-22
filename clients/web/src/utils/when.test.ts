/**
 * How long ago, in as few characters as a rail can spare.
 *
 * Shared by the document header and the comments beside it, which is
 * the point: two implementations drift the moment one of them learns
 * about weeks, and a version stamped "8d ago" next to a comment on it
 * stamped "1w ago" reads as two different days.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { formatExact, formatRelative } from "./when";

/** Freeze the clock so "ago" means something a test can assert. */
function at(iso: string) {
  vi.useFakeTimers();
  vi.setSystemTime(new Date(iso));
}

afterEach(() => {
  vi.useRealTimers();
});

describe("saying how long ago", () => {
  it("calls the last minute just now", () => {
    at("2026-09-29T12:00:30Z");
    expect(formatRelative("2026-09-29T12:00:00Z")).toBe("just now");
  });

  it("counts minutes, then hours, then days", () => {
    at("2026-09-29T12:00:00Z");
    expect(formatRelative("2026-09-29T11:30:00Z")).toBe("30m ago");
    expect(formatRelative("2026-09-29T09:00:00Z")).toBe("3h ago");
    expect(formatRelative("2026-09-26T12:00:00Z")).toBe("3d ago");
    expect(formatRelative("2026-07-29T12:00:00Z")).toBe("2mo ago");
  });

  it("does not count backwards when a clock disagrees", () => {
    // The server stamps the comment and the browser reads it. A
    // machine a few seconds behind puts events in the future, and
    // "in -3 seconds" is worse than the small lie that it has just
    // happened.
    at("2026-09-29T12:00:00Z");
    expect(formatRelative("2026-09-29T12:00:05Z")).toBe("just now");
  });

  it("says nothing about a comment with no date", () => {
    // Rather than "just now", which would date every undated thing
    // to the moment it was looked at.
    expect(formatRelative("")).toBe("");
  });

  it("hands back something unparseable rather than NaN", () => {
    expect(formatRelative("not a date")).toBe("not a date");
  });
});

describe("the exact moment, for a tooltip", () => {
  it("gives a date and a time", () => {
    const shown = formatExact("2026-09-27T16:11:09Z");

    expect(shown).toMatch(/2026/);
    expect(shown).not.toBe("2026-09-27T16:11:09Z");
  });

  it("leaves an unparseable value alone", () => {
    expect(formatExact("whenever")).toBe("whenever");
  });

  it("says nothing about nothing", () => {
    expect(formatExact("")).toBe("");
  });
});
