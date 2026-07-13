/**
 * Tests for the FE visualizer streaming reducer.
 *
 * Covers the delta-to-spec transformation that runs on every
 * ``visualization_delta`` orchestrate event from the BE:
 *
 * - ``parsePartialSpec`` returns null for junk / incomplete input
 * - Progressive deltas produce a card that updates in place
 * - Multiple spec_ids produce distinct cards
 * - The final delta's spec matches a fresh parse of the same JSON
 * - Real token-by-token stream shapes converge to the expected Spec
 */

import { describe, expect, it } from "vitest";
import type { ChatItem } from "./model";
import {
  applyVisualizationDelta,
  parsePartialSpec,
} from "./visualizationStream";

// ── parsePartialSpec ────────────────────────────────────────────────

describe("parsePartialSpec", () => {
  it("rejects empty / whitespace", () => {
    expect(parsePartialSpec("")).toBeNull();
    expect(parsePartialSpec("   ")).toBeNull();
    expect(parsePartialSpec("\n\n")).toBeNull();
  });

  it("rejects bare '{' with nothing else", () => {
    // Not enough shape yet — no root, no elements. partial-json
    // parses '{' to {} which lacks both required keys.
    expect(parsePartialSpec("{")).toBeNull();
  });

  it("accepts a partial spec with the required keys", () => {
    const spec = parsePartialSpec('{"root":"r","elements":{"r":{"type":"Card"');
    expect(spec).not.toBeNull();
    expect(spec!.root).toBe("r");
    expect(spec!.elements.r).toBeDefined();
  });

  it("accepts a complete spec", () => {
    const spec = parsePartialSpec('{"root":"r","elements":{"r":{"type":"Text","props":{"text":"hi"}}}}');
    expect(spec).not.toBeNull();
    expect(spec!.root).toBe("r");
  });

  it("rejects specs missing root", () => {
    expect(parsePartialSpec('{"elements":{}}')).toBeNull();
  });

  it("rejects specs missing elements", () => {
    expect(parsePartialSpec('{"root":"r"}')).toBeNull();
  });

  it("rejects array / non-object payloads", () => {
    expect(parsePartialSpec('["not","a","spec"]')).toBeNull();
    expect(parsePartialSpec('"literal string"')).toBeNull();
    expect(parsePartialSpec("42")).toBeNull();
  });

  it("rejects non-string root", () => {
    expect(parsePartialSpec('{"root":42,"elements":{}}')).toBeNull();
    expect(parsePartialSpec('{"root":null,"elements":{}}')).toBeNull();
  });

  it("rejects when root's element hasn't streamed in yet", () => {
    // The Renderer walks starting from ``elements[root]``. If root
    // isn't a resolvable id in the elements map, the walk crashes.
    // We must not return a Spec until the root element materializes.
    expect(
      parsePartialSpec('{"root":"root","elements":{"chart":{"type":"LineGraph"}}}'),
    ).toBeNull();
  });

  it("prunes unresolved children so mid-stream Card+chart doesn't crash", () => {
    // The model streamed ``root`` referencing ``chart`` but
    // ``chart`` hasn't landed yet. Instead of returning a spec that
    // crashes the renderer, we prune the unresolved child id.
    const spec = parsePartialSpec(
      '{"root":"root","elements":{"root":{"type":"Card","children":["chart"]}}}',
    );
    expect(spec).not.toBeNull();
    const rootEl = spec!.elements.root as { children?: string[] };
    expect(rootEl.children).toEqual([]);
  });

  it("keeps children that DO resolve", () => {
    const spec = parsePartialSpec(
      '{"root":"root","elements":{"root":{"type":"Card","children":["chart"]},"chart":{"type":"LineGraph"}}}',
    );
    expect(spec).not.toBeNull();
    const rootEl = spec!.elements.root as { children?: string[] };
    expect(rootEl.children).toEqual(["chart"]);
  });
});

// ── applyVisualizationDelta ────────────────────────────────────────

describe("applyVisualizationDelta — reducer", () => {
  it("returns same items ref when the delta can't yet parse", () => {
    const items: ChatItem[] = [];
    const result = applyVisualizationDelta(items, {
      spec_id: "run-1",
      json: "{",
    });
    expect(result).toBe(items); // same ref — nothing to update
  });

  it("creates a new visualization card on first parseable delta", () => {
    const items: ChatItem[] = [];
    const result = applyVisualizationDelta(items, {
      spec_id: "run-1",
      json: '{"root":"r","elements":{"r":{"type":"Text","props":{"text":"x"}}}}',
    });
    expect(result.length).toBe(1);
    expect(result[0].kind).toBe("visualization");
    const card = result[0] as Extract<ChatItem, { kind: "visualization" }>;
    expect(card.specId).toBe("run-1");
    expect(card.sourceAgent).toBe("visualizer");
    expect((card.spec as { root: string }).root).toBe("r");
  });

  it("updates the same card on subsequent deltas with same spec_id", () => {
    let items: ChatItem[] = [];
    items = applyVisualizationDelta(items, {
      spec_id: "run-1",
      json: '{"root":"r","elements":{"r":{"type":"Card"',
    });
    const originalId = (items[0] as Extract<ChatItem, { kind: "visualization" }>).id;
    items = applyVisualizationDelta(items, {
      spec_id: "run-1",
      json: '{"root":"r","elements":{"r":{"type":"Card","props":{"title":"Hi"}}}}',
    });
    expect(items.length).toBe(1);
    const card = items[0] as Extract<ChatItem, { kind: "visualization" }>;
    // Same identity — it's an update, not an append.
    expect(card.id).toBe(originalId);
    // Spec updated with new props.
    const props = (card.spec as {
      elements: { r: { props: { title?: string } } };
    }).elements.r.props;
    expect(props.title).toBe("Hi");
  });

  it("keeps distinct cards for distinct spec_ids", () => {
    let items: ChatItem[] = [];
    items = applyVisualizationDelta(items, {
      spec_id: "run-1",
      json: '{"root":"r","elements":{"r":{"type":"Text","props":{"text":"x"}}}}',
    });
    items = applyVisualizationDelta(items, {
      spec_id: "run-2",
      json: '{"root":"r","elements":{"r":{"type":"Text","props":{"text":"x"}}}}',
    });
    expect(items.length).toBe(2);
    expect(items[0].kind).toBe("visualization");
    expect(items[1].kind).toBe("visualization");
    expect((items[0] as Extract<ChatItem, { kind: "visualization" }>).specId).toBe("run-1");
    expect((items[1] as Extract<ChatItem, { kind: "visualization" }>).specId).toBe("run-2");
  });

  it("returns same ref when spec_id is empty", () => {
    const items: ChatItem[] = [];
    const result = applyVisualizationDelta(items, {
      spec_id: "",
      json: '{"root":"r","elements":{"r":{"type":"Text","props":{"text":"x"}}}}',
    });
    expect(result).toBe(items);
  });
});

// ── Realistic token-by-token stream ────────────────────────────────

describe("token-by-token stream converges to the target spec", () => {
  it("progressive deltas end at the same spec as a one-shot parse", () => {
    const target =
      '{"root":"root","elements":{"root":{"type":"Card","props":{"title":"AAPL"},"children":["chart"]},"chart":{"type":"LineGraph","props":{"data":[{"x":"Jan","y":143},{"x":"Feb","y":147.41}]},"children":[]}}}';

    let items: ChatItem[] = [];
    // Deliver as growing prefixes (mimics the BE's accumulated
    // string throttled every ~50ms).
    let cursor = 0;
    while (cursor < target.length) {
      cursor = Math.min(target.length, cursor + 12); // ~12 chars per delta
      items = applyVisualizationDelta(items, {
        spec_id: "run-a",
        json: target.slice(0, cursor),
      });
    }
    // Final delta:
    items = applyVisualizationDelta(items, {
      spec_id: "run-a",
      json: target,
      final: true,
    });

    expect(items.length).toBe(1);
    const card = items[0] as Extract<ChatItem, { kind: "visualization" }>;
    // The final spec should equal the raw JSON.parse of target.
    expect(card.spec).toEqual(JSON.parse(target));
  });

  it("intermediate deltas produce valid Spec references (never null once opened)", () => {
    const target = '{"root":"r","elements":{"r":{"type":"Text","props":{"text":"hello world"}}}}';
    let items: ChatItem[] = [];
    // After the first delta that opens root+elements, every
    // subsequent delta should keep the card alive (not remove it,
    // not turn spec into null).
    let sawFirstCard = false;
    for (let n = 1; n <= target.length; n++) {
      items = applyVisualizationDelta(items, {
        spec_id: "run-a",
        json: target.slice(0, n),
      });
      const card = items.find(
        (it) => it.kind === "visualization",
      ) as Extract<ChatItem, { kind: "visualization" }> | undefined;
      if (card) {
        sawFirstCard = true;
        expect(card.spec).not.toBeNull();
        expect((card.spec as { root: string }).root).toBe("r");
      }
    }
    expect(sawFirstCard).toBe(true);
  });
});
