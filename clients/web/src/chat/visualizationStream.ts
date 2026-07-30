/**
 * FE-side reducer for the visualizer streaming path.
 *
 * BE broadcasts ``visualization_delta`` orchestrate events with the
 * ACCUMULATED JSON string as each ~50ms window closes. This module
 * partial-parses that string into a Spec and reduces it against the
 * items list — first delta creates a card, subsequent deltas update
 * the same card by ``spec_id``.
 *
 * Kept separate from App.tsx so the logic is testable with a
 * scripted delta tape. The App just plumbs events into
 * ``applyVisualizationDelta`` and lets it own the item update.
 */

import type { Spec } from "@json-render/core";
import { parse as parsePartialJson, Allow } from "partial-json";
import type { ChatItem } from "./model";
import { visualizationItem } from "./model";

export interface VisualizationDeltaEvent {
  spec_id: string;
  json: string;
  /** ``true`` on the last delta of the stream so callers can trigger
   *  persistence exactly once. */
  final?: boolean;
}

/** Attempt to partial-parse the accumulated JSON into a Spec that
 *  is SAFE for @json-render/react's Renderer to walk.
 *
 *  "Safe" means: root resolves to an element in the map, and every
 *  child id referenced from an element's ``children`` array also
 *  resolves. If either invariant is broken, the Renderer throws
 *  (which crashes the surrounding React tree — the bug this
 *  function prevents).
 *
 *  Returns null when the current buffer can't yet be coerced into
 *  a safe spec. Callers keep the previous good spec on screen
 *  until a new safe one arrives — that's what makes progressive
 *  streaming look smooth instead of flickering broken frames.
 */
export function parsePartialSpec(json: string): Spec | null {
  let partial: unknown;
  try {
    partial = parsePartialJson(json, Allow.ALL);
  } catch {
    return null;
  }
  if (
    !partial ||
    typeof partial !== "object" ||
    Array.isArray(partial) ||
    !("root" in (partial as Record<string, unknown>)) ||
    !("elements" in (partial as Record<string, unknown>))
  ) {
    return null;
  }
  const root = (partial as { root: unknown }).root;
  const elementsRaw = (partial as { elements: unknown }).elements;
  if (typeof root !== "string" || !root) return null;
  if (
    !elementsRaw ||
    typeof elementsRaw !== "object" ||
    Array.isArray(elementsRaw)
  ) {
    return null;
  }
  const elements = elementsRaw as Record<string, unknown>;
  // Renderer requires ``elements[root]`` to be an object with a
  // ``type`` string. Partial-json may leave it as null or missing.
  const rootEl = elements[root];
  if (
    !rootEl ||
    typeof rootEl !== "object" ||
    Array.isArray(rootEl) ||
    typeof (rootEl as { type?: unknown }).type !== "string"
  ) {
    return null;
  }
  // Prune child-ids that don't yet resolve so a mid-stream Card
  // pointing at ``["chart"]`` doesn't crash when ``chart`` hasn't
  // been streamed in yet. We mutate a shallow copy so the caller's
  // input (the raw partial-json object) is unaffected.
  const safeElements: Record<string, unknown> = {};
  for (const [id, el] of Object.entries(elements)) {
    if (!el || typeof el !== "object" || Array.isArray(el)) continue;
    if (typeof (el as { type?: unknown }).type !== "string") continue;
    const rawChildren = (el as { children?: unknown }).children;
    let children: string[] | undefined;
    if (Array.isArray(rawChildren)) {
      children = rawChildren.filter(
        (c): c is string => typeof c === "string" && c in elements,
      );
    }
    safeElements[id] = children ? { ...el, children } : el;
  }
  if (!safeElements[root]) return null;
  return { ...(partial as Record<string, unknown>), elements: safeElements } as Spec;
}

/** Fold one visualization_delta into the current items list.
 *  Returns the new items list — same reference if nothing changed
 *  (e.g. an early delta that can't yet be partial-parsed). Callers
 *  can shallow-compare and skip re-render.
 */
export function applyVisualizationDelta(
  items: ChatItem[],
  event: VisualizationDeltaEvent,
): ChatItem[] {
  if (!event.spec_id || typeof event.json !== "string") return items;
  const spec = parsePartialSpec(event.json);
  if (!spec) return items;

  const idx = items.findIndex(
    (it) => it.kind === "visualization" && it.specId === event.spec_id,
  );
  if (idx >= 0) {
    const existing = items[idx] as Extract<
      ChatItem,
      { kind: "visualization" }
    >;
    const next = items.slice();
    next[idx] = { ...existing, spec };
    return next;
  }
  return [
    ...items,
    visualizationItem(spec, "", "visualizer", event.spec_id),
  ];
}
