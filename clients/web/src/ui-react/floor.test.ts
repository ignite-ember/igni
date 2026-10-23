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
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

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
  "Spinner",
  "Switch",
  "TabPanel",
  "Table",
  "Tabs",
  "Text",
  "Textarea",
  "Toolbar",
  "Tooltip",
  "fenceInfo",
  "loadMermaid",
];

/**
 * The exported *types*, which the runtime check above cannot see.
 *
 * `Object.keys(floor)` lists values. A type export leaves no trace at
 * runtime, so deleting one — or forgetting to add one for a new
 * component — compiles, passes, and silently removes the only way an
 * extension can name a prop bag to write a wrapper around it. That is
 * a breaking change under §8 exactly as removing a component is.
 *
 * Read from source for that reason. `BadgeProps` and `ChipProps` were
 * declared and not exported for as long as this file only counted
 * values.
 */
const PUBLISHED_TYPES = [
  "BadgeProps",
  "ButtonProps",
  "ButtonSize",
  "ButtonVariant",
  "CheckboxProps",
  "ChipProps",
  "DialogProps",
  "EmptyProps",
  "FieldProps",
  "HeadingProps",
  "InputProps",
  "MenuIconName",
  "MenuIconProps",
  "OverflowAction",
  "ProgressProps",
  "RadioProps",
  "SelectProps",
  "SpinnerProps",
  "SwitchProps",
  "TabPanelProps",
  "TabsProps",
  "TextProps",
  "TextTone",
  "TextareaProps",
  "Tone",
  "TooltipProps",
];

describe("the published types", () => {
  it("are exactly these", () => {
    const source = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "index.ts"), "utf8");
    const found = [...source.matchAll(/\btype (\w+)/g)].map((m) => m[1]);
    expect([...new Set(found)].sort()).toEqual(PUBLISHED_TYPES);
  });
});

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
