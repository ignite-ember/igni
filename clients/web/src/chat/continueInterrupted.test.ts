/** Tests for the "continue from partial" prompt builder.
 *
 * Mirrors ``test_permission_mode_lock.py``'s style — direct
 * exercises of the helper with no React, no App, no mocking.
 */

import { describe, expect, it } from "vitest";
import { buildContinuedPrompt } from "./continueInterrupted";
import type { ChatItem } from "./model";

function userItem(id: number, text: string): ChatItem {
  return { kind: "user", id, text };
}
function assistantItem(
  id: number,
  text: string,
  interrupted?: "cancelled" | "errored" | "abandoned",
): ChatItem {
  return interrupted
    ? { kind: "assistant", id, text, interrupted }
    : { kind: "assistant", id, text };
}

describe("buildContinuedPrompt", () => {
  it("returns the user's text unchanged when there's no interrupted bubble", () => {
    // Normal case — the helper is a no-op.
    const items: ChatItem[] = [
      userItem(1, "first"),
      assistantItem(2, "first reply"),
      userItem(3, "second"),
      assistantItem(4, "second reply"),
    ];
    const result = buildContinuedPrompt(items, "third");
    expect(result.text).toBe("third");
    expect(result.clearIndex).toBe(-1);
  });

  it("prepends the partial when the most recent assistant is interrupted", () => {
    // The user types "continue" after a cancelled run.
    const items: ChatItem[] = [
      userItem(1, "build a todo app"),
      assistantItem(2, "I'll start by checking...", "cancelled"),
    ];
    const result = buildContinuedPrompt(items, "continue");
    expect(result.text).toContain("Continue from where you left off.");
    expect(result.text).toContain("I'll start by checking...");
    expect(result.text).toContain('"continue"');
    expect(result.clearIndex).toBe(1);
  });

  it("finds the MOST RECENT interrupted bubble, ignoring earlier ones", () => {
    // Two interrupted bubbles in history — only the last one is
    // relevant to the "continue" action.
    const items: ChatItem[] = [
      userItem(1, "first ask"),
      assistantItem(2, "first partial", "cancelled"),
      userItem(3, "continue first"),
      assistantItem(4, "second partial", "cancelled"),
    ];
    const result = buildContinuedPrompt(items, "third ask");
    expect(result.text).toContain("second partial");
    expect(result.text).not.toContain("first partial");
    expect(result.clearIndex).toBe(3);
  });

  it("escapes the user's text via JSON.stringify so markdown injection is safe", () => {
    // The user pastes something with markdown formatting that
    // would otherwise re-render as the new prompt.
    const items: ChatItem[] = [
      userItem(1, "go"),
      assistantItem(2, "stub", "cancelled"),
    ];
    const result = buildContinuedPrompt(items, "**bold** and ```code```");
    // The 5-backtick outer fence can't be closed by the 3-backtick
    // sequence inside the user's text — the partial stays fenced.
    // JSON.stringify quotes the user text so any markdown inside
    // it (headings, etc.) is preserved as a literal string.
    expect(result.text).toContain('"**bold** and ```code```"');
    // Sanity: the partial itself is in the 5-backtick fence.
    expect(result.text).toMatch(/`````\nstub\n`````/);
  });

  it("the fenced partial block prevents the agent from re-rendering partial markdown", () => {
    // Sanity: the partial is wrapped in ``` so headings / lists
    // in the partial don't bleed into the new prompt's structure.
    const items: ChatItem[] = [
      userItem(1, "ask"),
      assistantItem(2, "# Heading\n- bullet 1\n- bullet 2", "cancelled"),
    ];
    const result = buildContinuedPrompt(items, "ok");
    // Both the literal "#" and "-" are inside the fenced block,
    // so they're never seen as markdown by the next prompt.
    expect(result.text).toMatch(/```\n# Heading\n- bullet 1\n- bullet 2\n```/);
  });

  it("empty partial still returns a context wrapper (defensive)", () => {
    // If the bubble has ``interrupted`` but an empty text body
    // (shouldn't happen, but the BE sometimes does this when the
    // stream ends before any token lands), still produce a context
    // wrapper so the user doesn't get a silent no-op.
    const items: ChatItem[] = [
      userItem(1, "ask"),
      assistantItem(2, "", "cancelled"),
    ];
    const result = buildContinuedPrompt(items, "continue");
    expect(result.text).toContain("Your previous partial response was:");
    expect(result.text).toContain("```");
  });

  it("scanning picks the MOST RECENT interrupted bubble (reverse-scan)", () => {
    // Two interrupted bubbles in history. The reverse-scan finds
    // the most recent one (at index 2), not the older one at
    // index 1. The user already continued past the first one,
    // so it's stale.
    const items: ChatItem[] = [
      userItem(1, "first ask"),
      assistantItem(2, "first partial", "cancelled"),
      userItem(3, "continue first"),
      assistantItem(4, "second partial", "cancelled"),
    ];
    const result = buildContinuedPrompt(items, "third ask");
    expect(result.clearIndex).toBe(3);
    expect(result.text).toContain("second partial");
    expect(result.text).not.toContain("first partial");
  });

  it("ignores completed bubbles at the tail — only interrupted ones count", () => {
    // Trailing completed bubbles shouldn't trigger the
    // continuation wrapper. Only interrupted bubbles in the tail
    // do.
    const items: ChatItem[] = [
      userItem(1, "ask"),
      assistantItem(2, "completed"),
      assistantItem(3, "partial", "cancelled"),
      userItem(4, "second ask"),
      assistantItem(5, "second completed"),
    ];
    const result = buildContinuedPrompt(items, "third");
    // Bubble at index 2 is the most recent interrupted — even
    // though there are completed bubbles AFTER it, those don't
    // qualify and the user's "continue" reaches back to index 2.
    expect(result.clearIndex).toBe(2);
    expect(result.text).toContain("partial");
    expect(result.text).not.toContain("completed");
  });
});
