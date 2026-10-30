// @vitest-environment jsdom
/**
 * A card an extension appends (`host.chat.append(view, title)`) is
 * labelled with the extension's name and carries its title — as it
 * arrives, and after a reload restores it from the history. Only the
 * visualizer's own cards say "via visualizer".
 */

import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { ChatItemView } from "../components/ChatItems";
import { ExtensionNamesContext } from "../extensions/extensionNames";
import { restoredItem, type ChatItem } from "./model";
import {
  applyVisualizationDelta,
  visualizationDeltaFromPayload,
} from "./visualizationStream";

afterEach(() => cleanup());

const SPEC = { root: "t", elements: { t: { type: "Text", props: { text: "3 open" } } } };
const NAMES = new Map([["acme.claims", "Claims desk"]]);

/** What `chat.append` pushes on the `orchestrate_event` channel. */
const APPENDED = {
  type: "visualization_delta",
  agent_path: "ext:acme.claims",
  spec_id: "ext-0123456789ab",
  json: JSON.stringify(SPEC),
  final: true,
  title: "Open claims",
  source: "acme.claims",
};

function renderCard(item: ChatItem) {
  render(
    <ExtensionNamesContext.Provider value={NAMES}>
      <ChatItemView item={item} />
    </ExtensionNamesContext.Provider>,
  );
}

describe("a card an extension appended", () => {
  it("shows the extension's name and the title as it arrives", () => {
    const event = visualizationDeltaFromPayload(APPENDED);
    expect(event).not.toBeNull();
    const [item] = applyVisualizationDelta([], event!);
    renderCard(item);
    expect(screen.getByText("Open claims")).toBeTruthy();
    expect(screen.getByText("via Claims desk")).toBeTruthy();
    expect(screen.queryByText("via visualizer")).toBeNull();
  });

  it("shows the extension's name and the title after a reload", () => {
    const item = restoredItem({
      role: "visualization",
      spec_id: "ext-0123456789ab",
      spec: SPEC,
      title: "Open claims",
      source_agent: "acme.claims",
      extension_id: "acme.claims",
    });
    expect(item).not.toBeNull();
    renderCard(item!);
    expect(screen.getByText("Open claims")).toBeTruthy();
    expect(screen.getByText("via Claims desk")).toBeTruthy();
    expect(screen.queryByText("via visualizer")).toBeNull();
  });

  it("keeps its title when a replace sends none", () => {
    const first = applyVisualizationDelta([], visualizationDeltaFromPayload(APPENDED)!);
    const replaced = applyVisualizationDelta(
      first,
      visualizationDeltaFromPayload({ ...APPENDED, title: undefined })!,
    );
    expect(replaced[0].kind === "visualization" && replaced[0].title).toBe("Open claims");
  });
});

describe("a card the visualizer drew", () => {
  it("still says via visualizer", () => {
    const event = visualizationDeltaFromPayload({
      type: "visualization_delta",
      agent_path: "main/visualizer",
      spec_id: "viz-1",
      json: JSON.stringify(SPEC),
    });
    const [item] = applyVisualizationDelta([], event!);
    renderCard(item);
    expect(screen.getByText("via visualizer")).toBeTruthy();
  });

  it("is not taken for an extension's on a reload", () => {
    const item = restoredItem({
      role: "visualization",
      spec_id: "viz-1",
      spec: SPEC,
      source_agent: "visualizer",
    });
    renderCard(item!);
    expect(screen.getByText("via visualizer")).toBeTruthy();
  });

  it("ignores a payload that is not a visualization delta", () => {
    expect(visualizationDeltaFromPayload({ type: "agent_started" })).toBeNull();
    expect(visualizationDeltaFromPayload(null)).toBeNull();
  });
});
