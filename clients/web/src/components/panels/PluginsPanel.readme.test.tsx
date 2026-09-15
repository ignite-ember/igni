// @vitest-environment jsdom
/**
 * A plugin README is third-party markup rendered inside our own window.
 *
 * `PluginsPanel` renders it with `rehype-raw` — raw HTML enabled on
 * purpose, because real READMEs use it for layout — and relies on
 * `rehype-sanitize` with a widened schema to make that safe. The comment
 * above that schema claimed it "strips anything that could run code or
 * exfiltrate (scripts, iframes, event handlers)".
 *
 * Half true, and the half that was false mattered. Rendering payloads
 * through the actual pipeline confirmed `<script>`, `<iframe>`,
 * `onerror` and `javascript:` are all handled, and found three that were
 * not:
 *
 *   * `style` on `*` allowed a full-viewport `position: fixed` overlay —
 *     a marketplace listing could repaint the entire app with something
 *     that looks like our own dialog asking for a token.
 *   * `style` allowed `background-image: url(https://...)`, an outbound
 *     request on render. self-hosting.md §8 is explicit that a silent
 *     phone-home breaks the air-gap claim.
 *   * `rel` on `<a>` let a README ask for `rel="opener"`, re-enabling the
 *     reverse tabnabbing the browser default prevents.
 *
 * These tests import the schema and the component overrides the panel
 * actually uses, rather than a copy — a copy would keep passing after
 * somebody widens the real one.
 */

import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeRaw from "rehype-raw";
import rehypeSanitize from "rehype-sanitize";

import { README_SANITIZE_SCHEMA, README_COMPONENTS } from "./PluginsPanel";

/** The same pipeline as the render site, minus rehype-highlight (which
 *  only touches code blocks and is not part of what is being asserted). */
function renderReadme(markdown: string): HTMLElement {
  const { container } = render(
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[rehypeRaw, [rehypeSanitize, README_SANITIZE_SCHEMA]]}
      components={README_COMPONENTS}
    >
      {markdown}
    </ReactMarkdown>,
  );
  return container;
}

describe("what the sanitizer already blocked", () => {
  it("drops script tags entirely", () => {
    expect(renderReadme(`<script>alert(1)</script>`).querySelector("script")).toBeNull();
  });

  it("drops iframes entirely", () => {
    expect(renderReadme(`<iframe src="https://evil.example"></iframe>`).innerHTML).not.toContain(
      "iframe",
    );
  });

  it("strips event handlers off an element that survives", () => {
    const img = renderReadme(`<img src="x" onerror="alert(1)">`).querySelector("img");
    expect(img).not.toBeNull();
    expect(img!.getAttribute("onerror")).toBeNull();
  });

  it("neutralises javascript: hrefs", () => {
    const a = renderReadme(`<a href="javascript:alert(1)">x</a>`).querySelector("a");
    expect(a?.getAttribute("href") ?? "").not.toContain("javascript:");
  });
});

describe("a README cannot repaint the application", () => {
  it("cannot position an element at all", () => {
    // The overlay payload verbatim. It used to survive with every
    // declaration intact.
    const container = renderReadme(
      `<div style="position:fixed;top:0;left:0;width:100vw;height:100vh;z-index:99999;background:#fff">Paste your token</div>`,
    );

    expect(container.querySelector("[style]")).toBeNull();
    expect(container.innerHTML).not.toContain("position");
  });

  it("cannot carry inline style on any allowed tag", () => {
    // `mark` is one of the tags this schema adds, so it is a good check
    // that the fix is on `*` rather than on `div` specifically.
    for (const payload of [
      `<div style="color:red">x</div>`,
      `<mark style="color:red">x</mark>`,
      `<details style="color:red"><summary>s</summary>b</details>`,
      `<span style="color:red">x</span>`,
    ]) {
      expect(renderReadme(payload).querySelector("[style]")).toBeNull();
    }
  });
});

describe("a README cannot phone home through CSS", () => {
  it("cannot request a remote URL via background-image", () => {
    const container = renderReadme(
      `<div style="background-image:url(https://evil.example/pixel.png)">x</div>`,
    );

    expect(container.innerHTML).not.toContain("evil.example");
  });

  it("also cannot do it from an element the schema deliberately added", () => {
    const container = renderReadme(
      `<mark style="background:url('https://evil.example/seen')">x</mark>`,
    );

    expect(container.innerHTML).not.toContain("evil.example");
  });
});

describe("outward links are ours to configure", () => {
  it("forces noopener noreferrer even when the README asks for opener", () => {
    const a = renderReadme(
      `<a href="https://example.com" target="_blank" rel="opener">x</a>`,
    ).querySelector("a");

    expect(a).not.toBeNull();
    expect(a!.getAttribute("rel")).toBe("noopener noreferrer");
    expect(a!.getAttribute("target")).toBe("_blank");
  });

  it("applies to plain markdown links too, not just raw HTML ones", () => {
    const a = renderReadme(`[docs](https://example.com)`).querySelector("a");

    expect(a!.getAttribute("rel")).toBe("noopener noreferrer");
  });

  it("keeps the href it was given", () => {
    const a = renderReadme(`[docs](https://example.com/a/b)`).querySelector("a");

    expect(a!.getAttribute("href")).toBe("https://example.com/a/b");
  });
});

describe("the panel actually uses these", () => {
  /* Asserted on the source, because the tests above wire the schema and the
   * component overrides themselves. Reverting the render site alone — the
   * `components={README_COMPONENTS}` prop specifically — left every
   * behavioural test above still passing, so without this the anchor fix
   * could be dropped silently. */
  it("passes both the schema and the component overrides at the render site", async () => {
    // `?raw` rather than node:fs — under the jsdom environment
    // `import.meta.url` is not a file: URL, and a cwd-relative path would
    // depend on where vitest was invoked from.
    const source = (await import("./PluginsPanel.tsx?raw")).default;

    expect(source).toContain("[rehypeSanitize, README_SANITIZE_SCHEMA]");
    expect(source).toContain("components={README_COMPONENTS}");
    // rehype-raw is what makes any of this necessary; if it goes away the
    // sanitizer is moot, but so is the risk — this asserts the pairing
    // rather than either half.
    expect(source).toContain("rehypeRaw");
  });
});

describe("the layouts the schema exists for still work", () => {
  it("keeps align, width and height", () => {
    const container = renderReadme(
      `<p align="center"><img src="https://img.shields.io/badge/a-b-blue" width="120" height="20"></p>`,
    );

    expect(container.querySelector("p")?.getAttribute("align")).toBe("center");
    const img = container.querySelector("img");
    expect(img?.getAttribute("width")).toBe("120");
    expect(img?.getAttribute("height")).toBe("20");
  });

  it("keeps picture/source for light-dark badges", () => {
    const container = renderReadme(
      `<picture><source srcset="d.png" media="(prefers-color-scheme: dark)"><img src="l.png"></picture>`,
    );

    expect(container.querySelector("source")?.getAttribute("srcset")).toBe("d.png");
    expect(container.querySelector("img")?.getAttribute("src")).toBe("l.png");
  });

  it("keeps details/summary", () => {
    const container = renderReadme(`<details><summary>More</summary>Body</details>`);

    expect(container.querySelector("details summary")?.textContent).toBe("More");
  });
});
