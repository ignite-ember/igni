// @vitest-environment jsdom
/**
 * The design tokens an extension may build against.
 *
 * Two things are being pinned. That the published list is a curated
 * subset rather than "whatever is in the stylesheet" — because a
 * token named here is under the compatibility floor and one that is
 * not is ours to change. And that the values travel live rather than
 * copied, because `theme.css` defines them across forty-five blocks
 * and any second copy is wrong within a release.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, describe, expect, it } from "vitest";

import {
  PUBLISHED_TOKENS,
  TOKEN_NAMES,
  applyThemeTokens,
  readThemeTokens,
} from "./tokens";

afterEach(() => {
  document.documentElement.removeAttribute("style");
});

describe("what is published", () => {
  it("names the surfaces, text, lines and tones an extension needs", () => {
    expect(PUBLISHED_TOKENS.surface).toContain("--bg-surface");
    expect(PUBLISHED_TOKENS.text).toContain("--fg-muted");
    expect(PUBLISHED_TOKENS.line).toContain("--border");
    expect(PUBLISHED_TOKENS.tone).toContain("--danger");
    expect(PUBLISHED_TOKENS.form).toContain("--radius-sm");
  });

  it("leaves this app's own chrome out of the floor", () => {
    // An extension that positioned itself against the sidebar's width
    // would break the first time the sidebar moved. These stay ours.
    for (const internal of [
      "--rail-width",
      "--sidebar-w",
      "--crumb-row-h",
      "--page-chrome-h",
      "--dock-top",
      "--tree-width",
    ]) {
      expect(TOKEN_NAMES).not.toContain(internal);
    }
  });

  it("offers the contrast-safe variant wherever there is one", () => {
    // `--danger` is 3.74:1 on a raised surface; `--danger-text` is the
    // same hue at 4.50. An extension writing type needs the second.
    expect(TOKEN_NAMES).toContain("--danger");
    expect(TOKEN_NAMES).toContain("--danger-text");
  });

  it("has no duplicates across its groups", () => {
    expect(new Set(TOKEN_NAMES).size).toBe(TOKEN_NAMES.length);
  });
});

describe("reading them from the host", () => {
  it("takes the computed value, not the declared one", () => {
    document.documentElement.style.setProperty("--bg", "#111111");

    expect(readThemeTokens()["--bg"]).toBe("#111111");
  });

  it("omits a token the host has not defined", () => {
    // Omitted rather than sent empty: the guest's stylesheet carries a
    // fallback for each, and an empty string would override it with
    // nothing.
    const tokens = readThemeTokens();
    expect(tokens).not.toHaveProperty("--bg-raised");
  });

  it("never reports anything outside the published list", () => {
    document.documentElement.style.setProperty("--rail-width", "260px");

    expect(readThemeTokens()).not.toHaveProperty("--rail-width");
  });
});

describe("applying them in the extension", () => {
  it("sets each one on the document it is given", () => {
    const root = document.createElement("div");

    applyThemeTokens({ "--bg": "#222", "--fg": "#eee" }, root);

    expect(root.style.getPropertyValue("--bg")).toBe("#222");
    expect(root.style.getPropertyValue("--fg")).toBe("#eee");
  });

  it("ignores a name that is not published", () => {
    const root = document.createElement("div");

    // The host is trusted, but a typo on our side should not quietly
    // define something inside theirs.
    applyThemeTokens({ "--not-a-token": "red", "--bg": "#222" }, root);

    expect(root.style.getPropertyValue("--not-a-token")).toBe("");
    expect(root.style.getPropertyValue("--bg")).toBe("#222");
  });

  it("round-trips what the host read", () => {
    document.documentElement.style.setProperty("--bg", "#101010");
    document.documentElement.style.setProperty("--fg-muted", "#999999");
    const sent = readThemeTokens();

    const guest = document.createElement("div");
    applyThemeTokens(sent, guest);

    expect(guest.style.getPropertyValue("--bg")).toBe("#101010");
    expect(guest.style.getPropertyValue("--fg-muted")).toBe("#999999");
  });
});

describe("the published list and the stylesheet agree", () => {
  // The two halves of one promise. `igni-ui.css` carries a fallback
  // for every token it uses, so an extension opened outside igni
  // still looks right; the bridge overwrites them with live values
  // inside. A token the stylesheet uses but the host never sends
  // would silently stay on its fallback and drift from the app on
  // the first theme change — which is the whole failure this layer
  // exists to prevent.
  const css = readFileSync(
    join(dirname(fileURLToPath(import.meta.url)), "../../../ui/igni-ui.css"),
    "utf8",
  );

  it("uses no token that is not published", () => {
    const used = new Set(
      [...css.matchAll(/var\((--[a-z0-9-]+)/g)].map((m) => m[1]),
    );
    expect([...used].filter((name) => !TOKEN_NAMES.includes(name))).toEqual([]);
  });

  it("declares a fallback for nothing it does not publish", () => {
    const declared = new Set(
      [...css.matchAll(/^\s+(--[a-z0-9-]+)\s*:/gm)].map((m) => m[1]),
    );
    expect([...declared].filter((name) => !TOKEN_NAMES.includes(name))).toEqual(
      [],
    );
  });

  it("gives every token it uses a standalone fallback", () => {
    const used = new Set(
      [...css.matchAll(/var\((--[a-z0-9-]+)/g)].map((m) => m[1]),
    );
    const declared = new Set(
      [...css.matchAll(/^\s+(--[a-z0-9-]+)\s*:/gm)].map((m) => m[1]),
    );
    // Opening the page outside igni must not give an unstyled one.
    expect([...used].filter((name) => !declared.has(name))).toEqual([]);
  });
});
