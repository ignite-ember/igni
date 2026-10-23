// @vitest-environment jsdom
/**
 * The glyphs beside menu items.
 *
 * Worth a test for one reason above the rest: Archive and Remove sit
 * next to each other in the same menu and mean opposite things. One
 * keeps every version and every edge into the document resolving;
 * the other takes them. If those two ever draw the same shape, the
 * menu is actively misleading, and it is the kind of wrong nobody
 * reports — they just press the wrong one.
 */
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { MenuIcon, type MenuIconName } from "./MenuIcon";

afterEach(cleanup);

const ALL: MenuIconName[] = [
  "open",
  "rename",
  "export",
  "share",
  "archive",
  "restore",
  "remove",
  "document",
  "document-add",
  "folder",
  "folder-add",
  "outdent",
  "compare",
  "lineage",
  "code",
  "list-bullet",
  "list-ordered",
  "list-task",
  "quote",
  "code-block",
  "table",
  "divider",
  "link",
  "image",
  "undo",
  "redo",
  "resolve",
  "reopen",
];

function drawing(name: MenuIconName): string {
  const { container } = render(<MenuIcon name={name} />);
  return container.querySelector("path")?.getAttribute("d") ?? "";
}

describe("the set", () => {
  it("draws something for every name", () => {
    for (const name of ALL) {
      expect(drawing(name), name).not.toBe("");
      cleanup();
    }
  });

  it("gives every one of them a different shape", () => {
    // A set where two names share a path is a set where one of them
    // was pasted and not finished.
    const seen = new Map<string, MenuIconName>();
    for (const name of ALL) {
      const d = drawing(name);
      expect(seen.has(d), `${name} draws the same as ${seen.get(d)}`).toBe(
        false,
      );
      seen.set(d, name);
      cleanup();
    }
  });

  it("keeps archive and remove apart", () => {
    // Stated on its own, because this is the pair that costs.
    expect(drawing("archive")).not.toBe(drawing("remove"));
  });

  it("draws nothing rather than a broken box for a name it has not got", () => {
    const { container } = render(
      <MenuIcon name={"nonesuch" as MenuIconName} />,
    );

    expect(container.firstChild).toBeNull();
  });
});

describe("how it is drawn", () => {
  it("is hidden from a screen reader, which has the label already", () => {
    const { container } = render(<MenuIcon name="archive" />);

    expect(container.querySelector("svg")?.getAttribute("aria-hidden")).toBe(
      "true",
    );
  });

  it("takes the colour of the text it labels", () => {
    // ``currentColor``, so a destructive item's glyph turns red with
    // its label and a disabled one dims with it, rather than sitting
    // at full strength beside greyed-out text.
    const { container } = render(<MenuIcon name="remove" />);
    const svg = container.querySelector("svg");

    expect(svg?.getAttribute("class")).toContain("menu-icon");
    expect(svg?.getAttribute("fill")).not.toBe("currentColor");
  });

  it("is square, on one grid", () => {
    // Mixed viewBoxes are how a set ends up with one glyph visibly
    // heavier than its neighbours.
    for (const name of ALL) {
      const { container } = render(<MenuIcon name={name} />);
      expect(
        container.querySelector("svg")?.getAttribute("viewBox"),
        name,
      ).toBe("0 0 16 16");
      cleanup();
    }
  });
});
