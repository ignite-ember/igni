/**
 * Tests for ``computeThumb`` — the pure scroll-thumb geometry
 * helper inside ``ScrollIndicator``.
 *
 * The math drives the macOS-style overlay scrollbar we render on
 * top of the frosted headers. Bugs here are visual but subtle —
 * a thumb that grows past the track, or one that doesn't touch
 * the bottom when scrolled to the end, looks "almost right" and
 * regresses silently. Pin each property.
 */

import { describe, expect, it } from "vitest";
import {
  computeThumb,
  SCROLL_INDICATOR_MIN_THUMB,
  SCROLL_INDICATOR_PAD,
} from "./ScrollIndicator";

describe("computeThumb — visibility", () => {
  it("hides the thumb when the list fits exactly in the viewport", () => {
    // Same scrollHeight + clientHeight → nothing to scroll → no
    // thumb. The ``+1`` slack in the source absorbs sub-pixel
    // rounding so this doesn't flicker.
    expect(computeThumb(0, 600, 600).visible).toBe(false);
  });

  it("hides the thumb when scrollHeight is smaller than clientHeight", () => {
    // Defensive — shouldn't happen, but if it does (e.g. an
    // initial render before content paints), don't render a
    // weird zero-height thumb.
    expect(computeThumb(0, 400, 600).visible).toBe(false);
  });

  it("absorbs sub-pixel rounding (height + 1)", () => {
    // ``clientHeight = scrollHeight - 1`` is what you see in
    // jsdom + Chrome when a flex layout rounds by 1px. The
    // thumb must NOT appear in that state.
    expect(computeThumb(0, 600, 599).visible).toBe(false);
    // 2px difference IS scrollable.
    expect(computeThumb(0, 602, 600).visible).toBe(true);
  });

  it("is visible when there's anything to scroll", () => {
    // Sanity baseline — long list, small viewport.
    expect(computeThumb(0, 5000, 600).visible).toBe(true);
  });
});

describe("computeThumb — top position", () => {
  it("starts at PAD when scrolled to the top", () => {
    // The thumb floats at ``top: PAD`` (32px by default) so it
    // never touches the header edge.
    expect(computeThumb(0, 5000, 600).top).toBe(SCROLL_INDICATOR_PAD);
  });

  it("ends at track bottom when scrolled to the bottom", () => {
    // Load-bearing — the bottom of the thumb must visually align
    // with the bottom of the track when the user reaches the end
    // of the list. Otherwise the indicator lies.
    const rect = computeThumb(/* scrolled to end */ 5000 - 600, 5000, 600);
    // top + height = clientHeight - PAD (i.e. the track bottom).
    expect(rect.top + rect.height).toBeCloseTo(600 - SCROLL_INDICATOR_PAD, 6);
  });

  it("scales linearly with scrollTop between top and bottom", () => {
    // At 50% scrolled, the thumb's top should be at the
    // midpoint between PAD and (track-bottom - thumbHeight).
    const mid = computeThumb((5000 - 600) / 2, 5000, 600);
    const start = computeThumb(0, 5000, 600);
    const end = computeThumb(5000 - 600, 5000, 600);
    const expectedMidTop = (start.top + end.top) / 2;
    expect(mid.top).toBeCloseTo(expectedMidTop, 6);
  });
});

describe("computeThumb — height", () => {
  it("scales the thumb height by visible-portion ratio", () => {
    // ratio = clientHeight / scrollHeight = 600 / 5000 = 0.12.
    // thumb = clientHeight * ratio = 600 * 0.12 = 72. Within
    // [min=24, max=track=536], so it lands at 72 unclamped.
    const rect = computeThumb(0, 5000, 600);
    expect(rect.height).toBeCloseTo(72, 6);
  });

  it("clamps to the minimum (24px) on extremely tall lists", () => {
    // Without the min, a 100k-pixel list would give a thumb
    // height of ~3px — visually invisible and impossible to
    // grab. The 24px floor keeps the affordance usable.
    const rect = computeThumb(0, 100_000, 600);
    expect(rect.height).toBe(SCROLL_INDICATOR_MIN_THUMB);
  });

  it("doesn't exceed the available track length", () => {
    // Edge case: if the ratio puts the thumb taller than the
    // track, we'd overflow the track top + bottom. Clamp to
    // ``track`` so the indicator stays inside its bounds.
    // Construct a case where ratio * clientHeight > track:
    // clientHeight=100, track=100-64=36, scrollHeight just
    // big enough to be scrollable → ratio≈0.97, ratio*cH≈97.
    // Should cap at 36.
    const rect = computeThumb(0, 103, 100, /* pad */ 32);
    expect(rect.height).toBeLessThanOrEqual(36);
  });
});

describe("computeThumb — small-viewport edge cases", () => {
  it("track of zero (PAD eats whole viewport) yields a 24px thumb at the pad offset", () => {
    // Viewport smaller than 2*PAD → ``track`` clamps to 0.
    // The thumb is still given its minimum height (24); it
    // overflows the track but a measurable thumb still beats
    // a 0-height invisible one for the user.
    const rect = computeThumb(0, 200, 50, 32);
    expect(rect.visible).toBe(true);
    expect(rect.height).toBe(SCROLL_INDICATOR_MIN_THUMB);
    expect(rect.top).toBe(32);
  });

  it("custom pad override changes the offset and track length", () => {
    // The pad is a parameter (default 32). Verify the formula
    // still works for a non-default value — e.g. an embedded
    // surface with tighter chrome.
    const rect = computeThumb(0, 5000, 600, 10);
    expect(rect.top).toBe(10);
  });

  it("handles scrollTop > maxScroll without exploding (defensive)", () => {
    // Some browsers report transient ``scrollTop`` values just
    // past the limit during inertial scroll. The formula
    // shouldn't return NaN or jump out of the track.
    const rect = computeThumb(/* over-shoot */ 99_999, 5000, 600);
    expect(Number.isFinite(rect.top)).toBe(true);
    expect(rect.top + rect.height).toBeGreaterThan(SCROLL_INDICATOR_PAD);
  });
});
