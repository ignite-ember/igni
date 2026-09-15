// @vitest-environment jsdom
/**
 * Tests for group brand overrides.
 *
 * The security-relevant behaviour is the point of this file. Values
 * here come from an org admin via a server the client may not own, and
 * they end up on CSS custom properties — so the cases that matter are
 * the ones where a value tries to be something other than a colour.
 */

import { beforeEach, describe, expect, it } from "vitest";

import { applyTheme, brandMark, brandName } from "./theme";

let root: HTMLElement;

beforeEach(() => {
  root = document.createElement("div");
});

describe("applyTheme", () => {
  it("maps brand keys onto the stylesheet's tokens", () => {
    const { applied, rejected } = applyTheme({ accent: "#123456" }, root);
    expect(rejected).toEqual([]);
    expect(applied["--ember-orange"]).toBe("#123456");
    expect(root.style.getPropertyValue("--ember-orange")).toBe("#123456");
  });

  it("also writes --accent, the stylesheet's other name for the same thing", () => {
    // Found by running it. ``theme.css`` reads ``--ember-orange`` in 47
    // places and bare ``--accent`` in 17 more, and never defines the
    // latter — so setting only the first left those 17 on igni's
    // orange while the rest of the product had changed.
    applyTheme({ accent: "#123456" }, root);
    expect(root.style.getPropertyValue("--accent")).toBe("#123456");
  });

  it("leaves unset tokens to the shipped palette", () => {
    // A theme that sets only the accent must not blank out the rest.
    const { applied } = applyTheme({ accent: "#123456" }, root);
    expect(applied["--danger"]).toBeUndefined();
    expect(root.style.getPropertyValue("--danger")).toBe("");
  });

  it("refuses a value that tries to close the declaration", () => {
    // The reason ``setProperty`` is used instead of building CSS text.
    // As a string in a stylesheet this would end the rule and open its
    // own; as a property value it is simply not a colour.
    const { applied, rejected } = applyTheme(
      { accent: "#fff; } body { display: none } .x {" },
      root,
    );
    expect(rejected).toContain("accent");
    expect(applied["--ember-orange"]).toBeUndefined();
    expect(root.getAttribute("style") ?? "").not.toContain("display");
  });

  it("refuses non-hex colour syntax", () => {
    // Hex only, matching the server. ``rgb()`` nests expressions and
    // named colours would need enumerating to be checked at all.
    for (const bad of ["red", "rgb(1,2,3)", "var(--x)", "url(http://x)", "#12"]) {
      const { applied, rejected } = applyTheme({ accent: bad }, root);
      expect(rejected, bad).toContain("accent");
      expect(applied["--ember-orange"], bad).toBeUndefined();
    }
  });

  it("composes the gradient from validated colours", () => {
    // It spans the header and primary buttons; leaving it at igni's
    // orange while the accent moved reads as a bug, not as branding.
    const { applied } = applyTheme({ accent: "#111111", danger: "#222222" }, root);
    expect(applied["--ember-gradient"]).toBe("linear-gradient(135deg, #111111, #222222)");
  });

  it("does not compose a gradient from a rejected colour", () => {
    const { applied } = applyTheme({ accent: "#fff)); }" }, root);
    expect(applied["--ember-gradient"]).toBeUndefined();
  });

  it("clears what it owns when the theme goes away", () => {
    // Moving a user to a group with no branding has to restore the
    // shipped palette, not strand the last theme until reload.
    applyTheme({ accent: "#123456", danger: "#654321" }, root);
    const { applied } = applyTheme(null, root);
    expect(applied).toEqual({});
    expect(root.style.getPropertyValue("--ember-orange")).toBe("");
    expect(root.style.getPropertyValue("--accent")).toBe("");
    expect(root.style.getPropertyValue("--ember-gradient")).toBe("");
  });

  it("ignores keys outside the allowlist", () => {
    // The key arrives over the wire; deriving a property name from it
    // would let the server write any custom property it liked.
    applyTheme({ "--evil": "#fff", accent: "#123456" } as never, root);
    expect(root.style.getPropertyValue("--evil")).toBe("");
    expect(root.style.getPropertyValue("--ember-orange")).toBe("#123456");
  });
});

describe("brandName", () => {
  it("falls back to igni", () => {
    expect(brandName(null)).toBe("igni");
    expect(brandName({ brand_name: "   " })).toBe("igni");
  });

  it("uses the group's wordmark", () => {
    expect(brandName({ brand_name: "Acme AI" })).toBe("Acme AI");
  });
});

describe("brandMark", () => {
  it("accepts the data: URIs the server accepts", () => {
    const svg = "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=";
    expect(brandMark({ brand_mark: svg })).toBe(svg);
  });

  it("refuses a remote URL", () => {
    // The desktop client's CSP is ``img-src 'self' data: blob:``, so a
    // URL could not load anyway — and one that could would be a
    // per-user beacon to whoever set it.
    expect(brandMark({ brand_mark: "https://evil.example/logo.png" })).toBeNull();
  });

  it("refuses a javascript: URI", () => {
    expect(brandMark({ brand_mark: "javascript:alert(1)" })).toBeNull();
  });

  it("refuses a non-image data: URI", () => {
    expect(brandMark({ brand_mark: "data:text/html;base64,PGgxPmhp" })).toBeNull();
  });

  it("is null when unset", () => {
    expect(brandMark(null)).toBeNull();
    expect(brandMark({})).toBeNull();
  });
});
