/**
 * What an extension may add to the app's navigation, and what it may not.
 *
 * The rules worth pinning are the refusals. An extension that can
 * shadow a base page, take a label that already means something, or
 * leave an entry behind after it is gone, is one that can quietly
 * change what a word in the UI does.
 */
import { describe, expect, it } from "vitest";

import { PageRegistry, restore } from "../chat/pageStack";
import { Contributions } from "./contributions";

const BASE_SIDEBAR = [
  { kind: "plugins", label: "Plugins" },
  { kind: "mcp", label: "MCPs" },
];
const BASE_TOOLS = [
  { label: "MCP servers", command: "/mcp", desc: "connect external tools" },
  { label: "CodeIndex", command: "/codeindex", desc: "semantic code search" },
];

function fresh() {
  return new Contributions(BASE_SIDEBAR, BASE_TOOLS, new PageRegistry());
}

describe("adding", () => {
  it("puts a contributed page in the sidebar when it asks to be there", () => {
    const c = fresh();

    const refused = c.install("acme.scanner", {
      pages: [{ kind: "acme.scan", label: "Scanner", sidebar: true }],
    });

    expect(refused).toEqual([]);
    expect(c.sidebar().map((l) => l.label)).toEqual([
      "Plugins",
      "MCPs",
      "Scanner",
    ]);
    expect(c.pages.has("acme.scan")).toBe(true);
  });

  it("registers a page without a sidebar link when it does not", () => {
    const c = fresh();
    c.install("acme.scanner", {
      pages: [{ kind: "acme.scan", label: "Scanner" }],
    });

    expect(c.pages.has("acme.scan")).toBe(true);
    expect(c.sidebar().map((l) => l.label)).toEqual(["Plugins", "MCPs"]);
  });

  it("keeps igni's own entries first", () => {
    const c = fresh();
    c.install("acme.scanner", {
      pages: [{ kind: "acme.scan", label: "Scanner", sidebar: true }],
      menu: [{ label: "Scan", command: "/acme-scan", desc: "scan the tree" }],
    });

    // A reader who learned where Plugins sits finds it there after an
    // install, and an extension cannot push igni's navigation down.
    expect(c.sidebar()[0].label).toBe("Plugins");
    expect(c.tools()[0].label).toBe("MCP servers");
    expect(c.tools().at(-1)?.label).toBe("Scan");
  });

  it("says who owns each contributed entry", () => {
    const c = fresh();
    c.install("acme.scanner", {
      pages: [{ kind: "acme.scan", label: "Scanner", sidebar: true }],
    });

    expect(c.sidebar().at(-1)?.owner).toBe("acme.scanner");
    expect(c.pages.ownerOf("acme.scan")).toBe("acme.scanner");
    expect(c.pages.ownerOf("plugins")).toBeNull();
  });
});

describe("refusing", () => {
  it("will not let an extension shadow one of igni's own pages", () => {
    const c = fresh();

    const refused = c.install("acme.scanner", {
      pages: [{ kind: "artifacts", label: "Artifacts" }],
    });

    expect(refused).toHaveLength(1);
    expect(refused[0].reason).toContain("igni's own pages");
    expect(c.pages.ownerOf("artifacts")).toBeNull();
  });

  it("will not let one extension take another's page kind", () => {
    const c = fresh();
    c.install("acme.scanner", {
      pages: [{ kind: "shared.kind", label: "First" }],
    });

    const refused = c.install("other.ext", {
      pages: [{ kind: "shared.kind", label: "Second" }],
    });

    expect(refused[0].reason).toContain("acme.scanner");
    expect(c.pages.ownerOf("shared.kind")).toBe("acme.scanner");
  });

  it("will not let a tools label mean two things", () => {
    const c = fresh();

    const refused = c.install("acme.scanner", {
      menu: [{ label: "CodeIndex", command: "/acme", desc: "not that one" }],
    });

    expect(refused[0].reason).toContain("already in the tools menu");
    expect(c.tools().filter((e) => e.label === "CodeIndex")).toHaveLength(1);
  });

  it("installs the entries that were fine and reports the ones that were not", () => {
    const c = fresh();

    const refused = c.install("acme.scanner", {
      pages: [
        { kind: "artifacts", label: "Shadowing" },
        { kind: "acme.scan", label: "Scanner", sidebar: true },
      ],
    });

    // One bad page costs that page, not the extension — and the
    // administrator gets told which one.
    expect(refused).toHaveLength(1);
    expect(c.pages.has("acme.scan")).toBe(true);
    expect(c.sidebar().map((l) => l.label)).toContain("Scanner");
  });
});

describe("leaving", () => {
  it("takes every surface with it", () => {
    const c = fresh();
    c.install("acme.scanner", {
      pages: [{ kind: "acme.scan", label: "Scanner", sidebar: true }],
      menu: [{ label: "Scan", command: "/acme-scan", desc: "scan the tree" }],
    });

    c.uninstall("acme.scanner");

    expect(c.pages.has("acme.scan")).toBe(false);
    expect(c.sidebar().map((l) => l.label)).toEqual(["Plugins", "MCPs"]);
    expect(c.tools().map((e) => e.label)).toEqual(["MCP servers", "CodeIndex"]);
  });

  it("frees the names it was holding", () => {
    const c = fresh();
    c.install("acme.scanner", {
      pages: [{ kind: "shared.kind", label: "First" }],
    });
    c.uninstall("acme.scanner");

    const refused = c.install("other.ext", {
      pages: [{ kind: "shared.kind", label: "Second" }],
    });

    expect(refused).toEqual([]);
    expect(c.pages.ownerOf("shared.kind")).toBe("other.ext");
  });

  it("leaves igni's own entries alone", () => {
    const c = fresh();
    c.uninstall("acme.scanner");
    expect(c.sidebar()).toHaveLength(2);
    expect(c.tools()).toHaveLength(2);
  });
});

describe("a saved stack that names a page nobody has", () => {
  it("restores an extension's page while the extension is installed", () => {
    const c = fresh();
    c.install("acme.scanner", {
      pages: [{ kind: "acme.scan", label: "Scanner" }],
    });
    const saved = JSON.stringify([{ kind: "acme.scan", label: "Scanner" }]);

    expect(restore(saved, c.pages)).toEqual([
      { kind: "acme.scan", label: "Scanner" },
    ]);
  });

  it("opens on the chat once the extension is gone", () => {
    const c = fresh();
    c.install("acme.scanner", {
      pages: [{ kind: "acme.scan", label: "Scanner" }],
    });
    const saved = JSON.stringify([{ kind: "acme.scan", label: "Scanner" }]);
    c.uninstall("acme.scanner");

    // Not a route to a surface that is not there.
    expect(restore(saved, c.pages)).toEqual([]);
  });

  it("truncates the trail at the level it cannot place", () => {
    const c = fresh();
    const saved = JSON.stringify([
      { kind: "plugins", label: "Plugins" },
      { kind: "acme.scan", label: "Scanner" },
      { kind: "mcp", label: "MCPs" },
    ]);

    // Not "skip the middle": with a level missing, what sits above it
    // has no context, and a breadcrumb trail with a hole in it says
    // something untrue about how the reader got there.
    expect(restore(saved, c.pages)).toEqual([
      { kind: "plugins", label: "Plugins" },
    ]);
  });
});
