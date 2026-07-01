/**
 * Pure-helper tests for the find-in-conversation bar (CC parity
 * row 42). Three helpers escape the React closure and are worth
 * locking down:
 *
 *   • formatTurnTime  — relative timestamps for the result row
 *   • cleanSnippetText — strip wrapper tags / code-paste / @code
 *     pills out of snippet halves so the user sees what they
 *     actually wrote, not the persisted-message scaffolding
 *   • translateIndex  — bounds-aware BE→FE index mapping
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanSnippetText,
  formatTurnTime,
  translateIndex,
} from "./ChatSearchBar";

// ── formatTurnTime ──────────────────────────────────────────

describe("formatTurnTime", () => {
  // Anchor "now" to a fixed timestamp so the relative buckets land
  // deterministically — the helper calls ``Date.now()`` internally.
  const NOW_EPOCH_SEC = 1_750_000_000; // some Tue 2025-06-15
  const NOW_MS = NOW_EPOCH_SEC * 1000;

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(NOW_MS));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("renders 0 as empty (no persisted timestamp on this turn)", () => {
    // The BE writes 0 for turns that pre-date the ``created_at``
    // column. Showing "55y ago" would be noise — empty is right.
    expect(formatTurnTime(0)).toBe("");
  });

  it("returns 'just now' for sub-minute deltas", () => {
    expect(formatTurnTime(NOW_EPOCH_SEC - 30)).toBe("just now");
    expect(formatTurnTime(NOW_EPOCH_SEC - 59)).toBe("just now");
  });

  it("returns minute granularity up to 60m", () => {
    expect(formatTurnTime(NOW_EPOCH_SEC - 60)).toBe("1m ago");
    expect(formatTurnTime(NOW_EPOCH_SEC - 2 * 60)).toBe("2m ago");
    expect(formatTurnTime(NOW_EPOCH_SEC - 59 * 60)).toBe("59m ago");
  });

  it("rolls over to hours at the 60-minute boundary", () => {
    expect(formatTurnTime(NOW_EPOCH_SEC - 60 * 60)).toBe("1h ago");
    expect(formatTurnTime(NOW_EPOCH_SEC - 23 * 60 * 60)).toBe("23h ago");
  });

  it("rolls over to days at 24h, within the week", () => {
    expect(formatTurnTime(NOW_EPOCH_SEC - 24 * 60 * 60)).toBe("1d ago");
    expect(formatTurnTime(NOW_EPOCH_SEC - 6 * 24 * 60 * 60)).toBe("6d ago");
  });

  it("falls back to a locale-formatted date past 7 days", () => {
    // 8-day-old turn must NOT show as "8d ago" — the relative
    // labels get harder to scan after a week. Past that, just
    // print the date.
    const out = formatTurnTime(NOW_EPOCH_SEC - 8 * 24 * 60 * 60);
    expect(out).not.toMatch(/d ago/);
    // Format is "Jun 7, 04:26 PM" / "7 Jun, 16:26" depending on
    // locale — just verify it's non-empty and not the relative
    // shape.
    expect(out).not.toBe("");
    expect(out).not.toMatch(/^\d+[mhd] ago$/);
    expect(out).not.toBe("just now");
  });

  it("handles future timestamps (clock skew) with bare time-of-day", () => {
    // BE clock 30s ahead of FE clock — diff goes negative. Showing
    // "negative 30s ago" would be jarring; bare HH:MM keeps the
    // signal value of "this turn just landed".
    const out = formatTurnTime(NOW_EPOCH_SEC + 30);
    // Time-of-day form (matches both "04:26 PM" and "16:26").
    expect(out).toMatch(/\d{1,2}:\d{2}/);
    expect(out).not.toMatch(/ago/);
  });
});

// ── cleanSnippetText ────────────────────────────────────────

describe("cleanSnippetText", () => {
  it("drops a <system-context>...</system-context> block entirely", () => {
    // The BE wraps the per-turn context envelope in this tag;
    // restored items strip it before render. The search snippet
    // must match what the user sees — never leak "Current datetime:".
    const before =
      "<system-context>Current datetime: 2026-06-28</system-context> real ask";
    expect(cleanSnippetText(before).trim()).toBe("real ask");
  });

  it("drops a multi-line <attached-files>...</attached-files> block", () => {
    const before =
      "<attached-files>\n[Referenced files: a.py, b.py]\n</attached-files>\nthe ask";
    expect(cleanSnippetText(before).trim()).toBe("the ask");
  });

  it("strips bare <think> / </think> markers, keeps their inner text", () => {
    // ``<think>`` brackets are dropped but the inner monologue
    // text stays — search should still hit on words inside.
    expect(cleanSnippetText("<think>reasoning here</think> answer")).toBe(
      "reasoning here answer",
    );
  });

  it("strips <loop-iteration ...> tags with attributes", () => {
    expect(
      cleanSnippetText('<loop-iteration index="3">body</loop-iteration> tail'),
    ).toBe("body tail");
  });

  it("collapses [code-paste …] … [/code-paste] blocks to '[code]'", () => {
    // Multi-line pasted source code shouldn't appear verbatim in
    // a one-line snippet — render as a single marker so the user
    // still knows there was a paste at that point.
    const before =
      "before [code-paste lang=python]\ndef foo():\n    return 1\n[/code-paste] after";
    expect(cleanSnippetText(before)).toBe("before [code] after");
  });

  it("renames @code:<id> tokens to '[code]'", () => {
    // Same intent as the paste collapse — short, friendly token
    // instead of an opaque id.
    expect(cleanSnippetText("see @code:abc123 there")).toBe("see [code] there");
  });

  it("strips markdown bold/italic markers, keeps inner text", () => {
    expect(cleanSnippetText("a **bold** word")).toBe("a bold word");
    expect(cleanSnippetText("an _italic_ word")).toBe("an italic word");
    expect(cleanSnippetText("__strong__ and *em*")).toBe("strong and em");
  });

  it("strips inline-code backticks but keeps content", () => {
    expect(cleanSnippetText("call `foo()` here")).toBe("call foo() here");
  });

  it("strips leading heading hashes per line", () => {
    // The snippet may straddle a heading. ``# Foo`` is noisy in a
    // one-line context — just show ``Foo``.
    expect(cleanSnippetText("# Heading\nbody text")).toBe("Heading body text");
  });

  it("collapses any whitespace run (incl. newlines/tabs) to a single space", () => {
    expect(cleanSnippetText("a\n\nb\t\tc   d")).toBe("a b c d");
  });

  it("leaves clean prose untouched (just whitespace collapse)", () => {
    // Negative-space: the helper must not aggressively rewrite
    // plain text. Search results would lose recognisability if
    // it did.
    expect(cleanSnippetText("plain English sentence.")).toBe(
      "plain English sentence.",
    );
  });
});

// ── translateIndex ──────────────────────────────────────────

describe("translateIndex", () => {
  // Map shape: ``historyIndexToItemIndex[historyIdx] = itemIdx``,
  // -1 for history turns the FE skipped (e.g. system_context-only
  // turns ``restoredItem`` filters out).
  const MAP = [0, 1, -1, 2, 3]; // history idx 2 dropped at restore
  const LIVE = 8; // 5 from history + 3 appended live

  it("maps a known history index to its item index", () => {
    expect(translateIndex(0, MAP, LIVE)).toBe(0);
    expect(translateIndex(3, MAP, LIVE)).toBe(2);
    expect(translateIndex(4, MAP, LIVE)).toBe(3);
  });

  it("returns -1 for filtered-out history turns (map value = -1)", () => {
    // The match landed on a turn the FE chose to hide. Callers
    // treat -1 as "result exists but can't be jumped to" and
    // grey out the row.
    expect(translateIndex(2, MAP, LIVE)).toBe(-1);
  });

  it("returns -1 for negative history indices", () => {
    expect(translateIndex(-1, MAP, LIVE)).toBe(-1);
  });

  it("returns -1 when the history index overflows the map", () => {
    // History grew after the FE took its snapshot — the BE knows
    // about turns the FE doesn't have a mapping for yet. Don't
    // jump to a wrong row; show "can't jump".
    expect(translateIndex(99, MAP, LIVE)).toBe(-1);
  });

  it("returns -1 when the mapped item index exceeds liveItemCount", () => {
    // Live items got truncated (e.g. ``/clear`` after the search
    // ran). The stored mapping is now stale; refuse to point at a
    // non-existent row.
    expect(translateIndex(4, MAP, /* shrank to */ 2)).toBe(-1);
  });

  it("works with an empty map (no history loaded yet)", () => {
    expect(translateIndex(0, [], 0)).toBe(-1);
  });
});
