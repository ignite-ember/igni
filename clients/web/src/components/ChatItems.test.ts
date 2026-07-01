/**
 * Pure-helper tests pulled out of ChatItems.tsx — these have no
 * DOM dependency and they encode load-bearing UX behaviour:
 *
 *   • swapCodeBlocks / restoreCodeBlocks
 *     Edit-mode round-trip for ``[code-paste …]…[/code-paste]`` blocks.
 *     The user sees compact ``«code:foo.py L9»`` placeholders in the
 *     textarea so they can REORDER but not corrupt snippet content;
 *     ``save`` swaps each surviving placeholder back to its full
 *     block. Deleting a placeholder removes the snippet. The
 *     invariant: ``restore(swap(x))`` is identity when nothing
 *     changed in the middle.
 *
 *   • guessLang
 *     Maps a file extension to a syntax-highlight language. Wrong
 *     mapping = wrong colouring in tool-output code blocks.
 */

import { describe, expect, it } from "vitest";
import {
  guessLang,
  restoreCodeBlocks,
  swapCodeBlocks,
} from "./ChatItems";

// ── swap/restore code-paste round-trip ──────────────────────

describe("swapCodeBlocks + restoreCodeBlocks", () => {
  const sample = `before
[code-paste src/foo.py:9 lines=9-12]
def hello():
    return 1
[/code-paste]
middle
[code-paste src/bar.ts:42 lines=42-44]
const x = 1;
[/code-paste]
after`;

  it("swap returns text with each block replaced by a single placeholder", () => {
    const store = new Map<string, string>();
    const swapped = swapCodeBlocks(sample, store);
    // No raw ``[code-paste`` left in the swapped text — the
    // textarea must never show the full block during edit.
    expect(swapped).not.toContain("[code-paste");
    expect(swapped).not.toContain("[/code-paste]");
    // Both placeholders captured in the store.
    expect(store.size).toBe(2);
  });

  it("placeholder includes the filename and lines so the user can ID it", () => {
    // Without the filename + line span, two snippets from the
    // same file would be visually indistinguishable in the
    // textarea — couldn't reorder safely.
    const store = new Map<string, string>();
    const swapped = swapCodeBlocks(sample, store);
    expect(swapped).toContain("«code:foo.py 9-12#1»");
    expect(swapped).toContain("«code:bar.ts 42-44#2»");
  });

  it("restore is the exact inverse when text is untouched (identity round-trip)", () => {
    // Load-bearing: an unchanged edit must NOT mutate the
    // original message body. Any drift would corrupt the
    // saved turn.
    const store = new Map<string, string>();
    const swapped = swapCodeBlocks(sample, store);
    const restored = restoreCodeBlocks(swapped, store);
    expect(restored).toBe(sample);
  });

  it("removing a placeholder removes the corresponding snippet on save", () => {
    // The product behavior: cut-and-delete the «code» token in
    // the textarea, hit save → the snippet vanishes from the
    // saved message. (User can pare back over-eager attachment.)
    const store = new Map<string, string>();
    let swapped = swapCodeBlocks(sample, store);
    // User deletes the second placeholder + its surrounding
    // newline:
    swapped = swapped.replace("«code:bar.ts 42-44#2»\nafter", "after");
    const restored = restoreCodeBlocks(swapped, store);
    expect(restored).toContain("def hello()");
    expect(restored).not.toContain("const x = 1");
    expect(restored).toContain("after");
  });

  it("reordering placeholders reorders the snippets", () => {
    const store = new Map<string, string>();
    const swapped = swapCodeBlocks(sample, store);
    // User swaps the two placeholders.
    const ph1 = "«code:foo.py 9-12#1»";
    const ph2 = "«code:bar.ts 42-44#2»";
    const reordered = swapped.replace(ph1, "TMP").replace(ph2, ph1).replace("TMP", ph2);
    const restored = restoreCodeBlocks(reordered, store);
    const fooIdx = restored.indexOf("def hello()");
    const barIdx = restored.indexOf("const x = 1");
    expect(fooIdx).toBeGreaterThan(barIdx);
  });

  it("clears the store on each new swap (no leakage between edits)", () => {
    // The store is shared across edit sessions in the React
    // component. If swap doesn't clear, a second edit picks up
    // stale placeholders from a prior message and "restores"
    // them into the wrong text.
    const store = new Map<string, string>();
    swapCodeBlocks(sample, store);
    swapCodeBlocks("just plain text", store);
    expect(store.size).toBe(0);
  });

  it("restore preserves placeholder-less text untouched", () => {
    const store = new Map<string, string>();
    expect(restoreCodeBlocks("hello world", store)).toBe("hello world");
  });

  it("restore drops placeholders absent from the store as empty (lost-snippet recovery)", () => {
    // If the user pastes a token from a DIFFERENT message,
    // we can't reconstruct it — render as empty (nothing
    // weirder than a literal «code:…» token leaking into the
    // saved turn).
    const store = new Map<string, string>();
    expect(restoreCodeBlocks("a «code:ghost.py 1-2#7» b", store)).toBe("a  b");
  });
});

// ── guessLang ──────────────────────────────────────────────

describe("guessLang", () => {
  it("returns empty for an extension we don't know", () => {
    // Highlight.js falls back to plain text on empty lang —
    // safer than guessing wrong (highlighter mangles output
    // when given the wrong grammar).
    expect(guessLang("file.unknown")).toBe("");
    expect(guessLang("README")).toBe(""); // no extension at all
  });

  it("normalizes Python extensions", () => {
    expect(guessLang("foo.py")).toBe("python");
    expect(guessLang("types.pyi")).toBe("python");
  });

  it("disambiguates TypeScript from JSX flavor", () => {
    // ``.tsx`` must NOT collapse to "typescript" — highlight.js
    // mishandles the JSX tags.
    expect(guessLang("Button.tsx")).toBe("tsx");
    expect(guessLang("util.ts")).toBe("typescript");
    expect(guessLang("legacy.jsx")).toBe("jsx");
    expect(guessLang("util.js")).toBe("javascript");
  });

  it("maps shell aliases to 'bash'", () => {
    // ``.zsh`` / ``.bash`` files have no separate highlighter
    // — they all render under "bash".
    expect(guessLang("install.sh")).toBe("bash");
    expect(guessLang(".zshrc.zsh")).toBe("bash");
  });

  it("maps both yaml extensions", () => {
    expect(guessLang("config.yml")).toBe("yaml");
    expect(guessLang("config.yaml")).toBe("yaml");
  });

  it("maps html/xml to xml grammar", () => {
    // The xml highlighter handles both; highlight.js has no
    // separate html mode in the slim bundle we ship.
    expect(guessLang("index.html")).toBe("xml");
    expect(guessLang("doc.xml")).toBe("xml");
  });

  it("is case-insensitive on the extension", () => {
    // Some uploads from Finder carry the original
    // ``Document.PY``-style capitalisation; we shouldn't drop
    // syntax highlighting for that.
    expect(guessLang("Foo.PY")).toBe("python");
    expect(guessLang("Bar.TS")).toBe("typescript");
  });

  it("uses the last dot for the extension", () => {
    // ``foo.test.ts`` → ts, not test. (split(".").pop())
    expect(guessLang("foo.test.ts")).toBe("typescript");
    expect(guessLang("multi.part.name.py")).toBe("python");
  });

  it("handles a path with directory components", () => {
    expect(guessLang("src/lib/host.ts")).toBe("typescript");
    expect(guessLang("/abs/path/file.rs")).toBe("rust");
  });
});
