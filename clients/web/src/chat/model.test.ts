import { describe, expect, it } from "vitest";
import {
  applyEvent,
  newStreamState,
  onToolBoundary,
  splitThinkTags,
  type ChatItem,
} from "./model";
import type { ServerMessage } from "../protocol/messages";

const delta = (text: string, is_thinking = false): ServerMessage =>
  ({ type: "content_delta", text, is_thinking }) as ServerMessage;

const toolStarted: ServerMessage = {
  type: "tool_started",
  tool_name: "read_file",
  friendly_name: "Read",
  args_summary: "settings.py",
  run_id: "r1",
} as ServerMessage;

const toolCompleted: ServerMessage = {
  type: "tool_completed",
  summary: "ok",
  full_result: "content",
  run_id: "r1",
  has_markup: false,
  diff_rows: null,
  is_error: false,
} as ServerMessage;

function play(events: ServerMessage[]): ChatItem[] {
  const stream = newStreamState();
  let items: ChatItem[] = [];
  for (const e of events) items = applyEvent(items, e, stream);
  return items;
}

describe("splitThinkTags", () => {
  it("routes <think> content to thinking and the rest to text", () => {
    const s = newStreamState();
    expect(splitThinkTags(s, "<think>plan</think>answer")).toEqual([
      ["plan", true],
      ["answer", false],
    ]);
    expect(s.usesThinkTags).toBe(true);
    expect(s.inThinking).toBe(false);
  });

  it("handles tags split across deltas", () => {
    const s = newStreamState();
    expect(splitThinkTags(s, "<thi")).toEqual([]);
    expect(splitThinkTags(s, "nk>deep ")).toEqual([["deep ", true]]);
    expect(splitThinkTags(s, "thought</th")).toEqual([["thought", true]]);
    expect(splitThinkTags(s, "ink>done")).toEqual([["done", false]]);
  });

  it("keeps multi-delta thinking in thinking mode", () => {
    const s = newStreamState();
    splitThinkTags(s, "<think>first ");
    expect(splitThinkTags(s, "second")).toEqual([["second", true]]);
    expect(s.inThinking).toBe(true);
  });

  it("strips cosmetic newlines after the close tag", () => {
    const s = newStreamState();
    expect(splitThinkTags(s, "<think>x</think>\n\nanswer")).toEqual([
      ["x", true],
      ["answer", false],
    ]);
  });

  it("treats a bare stray </think> as thinking-close, not literal text", () => {
    const s = newStreamState();
    expect(splitThinkTags(s, "resumed reasoning</think>answer")).toEqual([
      ["resumed reasoning", true],
      ["answer", false],
    ]);
  });

  it("pre-enters thinking after a tool boundary for think-tag models", () => {
    const s = newStreamState();
    splitThinkTags(s, "<think>a</think>b");
    onToolBoundary(s);
    expect(s.inThinking).toBe(true);
    expect(splitThinkTags(s, "post-tool reasoning</think>final")).toEqual([
      ["post-tool reasoning", true],
      ["final", false],
    ]);
  });

  it("does NOT pre-enter thinking for models without think tags", () => {
    const s = newStreamState();
    splitThinkTags(s, "plain answer");
    onToolBoundary(s);
    expect(s.inThinking).toBe(false);
  });
});

describe("applyEvent", () => {
  it("renders inline <think> content as a thinking item", () => {
    const items = play([delta("<think>let me see</think>"), delta("the answer")]);
    expect(items.map((i) => i.kind)).toEqual(["thinking", "assistant"]);
    expect((items[0] as { text: string }).text).toBe("let me see");
    expect((items[1] as { text: string }).text).toBe("the answer");
  });

  it("never shows literal think tags in any item", () => {
    const items = play([
      delta("<thi"),
      delta("nk>hidden</think>"),
      delta("visible"),
    ]);
    for (const it of items) {
      if ("text" in it) {
        expect(it.text).not.toContain("<think>");
        expect(it.text).not.toContain("</think>");
      }
    }
  });

  it("merges consecutive same-kind deltas into one item", () => {
    const items = play([delta("Hello "), delta("world")]);
    expect(items).toHaveLength(1);
    expect((items[0] as { text: string }).text).toBe("Hello world");
  });

  it("respects the is_thinking flag from Agno reasoning events", () => {
    const items = play([delta("native reasoning", true), delta("answer")]);
    expect(items.map((i) => i.kind)).toEqual(["thinking", "assistant"]);
  });

  it("handles post-tool thinking resume without an opening tag", () => {
    const items = play([
      delta("<think>before tool</think>calling now"),
      toolStarted,
      toolCompleted,
      delta("resumed thought</think>final answer"),
    ]);
    const kinds = items.map((i) => i.kind);
    expect(kinds).toEqual(["thinking", "assistant", "tool", "thinking", "assistant"]);
    expect((items[4] as { text: string }).text).toBe("final answer");
  });

  it("updates the running tool card on completion", () => {
    const items = play([toolStarted, toolCompleted]);
    const tool = items[0] as Extract<ChatItem, { kind: "tool" }>;
    expect(tool.status).toBe("done");
    expect(tool.result).toBe("content");
  });

  it("marks failed tools as errors", () => {
    const items = play([
      toolStarted,
      { ...toolCompleted, is_error: true } as ServerMessage,
    ]);
    const tool = items[0] as Extract<ChatItem, { kind: "tool" }>;
    expect(tool.status).toBe("error");
  });
});
