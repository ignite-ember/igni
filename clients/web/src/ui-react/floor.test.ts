/**
 * The published surface, pinned.
 *
 * Removing or renaming an export here is a breaking change under
 * EXTENSIBILITY.md §8, with a deprecation window measured in months.
 * Nothing else in this suite would notice one: a refactor that drops
 * an export compiles, passes, and breaks every extension at once.
 *
 * Adding is free — the list below is sorted, and a new name just
 * goes in. Removing one should be an argument, which is what having
 * to edit this file is for.
 */
import { describe, expect, it } from "vitest";

import * as floor from "./index";

const PUBLISHED = [
  "Actions",
  "Badge",
  "Button",
  "Card",
  "Checkbox",
  "Chip",
  "Code",
  "Dialog",
  "Divider",
  "Empty",
  "Field",
  "Heading",
  "Input",
  "Item",
  "List",
  "MarkdownInline",
  "MenuIcon",
  "MermaidBlock",
  "Monogram",
  "OverflowMenu",
  "Progress",
  "Radio",
  "Row",
  "Select",
  "Skeleton",
  "SpecBlock",
  "Spinner",
  "Switch",
  "TabPanel",
  "Table",
  "Tabs",
  "Text",
  "Textarea",
  "Toolbar",
  "Tooltip",
  "applyVisualizationDelta",
  "closeOpenMarks",
  "dropEchoedTitle",
  "fenceInfo",
  "hexOf",
  "hueOf",
  "initialsOf",
  "loadMermaid",
  "parsePartialSpec",
];

describe("the published surface", () => {
  it("is exactly this", () => {
    expect(Object.keys(floor).sort()).toEqual(PUBLISHED);
  });

  it("exports no component without a name", () => {
    // An anonymous component is unreadable in a stack trace and in
    // React DevTools, which is where an extension author debugs.
    for (const [name, value] of Object.entries(floor)) {
      if (typeof value === "function" && /^[A-Z]/.test(name)) {
        expect(value.name, `${name} is anonymous`).not.toBe("");
      }
    }
  });
});
