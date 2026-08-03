import { describe, expect, it } from "vitest";
import { createElement } from "react";
import {
  extractUnfoldableSource,
  hasBoxDrawingChars,
  hasFlowChartArrows,
  isNoLanguageFence,
  normalizeAssistantMarkdown,
} from "./ChatItems";

describe("normalizeAssistantMarkdown", () => {
  it("inserts blank line before a heading glued to prior text", () => {
    const before = "some intro text\n## Origins";
    const out = normalizeAssistantMarkdown(before);
    expect(out).toBe("some intro text\n\n## Origins");
  });

  it("inserts blank line after a heading immediately followed by prose", () => {
    const before = "## Heading\nBody starts here.";
    const out = normalizeAssistantMarkdown(before);
    expect(out).toBe("## Heading\n\nBody starts here.");
  });

  it("splits a heading that has the following paragraph stuck to it without whitespace", () => {
    // The Gandalf bug: model emitted the heading and the first
    // paragraph as one line, no separator at all.
    const before =
      "## Origins Beyond the MountainsBefore ever he walked among the peoples of Middle-earth, the spirit existed.";
    const out = normalizeAssistantMarkdown(before);
    expect(out).toBe(
      "## Origins Beyond the Mountains\n\nBefore ever he walked among the peoples of Middle-earth, the spirit existed.",
    );
  });

  it("does NOT split camelCase brand names in short headings", () => {
    // Real cases: programming brands often appear in headings.
    // They don't have a trailing "space + more words" after the
    // capital letter, so the heuristic leaves them alone.
    expect(normalizeAssistantMarkdown("## iPhone")).toBe("## iPhone");
    expect(normalizeAssistantMarkdown("## JavaScript")).toBe("## JavaScript");
    expect(normalizeAssistantMarkdown("# ChatGPT")).toBe("# ChatGPT");
  });

  it("does NOT split a heading that ends cleanly with a newline", () => {
    const before = "## Origins Beyond the Mountains\n\nBefore ever he walked.";
    const out = normalizeAssistantMarkdown(before);
    expect(out).toBe(before);
  });

  it("leaves regular paragraph text alone", () => {
    const text = "Hello! How can I help you today?";
    expect(normalizeAssistantMarkdown(text)).toBe(text);
  });

  it("splits a heading with an opening code fence glued to its end", () => {
    const before = "### Recommended Fix Order```\n1 do thing\n```";
    const out = normalizeAssistantMarkdown(before);
    expect(out).toBe(
      "### Recommended Fix Order\n\n```\n1 do thing\n```",
    );
  });

  it("splits a heading with a fence + language info string", () => {
    const before = "## Example ```python\nprint(1)\n```";
    const out = normalizeAssistantMarkdown(before);
    expect(out).toBe("## Example\n\n```python\nprint(1)\n```");
  });

  it("does NOT split a heading that contains inline single-backtick code", () => {
    const before = "### Use `foo` correctly";
    expect(normalizeAssistantMarkdown(before)).toBe(before);
  });

  it("retains existing GFM-table normalization", () => {
    // One-line table → re-broken into rows.
    const before = "| a | b | |---|---| | 1 | 2 |";
    const out = normalizeAssistantMarkdown(before);
    expect(out).toContain("|---|---|\n");
  });

  it("splits a horizontal rule glued to a heading on the same line", () => {
    // Common case: the model emits `---` as a section divider and
    // forgets to put it on its own line, so it gets concatenated to
    // the next heading. Without the fix the whole line renders as
    // literal text "---## …".
    const before = "---## The Training Recipe: SSP";
    expect(normalizeAssistantMarkdown(before)).toBe(
      "---\n\n## The Training Recipe: SSP",
    );
  });

  it("splits a horizontal rule glued to plain paragraph text", () => {
    // Less common but the same fix: `---` followed by text on the
    // same line should still render the HR.
    const before = "intro paragraph\n---Then this happened.";
    expect(normalizeAssistantMarkdown(before)).toBe(
      "intro paragraph\n\n---\n\nThen this happened.",
    );
  });

  it("leaves a bare horizontal rule alone", () => {
    expect(normalizeAssistantMarkdown("---")).toBe("---");
  });

  it("leaves a horizontal rule with trailing whitespace alone", () => {
    // Both already parse as <hr> in CommonMark — don't touch.
    expect(normalizeAssistantMarkdown("--- ")).toBe("--- ");
    expect(normalizeAssistantMarkdown("---   ")).toBe("---   ");
  });

  it("leaves a 4+ dash line alone (still a valid HR)", () => {
    expect(normalizeAssistantMarkdown("----")).toBe("----");
    expect(normalizeAssistantMarkdown("-----")).toBe("-----");
  });

  it("splits multiple glued HRs in one message", () => {
    const before = "---## First\n\nbody\n---## Second";
    expect(normalizeAssistantMarkdown(before)).toBe(
      "---\n\n## First\n\nbody\n\n---\n\n## Second",
    );
  });
});

// ── extractUnfoldableSource ─────────────────────────────
//
// Dispatcher helper used by MarkdownPre to detect fenced blocks
// that should be unfolded as inline markdown (prose, calculations,
// ASCII diagrams) instead of rendered as a code pill. Triggers on:
//   - explicit ``language-md`` / ``language-markdown``
//   - ``language-text`` / ``language-plain`` (the "not really code"
//     hints)
//   - no language at all — the agent uses bare ``\`\`\`…\`\`\``
//     fences for chat content that isn't code
// Any other language (Python, JSON, etc.) keeps the CodeBlock
// path with its copy chip and collapse chevron.

describe("extractUnfoldableSource", () => {
  // ReactMarkdown hands MarkdownPre a single <code> element with
  // the language class and the source as children. Mimic that
  // shape with a real React element so isValidElement matches.
  const codeEl = (className: string, children: string) =>
    createElement("code", { className }, children);

  it("returns the source for language-md", () => {
    const el = codeEl("language-md", "# Heading\n\nSome prose");
    expect(extractUnfoldableSource(el)).toBe("# Heading\n\nSome prose");
  });

  it("returns the source for language-markdown", () => {
    const el = codeEl("language-markdown", "**bold** text");
    expect(extractUnfoldableSource(el)).toBe("**bold** text");
  });

  it("returns the source for language-text", () => {
    const el = codeEl("language-text", "plain prose");
    expect(extractUnfoldableSource(el)).toBe("plain prose");
  });

  it("returns the source for language-plain", () => {
    const el = codeEl("language-plain", "raw text");
    expect(extractUnfoldableSource(el)).toBe("raw text");
  });

  it("unfolds a bare (no-language) fence — the agent's main case", () => {
    // The agent writes ``\`\`\`…\`\`\`` for prose / calculations /
    // ASCII diagrams. Without a language class the dispatcher
    // must treat it as unfoldable, NOT as a code pill.
    const el = codeEl("", "100 × 200 = 20000");
    expect(extractUnfoldableSource(el)).toBe("100 × 200 = 20000");
  });

  it("handles language classes mixed with other tokens", () => {
    // rehype-highlight may attach multiple class tokens
    // (e.g. ``hljs language-md``). Match on the presence of the
    // marker, not on exact string equality.
    const el = codeEl("hljs language-md", "x");
    expect(extractUnfoldableSource(el)).toBe("x");
  });

  it("strips a single trailing newline (the Fence-Node artifact)", () => {
    // CommonMark and remark leave a trailing \n on fenced-block
    // source; strip it so the unfolded prose doesn't start with
    // a blank line.
    const el = codeEl("language-md", "# H\n");
    expect(extractUnfoldableSource(el)).toBe("# H");
  });

  it("returns null for a real code language", () => {
    const el = codeEl("language-python", "print(1)");
    expect(extractUnfoldableSource(el)).toBeNull();
  });

  it("returns null when an unfold-lang is mixed with a real code lang", () => {
    // Edge case: the agent wrote ```md something — the language
    // is ambiguous. Default to code (safer — at worst the user
    // sees a code pill; unfolding code would render it as
    // markdown and could lose syntax).
    const el = codeEl("language-md language-python", "print('# H')");
    expect(extractUnfoldableSource(el)).toBeNull();
  });

  it("returns null for non-ReactElement children", () => {
    // The dispatcher guard — a stray text node, null, etc. should
    // not crash, just return null so the fallback CodeBlock path
    // takes over.
    expect(extractUnfoldableSource("plain string")).toBeNull();
    expect(extractUnfoldableSource(null)).toBeNull();
    expect(extractUnfoldableSource(undefined)).toBeNull();
    expect(extractUnfoldableSource(42)).toBeNull();
  });
});

// ── hasBoxDrawingChars ──────────────────────────────────
//
// Detection helper for the monospace <pre> route in MarkdownBlock.
// Box-drawing glyphs (┌─┐│└┘ ├┤ ┬┴┼ ─) need column alignment
// that ReactMarkdown's inline render can't provide. Plain ASCII
// math or arrows don't need this treatment — only true
// drawing glyphs.

describe("hasBoxDrawingChars", () => {
  it("detects the box-drawing characters the agent uses", () => {
    expect(hasBoxDrawingChars("┌──┐")).toBe(true);
    expect(hasBoxDrawingChars("│ hi │")).toBe(true);
    expect(hasBoxDrawingChars("└──┘")).toBe(true);
    expect(hasBoxDrawingChars("─")).toBe(true);
    expect(hasBoxDrawingChars("│")).toBe(true);
  });

  it("returns true even when only one drawing char is present", () => {
    // The check is "any drawing glyph triggers the monospace
    // path" — the user wants column alignment preserved for
    // anything visually structured, not just diagrams that are
    // 100% drawing chars.
    expect(hasBoxDrawingChars("hello ┌─ world")).toBe(true);
  });

  it("returns false for plain prose without drawing glyphs", () => {
    expect(hasBoxDrawingChars("# Heading\n\nSome **bold** prose")).toBe(false);
    expect(hasBoxDrawingChars("100 × 200 = 20000")).toBe(false);
  });

  it("returns false for math arrows that aren't box-drawing", () => {
    // The math arrows ↑→↓←≈ are not in the Box Drawing block.
    // They render fine in a proportional font; routing them
    // through the monospace path would just look weird.
    expect(hasBoxDrawingChars("↑ → ↓ ←")).toBe(false);
    expect(hasBoxDrawingChars("≈ × ÷")).toBe(false);
  });

  it("returns false for empty / non-string-ish inputs", () => {
    expect(hasBoxDrawingChars("")).toBe(false);
  });
});

// ── hasFlowChartArrows ──────────────────────────────────
//
// Complements hasBoxDrawingChars for the PreformattedBlock
// dispatcher. The agent's "latency" section uses ↓ between
// pipeline stages; those arrows only line up vertically in a
// monospace font.

describe("hasFlowChartArrows", () => {
  it("detects the arrow chars the agent uses", () => {
    expect(hasFlowChartArrows("↓")).toBe(true);
    expect(hasFlowChartArrows("↑ ↓ → ←")).toBe(true);
    expect(hasFlowChartArrows("⇄")).toBe(true);
  });

  it("returns true even when only one arrow is present", () => {
    expect(hasFlowChartArrows("User hits send\n    ↓\nBackend responds")).toBe(true);
  });

  it("returns false for plain text without arrows", () => {
    expect(hasFlowChartArrows("100 × 200 = 20000")).toBe(false);
  });
});

// ── isNoLanguageFence ───────────────────────────────────
//
// Detects a bare ```…``` fence (no language class). The
// dispatcher routes these to PreformattedBlock so line breaks
// and runs of spaces are preserved. A bare fence is also
// captured by extractUnfoldableSource (returns the source for
// "no language"), so this helper is the split point between
// the preformatted and markdown paths.

describe("isNoLanguageFence", () => {
  const codeEl = (className: string, children: string) =>
    createElement("code", { className }, children);

  it("returns true for a bare fence (no className)", () => {
    expect(isNoLanguageFence(codeEl("", "100 × 200 = 20000"))).toBe(true);
  });

  it("returns true for a fence whose only class is non-language (e.g. hljs)", () => {
    // rehype-highlight may add a ``hljs`` class with no language.
    // That's still effectively "no language" — treat as bare fence.
    expect(isNoLanguageFence(codeEl("hljs", "x"))).toBe(true);
  });

  it("returns false for an explicit language fence", () => {
    expect(isNoLanguageFence(codeEl("language-md", "x"))).toBe(false);
    expect(isNoLanguageFence(codeEl("language-python", "print(1)"))).toBe(false);
    expect(isNoLanguageFence(codeEl("language-text", "x"))).toBe(false);
  });

  it("returns false for non-ReactElement children", () => {
    expect(isNoLanguageFence("plain string")).toBe(false);
    expect(isNoLanguageFence(null)).toBe(false);
    expect(isNoLanguageFence(undefined)).toBe(false);
  });
});
