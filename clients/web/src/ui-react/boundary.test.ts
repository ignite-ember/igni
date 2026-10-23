/**
 * Nothing under the floor imports the app.
 *
 * `@igni/ui-react` is consumed from outside this repository, where
 * `../components/` and `../chat/` do not exist. An import that leaves
 * this directory is therefore a module that cannot be published —
 * and if it is reachable from `index.ts`, it takes whatever it
 * imported under the compatibility floor with it.
 *
 * TypeScript will not catch this. Inside the repo those paths resolve
 * perfectly; it is only the published package where they are missing.
 * `SpecBlock` shipped under the floor doing
 * `import("../components/JsonRenderView")` — dynamic, so not even a
 * bundler would have said anything until an extension author's page
 * went blank at runtime.
 *
 * Dynamic imports are checked alongside static ones for that reason:
 * the one that actually happened was the one a compiler cannot see.
 */
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { withoutComments } from "../test-utils/sourceScan";

const here = dirname(fileURLToPath(import.meta.url));

/**
 * What an extension's own `node_modules` is expected to hold.
 *
 * Each of these is a real cost to an author, so the list is short and
 * adding to it is a decision. `mermaid` is optional — `Mermaid.tsx`
 * loads it on demand and only a diagram pays for it — but it is still
 * a dependency they have to install, and that belongs in the README
 * rather than in a surprise at runtime.
 */
const PEER_DEPENDENCIES = [
  "react",
  "react-dom",
  "react-markdown",
  "remark-gfm",
  "mermaid",
];

function isAllowed(spec: string): boolean {
  if (spec.startsWith("./")) return !spec.slice(2).includes("/");
  if (spec.startsWith("node:")) return false; // a browser bundle has no node
  if (spec.startsWith(".")) return false; // any other relative path leaves
  const pkg = spec.startsWith("@")
    ? spec.split("/").slice(0, 2).join("/")
    : spec.split("/")[0];
  return PEER_DEPENDENCIES.includes(pkg);
}

/** Static `from "x"`, bare `import "x"`, and dynamic `import("x")`. */
function specifiers(src: string): string[] {
  const patterns = [
    /\bfrom\s*["']([^"']+)["']/g,
    /\bimport\s*["']([^"']+)["']/g,
    /\bimport\s*\(\s*["']([^"']+)["']\s*\)/g,
    /\brequire\s*\(\s*["']([^"']+)["']\s*\)/g,
  ];
  return patterns.flatMap((p) => [...src.matchAll(p)].map((m) => m[1]));
}

const sources = readdirSync(here)
  .filter((f) => /\.tsx?$/.test(f) && !f.includes(".test."))
  .sort();

describe("the floor is self-contained", () => {
  it.each(sources)("%s imports nothing outside the package", (file) => {
    const found = specifiers(withoutComments(readFileSync(join(here, file), "utf8")));
    expect(found.filter((s) => !isAllowed(s)).sort()).toEqual([]);
  });

  it("reads imports at all", () => {
    // Same scanner guard as styled.test.ts: a regex that matched
    // nothing would make every case above pass vacuously.
    const found = specifiers(
      withoutComments(readFileSync(join(here, "Tooltip.tsx"), "utf8")),
    );
    expect(found).toContain("react");
    expect(found).toContain("react-dom");
  });

  it("sees a dynamic import, which is how the last one got through", () => {
    const found = specifiers(`void import("../components/JsonRenderView");`);
    expect(found).toEqual(["../components/JsonRenderView"]);
    expect(isAllowed(found[0])).toBe(false);
  });
});
